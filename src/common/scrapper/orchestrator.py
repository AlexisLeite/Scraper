"""Bounded concurrent URI traversal with Jev decisions and durable evidence."""
from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

from .. import jev_connector
from .audit import DEFAULT_AUDIT_DIR, AuditStore
from .models import (ContentEvent, ContentListener, HtmlToWebPageEntities, PageCapture,
                     RunSummary, UriToHtml, WebPageEntities)
from .processors import JevHtmlToWebPageEntities, PlaywrightUriToHtml


def canonical_uri(uri: str) -> str:
    """Normalize scheme/host/default port and fragment; retain path and query."""
    parsed = urlsplit(uri.strip())
    if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('La URI debe ser HTTP(S) y no contener credenciales.')
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('Puerto inválido en la URI.') from exc
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    if ':' in host:
        host = f'[{host}]'
    default_port = 80 if scheme == 'http' else 443
    netloc = host if port is None or port == default_port else f'{host}:{port}'
    return urlunsplit((scheme, netloc, parsed.path or '/', parsed.query, ''))


def _confidence_below(value: float | None, minimum: float) -> bool:
    return value is None or not 0 <= value <= 1 or value < minimum


def _processor_name(processor: object) -> str:
    cls = type(processor)
    return f'{cls.__module__}.{cls.__qualname__}'


@dataclass(frozen=True)
class ScrapperConfig:
    max_threads: int = 4
    max_hops: int = 2
    max_sites: int = 100
    max_retries: int = 2
    min_page_confidence: float = 0.65
    min_entity_confidence: float = 0.65
    uri_to_html: UriToHtml | None = None
    html_to_entities: HtmlToWebPageEntities | None = None
    audit_dir: str | Path = DEFAULT_AUDIT_DIR
    same_host_only: bool = False

    def __post_init__(self):
        if self.max_threads < 1 or self.max_sites < 1 or self.max_hops < 0 or self.max_retries < 0:
            raise ValueError('Los límites de threads/sitios deben ser positivos; saltos/reintentos no negativos.')
        for name in ('min_page_confidence', 'min_entity_confidence'):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f'{name} debe estar entre 0 y 1.')


@dataclass(frozen=True)
class _Queued:
    uri: str
    canonical: str
    depth: int
    parent_visit_id: str | None
    via_entity_id: str | None
    edge_id: str


@dataclass(frozen=True)
class _Outcome:
    entities: WebPageEntities | None
    error: str | None


class Scrapper:
    """Run one crawl at a time; processors may execute on multiple worker threads."""

    def __init__(self, config: ScrapperConfig | None = None, audit: AuditStore | None = None):
        self.config = config or ScrapperConfig()
        self.uri_to_html = self.config.uri_to_html or PlaywrightUriToHtml()
        self.html_to_entities = self.config.html_to_entities or JevHtmlToWebPageEntities()
        self.audit = audit or AuditStore(self.config.audit_dir)
        self._listeners: list[ContentListener] = []
        self._run_lock = Lock()

    def subscribe(self, listener: ContentListener) -> None:
        self._listeners.append(listener)

    def _visit(self, visit_id: str, uri: str) -> _Outcome:
        last_error = None
        for number in range(1, self.config.max_retries + 2):
            started = datetime.now(timezone.utc).isoformat()
            phase = 'capture'
            html_path = screenshot_path = None
            try:
                capture = self.uri_to_html(uri)
                if not isinstance(capture, PageCapture):
                    raise TypeError('uri_to_html debe devolver PageCapture.')
                html_path, screenshot_path = self.audit.save_capture(visit_id, number, capture)
                phase = 'entities'
                with jev_connector.record_usage(
                    lambda call: self.audit.record_model_call(visit_id, number, call)
                ):
                    entities = self.html_to_entities(capture)
                if not isinstance(entities, WebPageEntities):
                    raise TypeError('html_to_entities debe devolver WebPageEntities.')
                self.audit.record_attempt(visit_id, number, phase, 'done', started,
                                          html_path=html_path, screenshot_path=screenshot_path)
                return _Outcome(entities, None)
            except Exception as exc:
                last_error = f'{type(exc).__name__}: {exc}'
                self.audit.record_attempt(visit_id, number, phase, 'error', started,
                                          error=last_error, html_path=html_path,
                                          screenshot_path=screenshot_path)
        return _Outcome(None, last_error)

    def run(self, initial_uris: Iterable[str], on_content: ContentListener | None = None,
            on_progress: Callable[[dict], None] | None = None) -> RunSummary:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError('Este orquestador ya está ejecutando un recorrido.')
        try:
            seeds = list(initial_uris)
            config_record = {
                'initial_uris': seeds, 'max_threads': self.config.max_threads,
                'max_hops': self.config.max_hops, 'max_sites': self.config.max_sites,
                'max_retries': self.config.max_retries,
                'min_page_confidence': self.config.min_page_confidence,
                'min_entity_confidence': self.config.min_entity_confidence,
                'same_host_only': self.config.same_host_only,
                'usage_tracking': True,
                'uri_to_html': _processor_name(self.uri_to_html),
                'html_to_entities': _processor_name(self.html_to_entities),
            }
            run_id = self.audit.start_run(config_record)
            def report(**counts):
                if on_progress:
                    try:
                        on_progress({'run_id': run_id, **counts})
                    except Exception:
                        pass  # A UI listener must not interrupt the audited crawl.

            report(scheduled=0, succeeded=0, failed=0, doubtful=0, content_events=0)
            queue: deque[_Queued] = deque()
            accepted: set[str] = set()
            seed_hosts = set()
            for uri in seeds:
                try:
                    seed_hosts.add(urlsplit(canonical_uri(uri)).hostname)
                except ValueError:
                    pass

            def enqueue(uri: str, depth: int, parent: str | None,
                        entity_id: str | None, confidence: float | None,
                        block: str | None = None) -> None:
                try:
                    canonical = canonical_uri(uri)
                except ValueError:
                    self.audit.record_edge(run_id, parent, entity_id, uri, None,
                                           depth, confidence, 'invalid_uri')
                    return
                host = urlsplit(canonical).hostname
                if block:
                    disposition = block
                elif depth > self.config.max_hops:
                    disposition = 'max_hops'
                elif self.config.same_host_only and parent is not None and host not in seed_hosts:
                    disposition = 'outside_seed_hosts'
                elif canonical in accepted:
                    disposition = 'duplicate'
                elif len(accepted) >= self.config.max_sites:
                    disposition = 'max_sites'
                else:
                    disposition = 'queued'
                edge_id = self.audit.record_edge(run_id, parent, entity_id, uri, canonical,
                                                 depth, confidence, disposition)
                if disposition == 'queued':
                    accepted.add(canonical)
                    queue.append(_Queued(uri, canonical, depth, parent, entity_id, edge_id))

            for uri in seeds:
                enqueue(uri, 0, None, None, None)

            scheduled = succeeded = failed = doubtful = content_events = 0
            listeners = [*self._listeners, *([on_content] if on_content else [])]
            with ThreadPoolExecutor(max_workers=self.config.max_threads) as pool:
                # Drain one depth before dispatching the next. Futures may finish in any
                # order; consuming them in discovery order makes the BFS history stable.
                while queue:
                    depth = queue[0].depth
                    running: deque[tuple[_Queued, str, Future[_Outcome]]] = deque()
                    while (queue and queue[0].depth == depth) or running:
                        while (queue and queue[0].depth == depth and
                               len(running) < self.config.max_threads):
                            item = queue.popleft()
                            visit_id = self.audit.start_visit(run_id, item.uri, item.canonical,
                                                              item.depth, item.parent_visit_id,
                                                              item.via_entity_id, item.edge_id)
                            running.append((item, visit_id, pool.submit(self._visit, visit_id, item.uri)))
                            scheduled += 1
                            report(scheduled=scheduled, succeeded=succeeded, failed=failed,
                                   doubtful=doubtful, content_events=content_events)
                        item, visit_id, future = running.popleft()
                        try:
                            outcome = future.result()
                        except Exception as exc:
                            outcome = _Outcome(None, f'{type(exc).__name__}: {exc}')
                        if outcome.entities is None:
                            failed += 1
                            self.audit.fail_visit(visit_id, outcome.error or 'Error desconocido')
                            report(scheduled=scheduled, succeeded=succeeded, failed=failed,
                                   doubtful=doubtful, content_events=content_events)
                            continue
                        result = outcome.entities
                        succeeded += 1
                        page_doubtful = _confidence_below(result.confidence,
                                                           self.config.min_page_confidence)
                        doubtful += int(page_doubtful)
                        self.audit.complete_visit(visit_id, result, page_doubtful)
                        for entity in result.entities:
                            entity_doubtful = _confidence_below(entity.confidence,
                                                                self.config.min_entity_confidence)
                            if entity.is_content:
                                event = ContentEvent(run_id, visit_id, item.uri, item.depth,
                                                     entity, page_doubtful or entity_doubtful)
                                event_id = self.audit.record_event(event)
                                content_events += 1
                                errors = []
                                for listener in listeners:
                                    try:
                                        listener(event)
                                    except Exception as exc:
                                        errors.append(f'{type(exc).__name__}: {exc}')
                                if errors:
                                    self.audit.set_event_callback_errors(event_id, errors)
                            if entity.is_navigation:
                                block = ('doubtful_page' if page_doubtful else
                                         'doubtful_entity' if entity_doubtful else None)
                                for link in entity.links:
                                    target = link.get('url')
                                    if isinstance(target, str) and target:
                                        enqueue(target, item.depth + 1, visit_id,
                                                entity.id, entity.confidence, block)
                        report(scheduled=scheduled, succeeded=succeeded, failed=failed,
                               doubtful=doubtful, content_events=content_events)
            summary = RunSummary(run_id, scheduled, succeeded, failed, doubtful,
                                 content_events, len(queue))
            self.audit.finish_run(summary)
            return summary
        finally:
            self._run_lock.release()
