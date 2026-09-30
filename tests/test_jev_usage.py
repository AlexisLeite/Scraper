import io
import json
import threading

from src.common import jev_connector
from src.common.scrapper import (AuditStore, PageCapture, Scrapper, ScrapperConfig,
                                 WebPageEntities, WebPageEntity)


def result(uri):
    entity = WebPageEntity('content', 'attributes', ('content',), 0.9, ('content',),
                           '#content', 'main', '', uri)
    return WebPageEntities('product', 0.9, 'jev-test', ('page:text',), (entity,))


def test_connector_records_provider_usage_without_changing_response(monkeypatch):
    monkeypatch.setenv('TYPESAFE_API_KEY', 'test-only-value')
    calls = []
    response = {'model': 'jev-test', 'answers': {'kind': {'type': 'noul', 'noul': 0.8}},
                'usage': {'input_tokens': 123, 'output_tokens': 7}}
    def opener(request, timeout):
        assert request.get_header('Authorization') == 'Bearer test-only-value'
        return io.BytesIO(json.dumps(response).encode())
    with jev_connector.record_usage(calls.append):
        output = jev_connector.predict('hello', {'kind': {'type': 'noul'}}, opener=opener)
    assert output == response
    assert calls == [{'model': 'jev-test', 'input_tokens': 123, 'output_tokens': 7,
                      'request_bytes': calls[0]['request_bytes'], 'status': 'done'}]
    assert calls[0]['request_bytes'] > 0


def test_scraper_usage_is_durable_per_visit_and_aggregated(tmp_path, monkeypatch):
    monkeypatch.setenv('TYPESAFE_API_KEY', 'test-only-value')
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
    scraper = Scrapper(ScrapperConfig(max_threads=2, max_hops=0, max_sites=2,
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
    monkeypatch.setenv('TYPESAFE_API_KEY', 'test-only-value')
    uri = 'https://shop.test/'
    def classify(page):
        response = {'model': 'jev-test', 'answers': {'kind': {'type': 'noul', 'noul': 0.8}}}
        jev_connector.predict(uri, {'kind': {'type': 'noul'}},
                              opener=lambda _request, timeout: io.BytesIO(json.dumps(response).encode()))
        return result(uri)
    scraper = Scrapper(ScrapperConfig(max_threads=1, max_hops=0, max_sites=1,
                                      max_retries=0, audit_dir=tmp_path,
                                      uri_to_html=lambda value: PageCapture(value, value, value, '<html/>'),
                                      html_to_entities=classify))
    summary = scraper.run([uri])
    usage = scraper.audit.get_run(summary.run_id)['usage']
    assert usage['calls'] == 1 and usage['unreported_calls'] == 1
    assert usage['input_tokens'] == 0 and not usage['exact']


def test_cli_test_credential_overrides_server_credential(tmp_path, monkeypatch):
    from _credentials import activate_test_key
    import _credentials
    private = tmp_path / '.env.test'
    private.write_text('TYPESAFE_API_KEY=separate-test-value\n')
    monkeypatch.setattr(_credentials, 'TEST_ENV', private)
    monkeypatch.delenv('TYPESAFE_TEST_API_KEY', raising=False)
    monkeypatch.setenv('TYPESAFE_API_KEY', 'server-value')
    assert activate_test_key()
    assert jev_connector.api_key() == 'separate-test-value'
