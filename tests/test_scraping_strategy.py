from tests.legacy.strategy import JevStrategy
"""The queue works with arbitrary strategies and a live, worker-owned Page."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from threading import Barrier, Thread

import pytest

from src.common.scrapper import (PageCapture, Scrapper, ScrapperConfig,
                                 WebPageEntities, WebPageEntity)
from tests.legacy import jev_connector


@pytest.fixture
def local_site():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            html = '''<html><title>Fixture</title><body><main>
                <div id="cards"><article><a href="/a">A</a></article><article><a href="/b">B</a></article></div>
                <button id="more" onclick="document.querySelector('#cards').insertAdjacentHTML('beforeend','<article><a href=/c>C</a></article>');this.disabled=true">Más</button>
                </main></body></html>'''
            body = html.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f'http://127.0.0.1:{server.server_port}/', requests
    server.shutdown()
    server.server_close()
    worker.join()


def test_arbitrary_strategy_receives_live_page_and_emits_without_jev(tmp_path, local_site):
    url, requests = local_site
    pages = []
    events = []
    class Strategy:
        def process(self, page, api):
            pages.append(page)
            api.emit_content({'id': 'title', 'title': page.title()})
            assert events[-1].entity['title'] == 'Fixture'
            if page.url == url:
                api.enqueue(url + 'child?variant=1&utm_source=test')
                api.enqueue(url + 'child?utm_source=other&variant=1#top')
                api.enqueue(url + 'child?variant=2')
    scraper = Scrapper(ScrapperConfig(strategy=Strategy(), max_threads=1,
        max_sites=3, max_hops=1, audit_dir=tmp_path))
    summary = scraper.run([url], events.append)
    assert summary.scheduled == summary.succeeded == len(events) == 3
    assert all(page.is_closed() for page in pages)
    assert all(event.entity['title'] == 'Fixture' for event in events)
    assert scraper.audit.get_run(summary.run_id)['usage']['calls'] == 0
    assert any(edge['disposition'] == 'duplicate' for edge in scraper.audit.list_edges(summary.run_id))
    assert len([path for path in requests if not path.endswith('favicon.ico')]) == 3


@pytest.mark.parametrize('max_sites,max_hops,clicked', [(1, 2, False), (2, 0, False), (2, 1, True)])
def test_native_hop_is_consumed_before_click_and_keeps_same_page(
        tmp_path, local_site, max_sites, max_hops, clicked):
    url, requests = local_site
    seen = []
    class Strategy:
        def process(self, page, api):
            seen.append(page)
            api.emit_content({'id': 'before', 'count': page.locator('#cards > *').count()})
            if api.consume_hop(key='more'):
                assert api.depth == 1
                page.locator('#more').click()
                api.emit_content({'id': 'after', 'count': page.locator('#cards > *').count()})
    scraper = Scrapper(ScrapperConfig(strategy=Strategy(), max_threads=1,
        max_sites=max_sites, max_hops=max_hops, max_retries=3, audit_dir=tmp_path))
    summary = scraper.run([url])
    assert summary.scheduled == summary.succeeded == (2 if clicked else 1)
    visits = scraper.audit.list_visits(summary.run_id)
    assert len(seen) == 1 and requests.count('/') == 1
    if clicked:
        assert visits[1]['depth'] == 1 and visits[1]['parent_visit_id'] == visits[0]['id']
        assert 'href="/c"' in scraper.audit.read_html(visits[1]['id'])
        assert [event['payload']['entity']['count'] for event in scraper.audit.list_events(summary.run_id)] == [2, 3]


def test_native_failed_interaction_is_archived_and_not_retried(tmp_path, local_site):
    url, requests = local_site
    class Strategy:
        def process(self, page, api):
            if api.consume_hop():
                page.locator('#more').click()
                raise RuntimeError('failure after click')
    scraper = Scrapper(ScrapperConfig(strategy=Strategy(), max_sites=2,
        max_hops=1, max_retries=3, audit_dir=tmp_path))
    summary = scraper.run([url])
    assert (summary.scheduled, summary.succeeded, summary.failed) == (2, 1, 1)
    visits = scraper.audit.list_visits(summary.run_id)
    assert requests.count('/') == 1
    assert 'href="/c"' in scraper.audit.read_html(visits[1]['id'])
    assert len(scraper.audit.get_visit(visits[1]['id'])['attempts']) == 1


def test_hop_reservation_is_atomic_between_workers(tmp_path):
    barrier = Barrier(2)
    reservations = []
    @contextmanager
    def loader(uri):
        yield PageCapture(uri, uri, 'Fixture', '<main/>')
    class Strategy:
        def process(self, page, api):
            barrier.wait(timeout=3)
            reservations.append(api.consume_hop(key='more'))
    scraper = Scrapper(ScrapperConfig(strategy=Strategy(), page_loader=loader,
        max_threads=2, max_sites=3, max_hops=1, audit_dir=tmp_path))
    summary = scraper.run(['https://fixture.test/a', 'https://fixture.test/b'])
    assert sorted(reservations) == [False, True]
    assert summary.scheduled == summary.succeeded == 3


def test_expansion_depth_is_inherited_by_enqueued_links(tmp_path):
    @contextmanager
    def loader(uri):
        yield PageCapture(uri, uri, 'Fixture', '<main/>')
    class Strategy:
        def process(self, page, api):
            if api.consume_hop(key='more'):
                api.enqueue('https://fixture.test/child')
    scraper = Scrapper(ScrapperConfig(strategy=Strategy(), page_loader=loader,
        max_sites=3, max_hops=1, audit_dir=tmp_path))
    summary = scraper.run(['https://fixture.test/'])
    edges = scraper.audit.list_edges(summary.run_id)
    assert summary.scheduled == 2
    assert edges[-1]['depth'] == 2 and edges[-1]['disposition'] == 'max_hops'


def test_jev_strategy_classifies_after_click_without_reloading(tmp_path, local_site, monkeypatch):
    url, requests = local_site
    monkeypatch.setenv('TYPESAFE_TEST_API_KEY', 'fixture-only')
    captures = []
    class Classifier:
        def __call__(self, capture):
            captures.append(capture)
            response = {'answers': {}, 'usage': {'input_tokens': len(captures) * 10, 'output_tokens': 2}}
            jev_connector.predict('fixture', {}, opener=lambda request, timeout:
                                  io.BytesIO(json.dumps(response).encode()))
            content = WebPageEntity('products', 'primary_products', ('content',), .99,
                ('g1',), '#cards', 'main', '', capture.html)
            action = {'effect': 'planned', 'control': {'selector': '#more',
                'predicted_role': 'advance_products', 'confidence': .99},
                'primary_selector': '#cards', 'review_selector': None}
            return WebPageEntities('listing', .99, 'fixture', (), (content,), actions=(action,))
    scraper = Scrapper(ScrapperConfig(strategy=JevStrategy(classifier=Classifier()),
        max_sites=2, max_hops=2, max_retries=3, audit_dir=tmp_path))
    summary = scraper.run([url])
    assert summary.scheduled == summary.succeeded == 2
    assert requests.count('/') == 1 and len(captures) == 2
    assert 'href="/c"' not in captures[0].html and 'href="/c"' in captures[1].html
    parent, child = scraper.audit.list_visits(summary.run_id)
    action = scraper.audit.get_visit(parent['id'])['result']['actions'][0]
    assert action['effect'] == 'added_products' and action['child_visit_id'] == child['id']
    child_result = scraper.audit.get_visit(child['id'])['result']
    assert child_result['actions'][1]['budget_disposition'] == 'duplicate'
    assert {'before', 'after'} == set(action['evidence'])
    assert scraper.audit.get_visit(parent['id'])['usage']['input_tokens'] == 10
    assert scraper.audit.get_visit(child['id'])['usage']['input_tokens'] == 20
    usage = scraper.audit.get_run(summary.run_id)['usage']
    assert usage['calls'] == 2 and usage['input_tokens'] == 30 and usage['output_tokens'] == 4


def test_strategy_can_chain_distinct_controls_in_one_session(tmp_path, local_site):
    url, requests = local_site
    class Strategy:
        def process(self, page, api):
            page.evaluate("document.body.insertAdjacentHTML('beforeend', '<button id=second>Second</button>')")
            page.locator('#second').evaluate("node => node.onclick = () => document.body.dataset.second = 'done'")
            if api.consume_hop(key='more'):
                page.locator('#more').click()
            if api.consume_hop(key='second'):
                page.locator('#second').click()
                assert api.depth == 2
                assert page.locator('#cards > *').count() == 3
    scraper = Scrapper(ScrapperConfig(strategy=Strategy(), max_sites=3,
        max_hops=2, audit_dir=tmp_path))
    summary = scraper.run([url])
    visits = scraper.audit.list_visits(summary.run_id)
    assert summary.scheduled == summary.succeeded == 3
    assert [visit['depth'] for visit in visits] == [0, 1, 2]
    assert visits[2]['parent_visit_id'] == visits[1]['id']
    assert 'data-second="done"' in scraper.audit.read_html(visits[2]['id'])
    assert requests.count('/') == 1
