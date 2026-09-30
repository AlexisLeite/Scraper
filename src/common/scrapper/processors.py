"""Default Playwright capture and Jev classification processors."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .. import jev_connector
from ..dom import candidate_groups, clean_document, group_values
from ..jev_extractor import analyze_interactions, choose_primary_group, decide_groups, reconcile_primary
from ..uri_to_html import capture_page, verify_action
from .models import PageCapture, WebPageEntities, WebPageEntity

NAVIGATION_ROLES = frozenset({'categories', 'breadcrumb', 'pagination', 'site_menu'})
CONTENT_ROLES = frozenset({'primary_products', 'secondary_products', 'reviews', 'attributes'})


class PlaywrightUriToHtml:
    """Capture a complete rendered page with the existing Playwright adapter."""

    def __call__(self, uri: str) -> PageCapture:
        html, screenshot, final_uri, title = capture_page(uri)
        return PageCapture(uri, final_uri, title, html, screenshot or None, interactive=True)


class JevHtmlToWebPageEntities:
    """Classify structural DOM groups; keep unknown groups for later review."""

    def __init__(self, predict: Callable[[Any, dict], dict] | None = None, max_groups: int = 100,
                 verify=verify_action):
        if max_groups < 1:
            raise ValueError('max_groups debe ser positivo.')
        self.predict = predict or jev_connector.predict
        self.max_groups = max_groups
        self.verify = verify

    def __call__(self, capture: PageCapture) -> WebPageEntities:
        soup, structured = clean_document(capture.html)
        title = capture.title or (soup.title.get_text(' ', strip=True) if soup.title else capture.final_uri)
        groups = candidate_groups(soup, capture.final_uri, title, limit=self.max_groups)
        page, decisions = decide_groups(title, capture.final_uri, soup, structured, groups, self.predict)
        primary = []
        if page['value'] == 'listing':
            primary = choose_primary_group(soup, capture.final_uri, title, groups, decisions, self.predict)
            reconcile_primary(decisions, primary)
        reviews, actions = analyze_interactions(
            soup, capture.final_uri, page, groups, decisions, primary, self.predict,
            self.verify, interactive=capture.interactive, capture_evidence=True)
        by_id = {group['id']: group for group in groups}
        entities = []
        for decision in decisions:
            group = by_id[decision['group_id']]
            values = group_values(group, soup, capture.final_uri)
            role = decision['value']
            purposes = []
            navigation_links = None
            if role in CONTENT_ROLES:
                purposes.append('content')
            if role in NAVIGATION_ROLES - {'pagination'} or (role in {'primary_products', 'secondary_products'} and values['links']):
                purposes.append('navigation')
            if role == 'pagination':
                # Only a verified change to the principal collection authorizes a hop.
                verified_urls = {a.get('after_url') for a in actions
                                 if a.get('effect') in {'added_products', 'replaced_products'} and
                                 a.get('after_url') != capture.final_uri}
                navigation_links = tuple(link for link in values['links'] if link['url'] in verified_urls)
                if navigation_links:
                    purposes.append('navigation')
            entities.append(WebPageEntity(
                id=group['id'], role=role, purposes=tuple(purposes),
                confidence=decision['confidence'], evidence_ids=tuple(decision['evidence_ids']),
                selector=group['selector'], region=group['region'], heading=group['heading'],
                text=values['text'], links=tuple(values['links']), controls=tuple(values['controls']),
                attributes=group['attrs'], verification=decision['verification'],
                navigation_links=navigation_links,
            ))
        return WebPageEntities(
            page_type=page['value'], confidence=page['confidence'], model=page['model'],
            evidence_ids=tuple(page['evidence_ids']), entities=tuple(entities),
            jsonld=tuple(structured), decisions=tuple(decisions),
            reviews=reviews, actions=tuple(actions),
        )
