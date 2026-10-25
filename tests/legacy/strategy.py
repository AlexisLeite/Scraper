"""Commercial extraction and navigation policy using Jev on a live page."""
from __future__ import annotations

from src.common.scrapper.strategies.discovery import discover_links

import json
from dataclasses import replace
from decimal import Decimal

from tests.legacy import jev_connector
from tests.legacy.jev_extractor import plan_interactions
from src.common.uri_to_html import verify_action, verify_loaded_action
from src.common.scrapper.models import PageAnalysis, PageCapture, StrategyExpense, WebPageEntities
from tests.legacy.processors import JevHtmlToWebPageEntities
from src.common.scrapper.runtime import snapshot


def confidence_below(value, minimum):
    return value is None or not 0 <= value <= 1 or value < minimum


class JevStrategy:
    """Own semantic decisions, content events and budgeted collection interactions."""

    def __init__(self, *, predict=None, classifier=None,
                 min_page_confidence=0.65, min_entity_confidence=0.65,
                 follow_navigation=True, verify=verify_loaded_action,
                 input_usd_per_million='0.042'):
        self.input_usd_per_million = Decimal(str(input_usd_per_million))
        if not self.input_usd_per_million.is_finite() or self.input_usd_per_million < 0:
            raise ValueError('La tarifa de entrada debe ser finita y no negativa.')
        self.classifier = classifier or JevHtmlToWebPageEntities(predict=predict)
        self.min_page_confidence = min_page_confidence
        self.min_entity_confidence = min_entity_confidence
        self.follow_navigation = follow_navigation
        self.verify = verify

    def _classify(self, capture, api):
        reported = False
        def report(call):
            nonlocal reported
            reported = True
            tokens = call['input_tokens']
            amount = (Decimal(tokens) / Decimal(1_000_000) * self.input_usd_per_million
                      if tokens is not None else None)
            api.report_expense(StrategyExpense(amount, provider='TypeSafe', category='model_call',
                operation=call['model'], status=call['status'], usage={**call,
                    'input_usd_per_million': str(self.input_usd_per_million),
                    'input_cost_usd': str(amount) if amount is not None else None,
                    'output_cost_usd': '0'}))
        try:
            with jev_connector.record_usage(report):
                with plan_interactions():
                    result = self.classifier(capture)
        finally:
            if not reported:
                api.report_expense(StrategyExpense(0, provider='TypeSafe',
                    operation='classification_without_model_calls'))
        if not isinstance(result, WebPageEntities):
            raise TypeError('html_to_entities debe devolver WebPageEntities.')
        return result

    def _publish(self, result, capture, api):
        page_doubtful = confidence_below(result.confidence, self.min_page_confidence)
        api.emit_content(PageAnalysis(result, page_doubtful, capture))
        for entity in result.entities:
            entity_doubtful = confidence_below(entity.confidence, self.min_entity_confidence)
            if entity.is_content:
                api.emit_content(entity, doubtful=page_doubtful or entity_doubtful)
            if entity.is_navigation and self.follow_navigation:
                links = entity.navigation_links if entity.navigation_links is not None else entity.links
                for link in links:
                    target = link.get('url')
                    if isinstance(target, str) and target:
                        api.enqueue(target, via=entity.id, confidence=entity.confidence)
        return page_doubtful

    def process(self, page, api):
        self._process(page, api)

    def _process(self, page, api):
        capture = api.capture or snapshot(page, api.uri)
        if self.follow_navigation:
            discover_links(capture, api)
        result = self._classify(capture, api)
        page_doubtful = self._publish(result, capture, api)
        # Each successful reservation advances the current branch and preserves
        # the live browser. Reclassification can nominate new, distinct controls.
        while True:
            expanded = False
            for index, planned in enumerate(result.actions):
                if planned.get('effect') != 'planned':
                    continue
                confidence = planned['control'].get('confidence')
                blocked = ('doubtful_page' if page_doubtful else 'doubtful_entity' if
                           confidence_below(confidence, self.min_entity_confidence) else None)
                parent_id = api.visit_id
                key = json.dumps([planned['control']['selector'],
                                  planned['control']['predicted_role']])
                if not api.consume_hop(key=key, via=f'action:{index}',
                                       confidence=confidence, blocked=blocked):
                    updated = {**planned, 'budget_disposition': api.last_disposition}
                    result = replace(result, actions=tuple(updated if i == index else action
                                     for i, action in enumerate(result.actions)))
                    api.emit_content(PageAnalysis(result, page_doubtful))
                    continue
                try:
                    api.report_expense(StrategyExpense(0, provider='Playwright',
                        category='interaction', operation=planned['control']['predicted_role']))
                    if isinstance(page, PageCapture):
                        # Legacy snapshot processors retain their verifier interface.
                        verifier = getattr(self.classifier, 'verify', verify_action)
                        check = verifier(capture.final_uri, planned['control'],
                            planned.get('primary_selector'), planned.get('review_selector'),
                            capture_evidence=True)
                    else:
                        check = self.verify(page, planned['control'], planned.get('primary_selector'),
                                            planned.get('review_selector'), capture_evidence=True)
                except Exception as exc:
                    check = {'effect': 'unverified', 'control': planned['control'],
                             'reason': f'{type(exc).__name__}: {exc}'}
                    if not isinstance(page, PageCapture):
                        after = snapshot(page, api.uri)
                        check['captures'] = {'after': {'url': after.final_uri, 'title': after.title,
                            'html': after.html, 'screenshot_base64': after.screenshot_base64}}
                    self._save_action(result, index, check, parent_id, page_doubtful, api)
                    raise
                self._save_action(result, index, check, parent_id, page_doubtful, api)
                after = check.get('captures', {}).get('after')
                if after:
                    capture = PageCapture(api.uri, after['url'], after['title'], after['html'],
                                          after.get('screenshot_base64'), interactive=True)
                    api.emit_content(PageAnalysis(WebPageEntities('other', None, 'Jev', (), (),
                                                  actions=(check,)), capture=capture))
                if not after or check['effect'] in {'not_found', 'not_interactable', 'unverified'}:
                    raise RuntimeError(f"No se obtuvo estado posterior: {check['effect']}")
                if self.follow_navigation:
                    discover_links(capture, api)
                result = self._classify(capture, api)
                result = replace(result, actions=(check, *result.actions),
                    entities=tuple(replace(entity, source_action=0) for entity in result.entities))
                page_doubtful = self._publish(result, capture, api)
                expanded = True
                break  # Controls from the previous DOM are stale; use its new analysis.
            if not expanded:
                return

    def _save_action(self, result, index, check, parent_id, doubtful, api):
        parent_check = {**check, 'child_visit_id': api.visit_id, 'budget_disposition': 'scheduled'}
        parent_result = replace(result, actions=tuple(parent_check if i == index else action
                                for i, action in enumerate(result.actions)))
        api.emit_content(PageAnalysis(parent_result, doubtful, visit_id=parent_id))
        api.emit_content(PageAnalysis(WebPageEntities('other', None, 'Jev', (), (), actions=(check,))))
