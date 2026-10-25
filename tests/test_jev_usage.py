from tests.legacy.config import ScrapperConfig as LegacyConfig
import io
import json
import threading

from tests.legacy import jev_connector
from src.common.scrapper import (AuditStore, PageCapture, Scrapper, ScrapperConfig,
                                 WebPageEntities, WebPageEntity)


def result(uri):
    entity = WebPageEntity('content', 'attributes', ('content',), 0.9, ('content',),
                           '#content', 'main', '', uri)
    return WebPageEntities('product', 0.9, 'jev-test', ('page:text',), (entity,))


def test_connector_records_provider_usage_without_changing_response(monkeypatch):
    monkeypatch.setenv('TYPESAFE_TEST_API_KEY', 'test-only-value')
    calls = []
    response = {'model': 'jev-test', 'answers': {'kind': {'type': 'noul', 'noul': 0.8}},
                'usage': {'input_tokens': 123, 'output_tokens': 7}}
    def opener(request, timeout):
        matches = request.get_header('Authorization') == 'Bearer test-only-value'
        assert matches
        return io.BytesIO(json.dumps(response).encode())
    with jev_connector.record_usage(calls.append):
        output = jev_connector.predict('hello', {'kind': {'type': 'noul'}}, opener=opener)
    assert output == response
    assert calls == [{'model': 'jev-test', 'input_tokens': 123, 'output_tokens': 7,
                      'request_bytes': calls[0]['request_bytes'], 'status': 'done'}]
    assert calls[0]['request_bytes'] > 0


def test_scraper_usage_is_durable_per_visit_and_aggregated(tmp_path, monkeypatch):
    monkeypatch.setenv('TYPESAFE_TEST_API_KEY', 'test-only-value')
    pages = ['https://shop.test/one', 'https://shop.test/two']
    barrier = threading.Barrier(2)
    def capture(uri):
        return PageCapture(uri, uri, uri, f'<html>{uri}</html>')
    def classify(page):
        barrier.wait(timeout=3)
        amount = 11 if page.final_uri == pages[0] else 22
        response = {'model': 'jev-test', 'answers': {'kind': {'type': 'noul', 'noul': 0.8}},
                    'usage': {'input_tokens': amount, 'output_tokens': 2}}
        jev_connector.predict(page.final_uri, {'kind': {'type': 'noul'}},
                              opener=lambda _request, timeout: io.BytesIO(json.dumps(response).encode()))
        return result(page.final_uri)
    scraper = Scrapper(LegacyConfig(max_threads=2, max_hops=0, max_sites=2,
                                      max_retries=0, audit_dir=tmp_path,
                                      uri_to_html=capture, html_to_entities=classify))
    summary = scraper.run(pages)
    store = AuditStore(tmp_path)
    run = store.get_run(summary.run_id)
    assert run['usage']['calls'] == 2
    assert (run['usage']['input_tokens'], run['usage']['output_tokens']) == (33, 4)
    assert run['usage']['exact'] and run['usage']['unreported_calls'] == 0
    assert store.list_runs()[0]['usage'] == run['usage']
    visits = store.list_visits(summary.run_id)
    assert [store.get_visit(visit['id'])['usage']['input_tokens'] for visit in visits] == [11, 22]
    assert all(len(store.get_visit(visit['id'])['model_calls']) == 1 for visit in visits)


def test_missing_provider_usage_is_explicitly_partial(tmp_path, monkeypatch):
    monkeypatch.setenv('TYPESAFE_TEST_API_KEY', 'test-only-value')
    uri = 'https://shop.test/'
    def classify(page):
        response = {'model': 'jev-test', 'answers': {'kind': {'type': 'noul', 'noul': 0.8}}}
        jev_connector.predict(uri, {'kind': {'type': 'noul'}},
                              opener=lambda _request, timeout: io.BytesIO(json.dumps(response).encode()))
        return result(uri)
    scraper = Scrapper(LegacyConfig(max_threads=1, max_hops=0, max_sites=1,
                                      max_retries=0, audit_dir=tmp_path,
                                      uri_to_html=lambda value: PageCapture(value, value, value, '<html/>'),
                                      html_to_entities=classify))
    summary = scraper.run([uri])
    usage = scraper.audit.get_run(summary.run_id)['usage']
    assert usage['calls'] == 1 and usage['unreported_calls'] == 1
    assert usage['input_tokens'] == 0 and not usage['exact']


def test_cli_test_credential_overrides_server_credential(tmp_path, monkeypatch):
    from tests._credentials import activate_test_key
    from tests import _credentials
    private = tmp_path / '.env.test'
    private.write_text('TYPESAFE_API_KEY=separate-test-value\n')
    monkeypatch.setattr(_credentials, 'TEST_ENV', private)
    monkeypatch.delenv('TYPESAFE_TEST_API_KEY', raising=False)
    monkeypatch.setenv('TYPESAFE_API_KEY', 'server-value')
    assert activate_test_key()
    assert jev_connector.api_key() == 'separate-test-value'
    assert __import__('os').environ['TYPESAFE_API_KEY'] == 'server-value'


def test_missing_test_key_never_falls_back_to_server(tmp_path, monkeypatch):
    from tests import _credentials
    monkeypatch.setattr(_credentials, 'TEST_ENV', tmp_path / 'missing')
    monkeypatch.delenv('TYPESAFE_TEST_API_KEY', raising=False)
    monkeypatch.setenv('TYPESAFE_API_KEY', 'server-fixture')
    env = tmp_path / '.env'
    env.write_text('TYPESAFE_API_KEY=server-file-fixture\n')
    monkeypatch.setattr(jev_connector, 'ENV_FILE', env)
    assert not _credentials.activate_test_key()
    assert not jev_connector.configured()
    import pytest
    with pytest.raises(RuntimeError, match='falta TYPESAFE_API_KEY'):
        jev_connector.predict('fixture', {'ok': {'type': 'noul'}},
                              opener=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('No HTTP allowed')))


def test_failed_usage_write_marks_incomplete_and_never_retries_paid_call(tmp_path, monkeypatch):
    monkeypatch.setenv('TYPESAFE_TEST_API_KEY', 'test-only-value')
    calls = []
    def classify(page):
        response = {'model': 'fixture', 'answers': {}, 'usage': {'input_tokens': 100, 'output_tokens': 10}}
        def opener(*args, **kwargs):
            calls.append(1)
            return io.BytesIO(json.dumps(response).encode())
        jev_connector.predict('fixture', {'ok': {'type': 'noul'}}, opener=opener)
        return result(page.final_uri)
    scraper = Scrapper(LegacyConfig(max_sites=1, max_hops=0, max_retries=2,
                                      audit_dir=tmp_path,
                                      uri_to_html=lambda uri: PageCapture(uri, uri, 'Fixture', '<main/>'),
                                      html_to_entities=classify))
    def fail_write(*args, **kwargs):
        raise RuntimeError('Simulated write failure')
    monkeypatch.setattr(scraper.audit, 'record_model_call', fail_write)
    summary = scraper.run(['https://fixture.test/'])
    assert summary.failed == 1 and summary.succeeded == 0 and len(calls) == 1
    run = scraper.audit.get_run(summary.run_id)
    assert not run['usage']['exact'] and run['usage']['recording_errors'] == 1
    visit = scraper.audit.get_visit(scraper.audit.list_visits(summary.run_id)[0]['id'])
    assert visit['usage_error'] and not visit['usage']['exact']
    assert len(visit['attempts']) == 1 and visit['attempts'][0]['phase'] == 'usage'
