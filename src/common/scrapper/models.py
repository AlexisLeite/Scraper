"""Extensible contracts for crawling, classification, and content events."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


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
    verification: str = 'Jev sobre DOM'
    navigation_links: tuple[dict[str, Any], ...] | None = None

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


@dataclass(frozen=True)
class ContentEvent:
    run_id: str
    visit_id: str
    uri: str
    depth: int
    entity: WebPageEntity
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
