"""Page lifecycle, strategy API, evidence and worker-local visit accounting."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..uri_to_html import capture_loaded_page, loaded_page
from .models import (ContentEvent, ExpenseRecordingError, PageAnalysis, PageCapture,
                     StrategyExpense)


def snapshot(page, uri):
    if isinstance(page, PageCapture):
        return page
    data = capture_loaded_page(page)
    return PageCapture(uri, data['url'], data['title'], data['html'],
                       data.get('screenshot_base64'), interactive=True)


@dataclass(frozen=True)
class NavigationRequest:
    uri: str
    depth: int
    parent_visit_id: str
    via: str | None = None
    confidence: float | None = None
    blocked: str | None = None


@dataclass
class ExecutionOutcome:
    navigation: list[NavigationRequest] = field(default_factory=list)
    events: list[tuple[str, ContentEvent]] = field(default_factory=list)
    succeeded: int = 0
    failed: int = 0
    doubtful: int = 0


@dataclass
class _Scope:
    visit_id: str
    uri: str
    depth: int
    attempt: int
    started: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    phase: str = 'capture'
    html_path: str | None = None
    screenshot_path: str | None = None
    analysis: PageAnalysis | None = None
    capture: PageCapture | None = None
    finished: bool = False
    strategy_started: bool = False
    expense_reported: bool = False


class StrategyAPI:
    """Navigation, content and expense reporting for any page strategy."""

    def __init__(self, audit, run_id, scope, reserve, outcome, listeners=(), strategy='', on_navigation=None, on_stage=None):
        self._audit = audit
        self.run_id = run_id
        self._scope = scope
        self._scopes = {scope.visit_id: scope}
        self._reserve = reserve
        self._outcome = outcome
        self._listeners = listeners
        self._strategy = strategy
        self.has_effects = False
        self.last_disposition = None
        self._page = None
        self._on_navigation = on_navigation
        self._on_stage = on_stage

    @property
    def visit_id(self):
        return self._scope.visit_id

    @property
    def uri(self):
        return self._scope.uri

    @property
    def depth(self):
        return self._scope.depth

    @property
    def attempt(self):
        return self._scope.attempt

    @property
    def capture(self):
        return self._scope.capture

    def enqueue(self, url, *, via=None, confidence=None, blocked=None):
        self.has_effects = True
        request = NavigationRequest(url, self.depth + 1, self.visit_id, via, confidence, blocked)
        if self._on_navigation:
            self._on_navigation(request)
        self._outcome.navigation.append(request)

    def report_expense(self, expense):
        if not isinstance(expense, StrategyExpense):
            raise TypeError('report_expense requiere StrategyExpense.')
        try:
            self._audit.record_expense(self.visit_id, self.attempt, self._strategy, expense)
            self._scope.expense_reported = True
            if expense.category == 'model_call':
                usage = expense.usage
                self._audit.record_model_call(self.visit_id, self.attempt, {
                    'model': expense.operation, 'status': expense.status,
                    'input_tokens': usage.get('input_tokens'),
                    'output_tokens': usage.get('output_tokens'),
                    'request_bytes': usage.get('request_bytes', 0)})
        except Exception as exc:
            error = 'No se pudo guardar el gasto de la estrategia; medición incompleta.'
            self._audit.mark_usage_incomplete(self.visit_id, error)
            raise ExpenseRecordingError(error) from exc

    def _ensure_expenses_reported(self):
        if self._scope.strategy_started and not self._scope.expense_reported:
            self.report_expense(StrategyExpense(None, category='unreported',
                status='missing', operation='La estrategia no reportó gastos en este estado.'))

    def _save_capture(self, capture, scope=None):
        scope = scope or self._scope
        if scope.capture is capture:
            return
        scope.html_path, scope.screenshot_path = self._audit.save_capture(
            scope.visit_id, scope.attempt, capture)
        scope.capture = capture
        scope.uri = capture.final_uri

    def emit_content(self, content, *, doubtful=False):
        self.has_effects = True
        if self._on_stage:
            self._on_stage("saving", True)
        if isinstance(content, PageAnalysis):
            scope = self._scopes[content.visit_id or self.visit_id]
            scope.analysis = content
            if content.capture is not None:
                self._save_capture(content.capture, scope)
                scope.phase = 'entities'
            self._audit.save_analysis(scope.visit_id, scope.attempt, content.result, content.doubtful)
            return
        event = ContentEvent(self.run_id, self.visit_id, self.uri, self.depth, content, doubtful)
        event_id = self._audit.record_event(event)
        self._outcome.events.append((event_id, event))
        errors = []
        for listener in self._listeners:
            try:
                listener(event)
            except Exception as exc:
                errors.append(f'{type(exc).__name__}: {exc}')
        if errors:
            self._audit.set_event_callback_errors(event_id, errors)

    def consume_hop(self, *, key=None, via=None, confidence=None, blocked=None):
        visit_id, disposition = self._reserve(self.uri, self.depth + 1, self.visit_id,
                                              key, via, confidence, blocked)
        self.last_disposition = disposition
        if visit_id is None:
            return False
        self.has_effects = True  # Never replay this strategy after a reserved interaction.
        if self._on_stage:
            self._on_stage("interaction", True)
        self._finish()
        scope = _Scope(visit_id, self.uri, self.depth + 1, 1, phase='interaction', strategy_started=True)
        self._scopes[visit_id] = scope
        self._scope = scope
        return True

    def _finish(self, error=None):
        scope = self._scope
        if scope.finished:
            return
        try:
            self._ensure_expenses_reported()
        except ExpenseRecordingError as exc:
            error = f'{error}; {exc}' if error else str(exc)
            scope.phase = exc.phase
        if self._page is not None and (scope.capture is None or scope.analysis is None):
            try:
                self._save_capture(snapshot(self._page, scope.uri), scope)
            except Exception as exc:
                capture_error = f'{type(exc).__name__}: {exc}'
                error = f'{error}; captura: {capture_error}' if error else capture_error
        scope.finished = True
        self._audit.record_attempt(scope.visit_id, scope.attempt, scope.phase,
            'error' if error else 'done', scope.started, error=error,
            html_path=scope.html_path, screenshot_path=scope.screenshot_path)
        if error:
            self._audit.fail_visit(scope.visit_id, error)
            self._outcome.failed += 1
        else:
            self._audit.finish_strategy_visit(scope.visit_id)
            self._outcome.succeeded += 1
            self._outcome.doubtful += int(bool(scope.analysis and scope.analysis.doubtful))


class PageExecutor:
    """Load and dispose pages in the same worker that invokes the strategy."""

    def __init__(self, strategy, audit, page_loader=loaded_page, max_retries=2, validate_capture=None):
        self.strategy = strategy
        self.audit = audit
        self.page_loader = page_loader
        self.max_retries = max_retries
        self.validate_capture = validate_capture
        strategy_type = type(strategy)
        self.strategy_name = f'{strategy_type.__module__}.{strategy_type.__qualname__}'

    def execute(self, item, visit_id, run_id, reserve, listeners=(), *, on_navigation=None, on_stage=None):
        outcome = ExecutionOutcome()
        for attempt in range(1, self.max_retries + 2):
            scope = _Scope(visit_id, item.uri, item.depth, attempt)
            api = StrategyAPI(self.audit, run_id, scope, reserve, outcome, listeners, self.strategy_name, on_navigation, on_stage)
            try:
                if on_stage:
                    on_stage("loading", False)
                with self.page_loader(item.uri) as page:
                    api._page = page
                    if on_stage:
                        on_stage("capture", False)
                    api._save_capture(snapshot(page, item.uri))
                    if self.validate_capture:
                        try:
                            self.validate_capture(scope.capture)
                        except Exception as exc:
                            scope.phase = getattr(exc, 'phase', 'capture_validation')
                            api._finish(f'{type(exc).__name__}: {exc}')
                            return outcome
                    scope.phase = 'entities'
                    scope.strategy_started = True
                    if on_stage:
                        on_stage("extracting", False)
                    try:
                        self.strategy.process(page, api)
                        api._finish()
                    except Exception as exc:
                        if api.has_effects:
                            # Preserve the state before disposing the live page.
                            api._scope.phase = getattr(exc, 'phase', api._scope.phase)
                            api._finish(f'{type(exc).__name__}: {exc}')
                            return outcome
                        raise
                return outcome
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
                api._scope.phase = getattr(exc, 'phase', api._scope.phase)
                can_retry = (not api.has_effects and getattr(exc, 'retryable', True)
                             and attempt <= self.max_retries)
                if can_retry:
                    try:
                        api._ensure_expenses_reported()
                    except ExpenseRecordingError as expense_error:
                        api._finish(str(expense_error))
                        return outcome
                    self.audit.record_attempt(scope.visit_id, attempt, scope.phase, 'error',
                        scope.started, error=error, html_path=scope.html_path,
                        screenshot_path=scope.screenshot_path)
                    continue
                api._finish(error)
                return outcome
        return outcome


def create_executor(config, audit):
    """Composition root; commercial defaults never enter the queue algorithm."""
    from .strategies.semantic import SemanticMetadataStrategy
    from .processors import PlaywrightUriToHtml
    strategy = config.strategy or SemanticMetadataStrategy(follow_navigation=config.follow_navigation)
    loader = config.page_loader or loaded_page
    if config.uri_to_html is not None:
        # Keep real Playwright processors on a live session; custom old capture
        # functions get a snapshot-only compatibility adapter.
        if type(config.uri_to_html) is not PlaywrightUriToHtml:
            @contextmanager
            def loader(uri):
                capture = config.uri_to_html(uri)
                if not isinstance(capture, PageCapture):
                    raise TypeError('uri_to_html debe devolver PageCapture.')
                yield capture
    return PageExecutor(strategy, audit, loader, config.max_retries)
