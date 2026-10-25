"""Generic bounded URL queue; page meaning belongs exclusively to strategies."""
from __future__ import annotations

from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from threading import Lock
from typing import Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .audit import AuditStore
from .config import ScrapperConfig
from .models import ContentListener, RunSummary
from .runtime import create_executor
from .url_policy import URLPolicy
from .page_urls import is_page_url


def canonical_uri(uri: str) -> str:
    """Normalize URL identity, preserving content parameters and path spelling."""
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
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith('utm_') and
             key.lower() not in {'fbclid', 'gclid', 'msclkid'}]
    # Sort parameter names, retaining order of repeated values (potentially semantic).
    query.sort(key=lambda pair: pair[0])
    return urlunsplit((scheme, netloc, parsed.path or '/', urlencode(query), ''))



@dataclass(frozen=True)
class _Queued:
    uri: str
    canonical: str
    depth: int
    parent_visit_id: str | None
    via: str | None
    edge_id: str


class Scrapper:
    """Schedule URL visits and atomically reserve strategy-announced hops."""

    def __init__(self, config: ScrapperConfig | None = None, audit: AuditStore | None = None):
        self.config = config or ScrapperConfig()
        self.audit = audit or AuditStore(self.config.audit_dir)
        self.executor = create_executor(self.config, self.audit)
        self._listeners = []
        self._run_lock = Lock()

    def subscribe(self, listener: ContentListener) -> None:
        self._listeners.append(listener)

    def run(self, initial_uris: Iterable[str], on_content: ContentListener | None = None,
            on_progress: Callable[[dict], None] | None = None) -> RunSummary:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError('Este orquestador ya está ejecutando un recorrido.')
        try:
            return self._run(list(initial_uris), on_content, on_progress)
        finally:
            self._run_lock.release()

    def _run(self, seeds, on_content, on_progress):
        config = self.config
        policy = URLPolicy(config.allowed_domains, config.blocked_domains,
                           config.allowed_url_patterns, config.blocked_url_patterns)
        strategy_type = type(self.executor.strategy)
        run_id = self.audit.start_run({
            'initial_uris': seeds, 'max_threads': config.max_threads,
            'max_hops': config.max_hops, 'max_sites': config.max_sites,
            'max_retries': config.max_retries, 'same_host_only': config.same_host_only,
            'traversal': config.traversal, 'usage_tracking': True,
            'expansion_visits': True, 'strategy_api': True,
            'expense_tracking': True,
            'allowed_domains': config.allowed_domains, 'blocked_domains': config.blocked_domains,
            'allowed_url_patterns': config.allowed_url_patterns,
            'blocked_url_patterns': config.blocked_url_patterns,
            'strategy': f'{strategy_type.__module__}.{strategy_type.__qualname__}',
        })
        counts = dict(scheduled=0, succeeded=0, failed=0, doubtful=0, content_events=0)
        queue = deque()
        accepted_urls = set()
        accepted_states = set()
        budget_lock = Lock()
        seed_hosts = set()
        for uri in seeds:
            try:
                seed_hosts.add(urlsplit(canonical_uri(uri)).hostname)
            except ValueError:
                pass

        def report():
            if on_progress:
                try:
                    on_progress({'run_id': run_id, **counts})
                except Exception:
                    pass

        def disposition(uri, depth, parent, blocked, key=None):
            try:
                canonical = canonical_uri(uri)
            except ValueError:
                return None, 'invalid_uri'
            if not is_page_url(canonical):
                return canonical, 'resource_url'
            if blocked:
                return canonical, blocked
            filtered = policy.disposition(canonical)
            if filtered:
                return canonical, filtered
            if depth > config.max_hops:
                return canonical, 'max_hops'
            if config.same_host_only and parent is not None and urlsplit(canonical).hostname not in seed_hosts:
                return canonical, 'outside_seed_hosts'
            if key is not None and (canonical, key) in accepted_states:
                return canonical, 'duplicate'
            if key is None and canonical in accepted_urls:
                return canonical, 'duplicate'
            return canonical, 'queued'

        def enqueue(uri, depth, parent=None, via=None, confidence=None, blocked=None):
            canonical, decision = disposition(uri, depth, parent, blocked)
            edge = self.audit.record_edge(run_id, parent, via, uri, canonical,
                                          depth, confidence, decision)
            if decision == 'queued':
                accepted_urls.add(canonical)
                queue.append(_Queued(uri, canonical, depth, parent, via, edge))

        def consume_hop(uri, depth, parent, key, via, confidence, blocked):
            # Reservation and deduplication are one operation shared by all workers.
            with budget_lock:
                canonical, decision = disposition(uri, depth, parent, blocked, key or '')
                if decision == 'queued' and counts['scheduled'] >= config.max_sites:
                    decision = 'max_sites'
                edge = self.audit.record_edge(run_id, parent, via, uri, canonical,
                                              depth, confidence, decision)
                if decision != 'queued':
                    return None, decision
                # An omitted key represents a new state, not a repeatable control.
                if key is not None:
                    accepted_states.add((canonical, key))
                visit_id = self.audit.start_visit(run_id, uri, canonical, depth,
                                                  parent, via, edge, kind='expansion')
                counts['scheduled'] += 1
                report()
                return visit_id, 'scheduled'

        for uri in seeds:
            enqueue(uri, 0)
        listeners = [*self._listeners, *([on_content] if on_content else [])]
        report()
        prefer_deep = False
        running = deque()
        dispatched = deque()
        completed = {}
        with ThreadPoolExecutor(max_workers=config.max_threads) as pool:
            while queue or running:
                depth = (min([item.depth for item in queue] + [item.depth for item, _ in running],
                             default=0) if config.traversal == 'bfs' else None)
                while queue and len(running) < config.max_threads:
                    candidates = [i for i, item in enumerate(queue) if depth is None or item.depth == depth]
                    if not candidates:
                        break
                    index = (max(candidates, key=lambda i: (queue[i].depth, i)) if
                             config.traversal == 'balanced' and prefer_deep else
                             min(candidates, key=lambda i: (queue[i].depth, i)))
                    item = queue[index]
                    del queue[index]
                    with budget_lock:
                        if counts['scheduled'] >= config.max_sites:
                            self.audit.set_edge_disposition(item.edge_id, 'max_sites')
                            continue
                        visit_id = self.audit.start_visit(run_id, item.uri, item.canonical,
                            item.depth, item.parent_visit_id, item.via, item.edge_id)
                        counts['scheduled'] += 1
                        prefer_deep = not prefer_deep
                        report()
                    future = pool.submit(self.executor.execute, item, visit_id, run_id, consume_hop, listeners)
                    running.append((item, future))
                    dispatched.append(future)
                if not running:
                    continue
                done, _ = wait([future for _, future in running], return_when=FIRST_COMPLETED)
                running = deque((item, future) for item, future in running if future not in done)
                for future in done:
                    outcome = future.result()
                    completed[future] = outcome
                    with budget_lock:
                        counts['succeeded'] += outcome.succeeded
                        counts['failed'] += outcome.failed
                        counts['doubtful'] += outcome.doubtful
                        counts['content_events'] += len(outcome.events)
                        report()
                # Free worker slots immediately; keep URL discovery in dispatch order.
                while dispatched and dispatched[0] in completed:
                    outcome = completed.pop(dispatched.popleft())
                    for request in outcome.navigation:
                        enqueue(request.uri, request.depth, request.parent_visit_id,
                                request.via, request.confidence, request.blocked)
        summary = RunSummary(run_id, **counts, pending=len(queue))
        self.audit.finish_run(summary)
        return summary
