from tests.legacy.strategy import JevStrategy
"""Every strategy reports its own prices; the queue records them without pricing rules."""
from contextlib import contextmanager
from decimal import Decimal
import io
import json

import pytest
from fastapi.testclient import TestClient

from tests.legacy import jev_connector
from src.common.scrapper import (AuditStore, PageCapture, Scrapper,
                                 ScrapperConfig, StrategyExpense, WebPageEntities)
from src.server import main as server


@contextmanager
def page_loader(uri):
    yield PageCapture(uri, uri, 'Fixture', '<main/>')


def scraper(tmp_path, strategy, **limits):
    return Scrapper(ScrapperConfig(strategy=strategy, page_loader=page_loader,
        audit_dir=tmp_path, **limits))


def test_generic_costs_are_exact_and_durable_per_visit_and_api(tmp_path, monkeypatch):
    class Strategy:
        def process(self, page, api):
            api.report_expense(StrategyExpense('0.10', provider='Data service'))
            if api.consume_hop(key='next'):
                api.report_expense(StrategyExpense('0.20', provider='Data service'))
                api.emit_content({'id': 'result', 'text': 'captured'})
    crawler = scraper(tmp_path, Strategy(), max_sites=2, max_hops=1)
    summary = crawler.run(['https://fixture.test/'])
    store = AuditStore(tmp_path)
    run = store.get_run(summary.run_id)
    assert run['costs']['amount_usd'] == '0.30' and run['costs']['exact']
    assert run['costs']['reports'] == 2 and len(run['costs']['breakdown']) == 1
    assert store.list_runs()[0]['costs'] == run['costs']
    visits = store.list_visits(summary.run_id)
    assert [store.get_visit(visit['id'])['costs']['amount_usd'] for visit in visits] == ['0.10', '0.20']
    assert store.get_visit(visits[1]['id'])['expenses'][0]['provider'] == 'Data service'
    monkeypatch.setattr(server, '_audit_store', store)
    client = TestClient(server.app)
    assert client.get(f'/api/scrapes/{summary.run_id}').json()['run']['costs'] == run['costs']
    assert client.get(f'/api/scrapes/{summary.run_id}/visits/{visits[1]["id"]}').json()['expenses'][0]['amount_usd'] == '0.20'


@pytest.mark.parametrize('reported', ['zero', 'unknown', 'missing'])
def test_explicit_zero_is_distinct_from_unknown_or_missing(tmp_path, reported):
    class Strategy:
        def process(self, page, api):
            if reported != 'missing':
                api.report_expense(StrategyExpense(0 if reported == 'zero' else None))
    crawler = scraper(tmp_path, Strategy(), max_sites=1, max_hops=0)
    summary = crawler.run(['https://fixture.test/'])
    costs = crawler.audit.get_run(summary.run_id)['costs']
    assert summary.succeeded == 1
    assert costs['amount_usd'] == '0'
    assert costs['exact'] == (reported == 'zero')
    assert costs['unreported_expenses'] == (0 if reported == 'zero' else 1)
    assert costs['reports'] == 1


def test_retries_include_expenses_from_failed_attempts(tmp_path):
    class Strategy:
        def process(self, page, api):
            api.report_expense(StrategyExpense(str(api.attempt), status='error' if api.attempt == 1 else 'done'))
            if api.attempt == 1:
                raise RuntimeError('temporary failure after paid work')
    crawler = scraper(tmp_path, Strategy(), max_sites=1, max_hops=0, max_retries=1)
    summary = crawler.run(['https://fixture.test/'])
    costs = crawler.audit.get_run(summary.run_id)['costs']
    assert costs['amount_usd'] == '3' and costs['exact']
    visit = crawler.audit.get_visit(crawler.audit.list_visits(summary.run_id)[0]['id'])
    assert [expense['attempt'] for expense in visit['expenses']] == [1, 2]
    assert [expense['status'] for expense in visit['expenses']] == ['error', 'done']


def test_missing_report_is_preserved_for_each_failed_attempt(tmp_path):
    class Strategy:
        def process(self, page, api):
            raise RuntimeError('no expense report')
    crawler = scraper(tmp_path, Strategy(), max_sites=1, max_retries=2)
    summary = crawler.run(['https://fixture.test/'])
    costs = crawler.audit.get_run(summary.run_id)['costs']
    assert summary.failed == 1 and costs['unreported_expenses'] == 3 and not costs['exact']


def test_sum_preserves_decimal_precision_beyond_default_context(tmp_path):
    class Strategy:
        def process(self, page, api):
            api.report_expense(StrategyExpense('1'))
            api.report_expense(StrategyExpense('0.00000000000000000000000000001'))
    crawler = scraper(tmp_path, Strategy(), max_sites=1)
    summary = crawler.run(['https://fixture.test/'])
    assert crawler.audit.get_run(summary.run_id)['costs']['amount_usd'] == '1.00000000000000000000000000001'


def test_jev_reports_its_rate_and_only_charges_input(tmp_path, monkeypatch):
    monkeypatch.setenv('TYPESAFE_TEST_API_KEY', 'fixture-only')
    def classify(page):
        response = {'model': 'fixture', 'answers': {},
                    'usage': {'input_tokens': 291421, 'output_tokens': 98765}}
        jev_connector.predict('fixture', {}, opener=lambda request, timeout:
                              io.BytesIO(json.dumps(response).encode()))
        return WebPageEntities('other', .9, 'fixture', (), ())
    crawler = scraper(tmp_path, JevStrategy(classifier=classify), max_sites=1, max_hops=0)
    summary = crawler.run(['https://fixture.test/'])
    run = crawler.audit.get_run(summary.run_id)
    assert Decimal(run['costs']['amount_usd']) == Decimal('0.012239682')
    assert run['costs']['exact'] and run['usage']['output_tokens'] == 98765
    expense = crawler.audit.get_visit(crawler.audit.list_visits(summary.run_id)[0]['id'])['expenses'][0]
    assert expense['usage']['input_usd_per_million'] == '0.042'
    assert expense['usage']['output_cost_usd'] == '0'


def test_partial_model_usage_retains_known_costs(tmp_path, monkeypatch):
    monkeypatch.setenv('TYPESAFE_TEST_API_KEY', 'fixture-only')
    def classify(page):
        for usage in ({'input_tokens': 100, 'output_tokens': 1}, {}):
            response = {'answers': {}, 'usage': usage}
            jev_connector.predict('fixture', {}, opener=lambda request, timeout:
                                  io.BytesIO(json.dumps(response).encode()))
        return WebPageEntities('other', .9, 'fixture', (), ())
    crawler = scraper(tmp_path, JevStrategy(classifier=classify), max_sites=1)
    summary = crawler.run(['https://fixture.test/'])
    run = crawler.audit.get_run(summary.run_id)
    assert Decimal(run['costs']['amount_usd']) == Decimal('0.0000042')
    assert not run['costs']['exact'] and run['costs']['unreported_expenses'] == 1
    assert run['usage']['calls'] == 2


def test_expense_write_failure_never_repeats_paid_work(tmp_path, monkeypatch):
    calls = []
    class Strategy:
        def process(self, page, api):
            calls.append(1)
            api.report_expense(StrategyExpense('0.5'))
    crawler = scraper(tmp_path, Strategy(), max_sites=1, max_retries=3)
    def fail_write(*args):
        raise RuntimeError('storage unavailable')
    monkeypatch.setattr(crawler.audit, 'record_expense', fail_write)
    summary = crawler.run(['https://fixture.test/'])
    assert summary.failed == 1 and calls == [1]
    costs = crawler.audit.get_run(summary.run_id)['costs']
    assert costs['recording_errors'] == 1 and not costs['exact']


def test_old_runs_have_no_invented_expense_total(tmp_path):
    store = AuditStore(tmp_path)
    run_id = store.start_run({'usage_tracking': True})
    assert store.get_run(run_id)['costs'] is None
    assert store.list_runs()[0]['costs'] is None


@pytest.mark.parametrize('amount', ['-0.1', 'NaN', 'Infinity'])
def test_invalid_expense_amounts_are_rejected(amount):
    with pytest.raises(ValueError):
        StrategyExpense(amount)
