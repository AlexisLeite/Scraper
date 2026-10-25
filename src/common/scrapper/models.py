"""Extensible contracts for crawling, classification, and content events."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:
    from playwright.sync_api import Page


@dataclass(frozen=True)
class PageCapture:
    requested_uri: str
    final_uri: str
    title: str
    html: str
    screenshot_base64: str | None = None
    interactive: bool = False


@dataclass(frozen=True)
class WebPageEntity:
    id: str
    role: str
    purposes: tuple[str, ...]
    confidence: float | None
    evidence_ids: tuple[str, ...]
    selector: str
    region: str
    heading: str
    text: str
    links: tuple[dict[str, Any], ...] = ()
    controls: tuple[dict[str, Any], ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)
    verification: str = 'DOM semántico'
    navigation_links: tuple[dict[str, Any], ...] | None = None
    parts: tuple[dict[str, Any], ...] = ()
    source_action: int | None = None
    unclassified_members: int = 0

    @property
    def is_navigation(self) -> bool:
        return 'navigation' in self.purposes

    @property
    def is_content(self) -> bool:
        return 'content' in self.purposes


@dataclass(frozen=True)
class WebPageEntities:
    page_type: str
    confidence: float | None
    model: str
    evidence_ids: tuple[str, ...]
    entities: tuple[WebPageEntity, ...]
    jsonld: tuple[dict[str, Any], ...] = ()
    decisions: tuple[dict[str, Any], ...] = ()
    reviews: dict[str, Any] = field(default_factory=dict)
    actions: tuple[dict[str, Any], ...] = ()
    classification: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContentEvent:
    run_id: str
    visit_id: str
    uri: str
    depth: int
    entity: Any
    doubtful: bool


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    scheduled: int
    succeeded: int
    failed: int
    doubtful: int
    content_events: int
    pending: int


class UriToHtml(Protocol):
    def __call__(self, uri: str) -> PageCapture: ...


class HtmlToWebPageEntities(Protocol):
    def __call__(self, capture: PageCapture) -> WebPageEntities: ...


ContentListener = Callable[[ContentEvent], None]


@dataclass(frozen=True)
class PageAnalysis:
    """Optional page-level evidence emitted by a strategy, without a content event."""
    result: Any
    doubtful: bool = False
    capture: PageCapture | None = None
    visit_id: str | None = None


@dataclass(frozen=True)
class StrategyExpense:
    """One expense in USD; None means the amount was not reported, never zero."""
    amount_usd: Decimal | str | int | None
    provider: str = ''
    category: str = 'execution'
    operation: str = ''
    status: str = 'done'
    usage: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.amount_usd is not None:
            amount = Decimal(str(self.amount_usd))
            if not amount.is_finite() or amount < 0:
                raise ValueError('El gasto debe ser un importe USD finito y no negativo.')
            object.__setattr__(self, 'amount_usd', amount)


class ExpenseRecordingError(RuntimeError):
    """An accounting failure must not cause a strategy to repeat paid work."""
    retryable = False
    phase = 'usage'


class ScrapingAPI(Protocol):
    """Worker-local API. A successful hop changes the current visit and depth."""
    run_id: str
    visit_id: str
    uri: str
    depth: int
    attempt: int
    last_disposition: str | None
    capture: PageCapture | None

    def enqueue(self, url: str, *, via: str | None = None,
                confidence: float | None = None, blocked: str | None = None) -> None: ...
    def emit_content(self, content: Any, *, doubtful: bool = False) -> None: ...
    def consume_hop(self, *, key: str | None = None, via: str | None = None,
                    confidence: float | None = None, blocked: str | None = None) -> bool: ...
    def report_expense(self, expense: StrategyExpense) -> None: ...


class ScrapingStrategy(Protocol):
    def process(self, page: Page, api: ScrapingAPI) -> None:
        """Inspect a loaded Page and report expenses, including explicit zero cost."""
        ...
