"""Traversal configuration and pluggable page execution."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ContextManager

from .audit import DEFAULT_AUDIT_DIR
from .models import ScrapingStrategy, UriToHtml
from .url_policy import URLPolicy


@dataclass(frozen=True)
class ScrapperConfig:
    max_threads: int = 4
    max_hops: int = 2
    max_sites: int = 100
    max_retries: int = 2
    audit_dir: str | Path = DEFAULT_AUDIT_DIR
    same_host_only: bool = False
    traversal: str = 'balanced'
    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()
    allowed_url_patterns: tuple[str, ...] = ()
    blocked_url_patterns: tuple[str, ...] = ()
    strategy: ScrapingStrategy | None = None
    page_loader: Callable[[str], ContextManager[Any]] | None = None
    follow_navigation: bool = True
    uri_to_html: UriToHtml | None = None

    def __post_init__(self):
        if type(self.max_threads) is not int or not 1 <= self.max_threads <= 50:
            raise ValueError('max_threads debe ser un entero entre 1 y 50.')
        URLPolicy(self.allowed_domains, self.blocked_domains,
                  self.allowed_url_patterns, self.blocked_url_patterns)
        if self.max_threads < 1 or self.max_sites < 1 or self.max_hops < 0 or self.max_retries < 0:
            raise ValueError('Los límites de threads/sitios deben ser positivos; saltos/reintentos no negativos.')
        if self.traversal not in {'balanced', 'bfs'}:
            raise ValueError('traversal debe ser balanced o bfs.')
        if self.page_loader is not None and self.uri_to_html is not None:
            raise ValueError('Usa page_loader o uri_to_html, no ambos.')
