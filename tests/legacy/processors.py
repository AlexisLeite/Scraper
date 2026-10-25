"""Default Playwright capture and Jev classification processors."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tests.legacy import jev_connector
from tests.legacy.block_tree import refresh_parts
from src.common.dom import candidate_groups, clean_document, group_values
from tests.legacy.jev_extractor import analyze_interactions, decide_groups, _select_roles
from src.common.uri_to_html import capture_page, verify_action
from src.common.scrapper.models import PageCapture, WebPageEntities, WebPageEntity

NAVIGATION_ROLES = frozenset({'categories', 'breadcrumb', 'collection_control', 'site_menu'})
CONTENT_ROLES = frozenset({'primary_products', 'secondary_products', 'product', 'reviews', 'attributes', 'offer', 'qa', 'discussion', 'editorial'})


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
        primary = _select_roles(groups, decisions, {'primary_products'})
        reviews, actions = analyze_interactions(
            soup, capture.final_uri, page, groups, decisions, primary, self.predict,
            self.verify, interactive=capture.interactive, capture_evidence=True)
        by_id = {group['id']: group for group in groups}
        entities = []
        for decision in decisions:
            group = by_id[decision['group_id']]
            values = group_values(group, soup, capture.final_uri)
            role = decision['value']
            parts = decision.get('parts', [])
            source_action = None
            unclassified_members = 0
            for action_index, action in enumerate(actions):
                kind = ('products' if role == 'primary_products' and action['effect'] in
                        {'added_products', 'replaced_products'} else
                        'reviews' if group['selector'] == reviews.get('selector') and action['effect'] in
                        {'loaded_reviews', 'replaced_reviews'} else None)
                after_values = action.get('after_content', {}).get(kind) if kind else None
                if after_values:
                    values = after_values
                    source_action = action_index
                    snapshot = action.get('captures', {}).get('after')
                    if snapshot:
                        after_soup, _ = clean_document(snapshot['html'])
                        parts, unclassified_members = refresh_parts(
                            decision.get('parts', []), group, soup, after_soup, action.get('after_url', capture.final_uri))
                    else:
                        parts = []
            purposes = []
            navigation_links = None
            if role in CONTENT_ROLES:
                purposes.append('content')
            if role in NAVIGATION_ROLES - {'collection_control'} or (role in {'primary_products', 'secondary_products'} and values['links']):
                purposes.append('navigation')
            if role == 'collection_control':
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
                navigation_links=navigation_links, parts=tuple(parts), source_action=source_action,
                unclassified_members=unclassified_members,
            ))
        return WebPageEntities(
            page_type=page['value'], confidence=page['confidence'], model=page['model'],
            evidence_ids=tuple(page['evidence_ids']), entities=tuple(entities),
            jsonld=tuple(structured), decisions=tuple(decisions),
            reviews=reviews, actions=tuple(actions), classification=page,
        )
