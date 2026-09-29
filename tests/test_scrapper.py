import base64
import threading
import time
from collections import Counter

from src.common.scrapper import (
    AuditStore, JevHtmlToWebPageEntities, PageCapture, Scrapper, ScrapperConfig,
    WebPageEntities, WebPageEntity, canonical_uri,
)


def entity(entity_id, role, purposes, links=(), confidence=0.9):
    return WebPageEntity(entity_id, role, tuple(purposes), confidence, (entity_id,),
                         f'#{entity_id}', 'main', '', entity_id,
                         tuple({'text': uri, 'url': uri} for uri in links))


def result(uri, confidence=0.9, next_uris=(), nav_confidence=0.9):
    return WebPageEntities('listing', confidence, 'Jev', ('page:text',), (
        entity('content', 'attributes', ('content',)),
        entity('navigation', 'categories', ('navigation',), next_uris, nav_confidence),
    ))


def capture(uri):
    return PageCapture(uri, uri, 'Test', f'<html><body>{uri}</body></html>',
                       base64.b64encode(b'jpeg-bytes').decode())


def test_page_is_visited_classified_and_archived_before_doubtful_hops(tmp_path):
    root = 'https://shop.test/'
    graph = {root: result(root, confidence=0.3, next_uris=(root+'next',))}
    classified = []
    def classify(page):
        classified.append(page.final_uri)
        return graph[page.final_uri]
    events = []
    scraper = Scrapper(ScrapperConfig(max_threads=2, max_hops=2, max_sites=5,
                                      max_retries=0, audit_dir=tmp_path,
                                      uri_to_html=capture, html_to_entities=classify))
    summary = scraper.run([root], events.append)
    assert classified == [root]
    assert (summary.scheduled, summary.succeeded, summary.doubtful, summary.content_events) == (1, 1, 1, 1)
    assert len(events) == 1 and events[0].doubtful
    assert [edge['disposition'] for edge in scraper.audit.list_edges(summary.run_id)] == [
        'scheduled', 'doubtful_page']
    visit = scraper.audit.list_doubtful(summary.run_id)[0]
    history = scraper.audit.get_visit(visit['id'])
    assert history['result']['entities'][0]['role'] == 'attributes'
    assert scraper.audit.read_html(visit['id']) == capture(root).html
    assert scraper.audit.read_screenshot(visit['id']) == b'jpeg-bytes'
    assert len(history['events']) == 1
    assert scraper.audit.get_run(summary.run_id)['summary']['doubtful'] == 1


def test_navigation_limits_duplicates_and_uncertain_entities(tmp_path):
    root = 'https://shop.test/'
    pages = {
        root: result(root, next_uris=(root+'b', root+'b', root+'c')),
        root+'b': result(root+'b', next_uris=(root, root+'d')),
        root+'c': result(root+'c', next_uris=(root+'e',), nav_confidence=0.2),
        root+'d': result(root+'d'),
    }
    scraper = Scrapper(ScrapperConfig(max_threads=2, max_hops=2, max_sites=4,
                                      max_retries=0, audit_dir=tmp_path,
                                      uri_to_html=capture,
                                      html_to_entities=lambda page: pages[page.final_uri]))
    summary = scraper.run([root])
    assert (summary.scheduled, summary.succeeded, summary.doubtful) == (4, 4, 0)
    assert {row['requested_uri'] for row in scraper.audit.list_visits(summary.run_id)} == set(pages)
    edges = scraper.audit.list_edges(summary.run_id)
    assert {'duplicate', 'doubtful_entity'} <= {edge['disposition'] for edge in edges}
    assert all(edge['to_visit_id'] for edge in edges if edge['disposition'] == 'scheduled')
    assert len(scraper.audit.list_events(summary.run_id)) == 4
    assert any(event['payload']['doubtful'] for event in scraper.audit.list_events(summary.run_id)) is False


def test_retries_and_site_limit_are_audited(tmp_path):
    first, second = 'https://shop.test/one', 'https://shop.test/two'
    attempts = Counter()
    def flaky(uri):
        attempts[uri] += 1
        if uri == first and attempts[uri] == 1:
            raise RuntimeError('temporary')
        return capture(uri)
    scraper = Scrapper(ScrapperConfig(max_threads=1, max_hops=0, max_sites=1,
                                      max_retries=1, audit_dir=tmp_path,
                                      uri_to_html=flaky,
                                      html_to_entities=lambda page: result(page.final_uri)))
    summary = scraper.run([first, second])
    assert (summary.scheduled, summary.succeeded, attempts[first], attempts[second]) == (1, 1, 2, 0)
    visit = scraper.audit.get_visit(scraper.audit.list_visits(summary.run_id)[0]['id'])
    assert [row['status'] for row in visit['attempts']] == ['error', 'done']
    assert [edge['disposition'] for edge in scraper.audit.list_edges(summary.run_id)] == [
        'scheduled', 'max_sites']


def test_failed_classification_keeps_full_capture_and_attempts(tmp_path):
    uri = 'https://shop.test/fail'
    def broken(_page):
        raise RuntimeError('model offline')
    scraper = Scrapper(ScrapperConfig(max_threads=1, max_hops=0, max_sites=1,
                                      max_retries=2, audit_dir=tmp_path,
                                      uri_to_html=capture, html_to_entities=broken))
    summary = scraper.run([uri])
    assert (summary.failed, summary.succeeded) == (1, 0)
    visit = scraper.audit.get_visit(scraper.audit.list_visits(summary.run_id)[0]['id'])
    assert len(visit['attempts']) == 3
    assert all(row['phase'] == 'entities' for row in visit['attempts'])
    assert scraper.audit.read_html(visit['id'], attempt=1) == capture(uri).html
    assert scraper.audit.read_html(visit['id'], attempt=3) == capture(uri).html


def test_parallel_workers_respect_limit_and_depth(tmp_path):
    active = maximum = 0
    lock = threading.Lock()
    def slow(uri):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.06)
        with lock:
            active -= 1
        return capture(uri)
    scraper = Scrapper(ScrapperConfig(max_threads=2, max_hops=0, max_sites=3,
                                      max_retries=0, audit_dir=tmp_path,
                                      uri_to_html=slow,
                                      html_to_entities=lambda page: result(page.final_uri, next_uris=(page.final_uri+'child',))))
    summary = scraper.run([f'https://shop.test/{i}' for i in range(3)])
    assert summary.scheduled == 3 and maximum == 2
    assert sum(edge['disposition'] == 'max_hops' for edge in scraper.audit.list_edges(summary.run_id)) == 3


def test_jev_html_processor_keeps_generic_entities_and_dom_values():
    html = '''<html><title>Tienda</title><body><main><h1>Cremas</h1>
    <nav><a href="/cremas">Cremas</a><a href="/serums">Sérums</a></nav>
    <div id="cards"><a href="/one">Uno</a><a href="/two">Dos</a><a href="/three">Tres</a></div>
    </main></body></html>'''
    def predict(state, questions):
        answers = {}
        for key, question in questions.items():
            if key == 'page_kind':
                choice = 'listing'
            elif key == 'primary_group':
                choice = next((g['id'] for g in state['groups'] if any(x['url'] == 'https://shop.test/one' for x in g['links'])), 'other')
            else:
                group = next(g for g in state['groups'] if g['id'] == key)
                choice = 'primary_products' if any(x['url'] == 'https://shop.test/one' for x in group['links']) else 'categories' if 'Cremas' in group['text'] else 'other'
            answers[key] = {'choice': choice, 'confidence': 0.9}
        return {'answers': answers}
    output = JevHtmlToWebPageEntities(predict=predict)(
        PageCapture('https://shop.test/', 'https://shop.test/', 'Tienda', html))
    assert output.page_type == 'listing'
    products = next(entity for entity in output.entities if entity.role == 'primary_products')
    assert products.is_content and products.is_navigation
    assert [link['url'] for link in products.links] == [
        'https://shop.test/one', 'https://shop.test/two', 'https://shop.test/three']
    assert any(entity.role == 'categories' for entity in output.entities)
    assert canonical_uri('HTTPS://Shop.Test:443/a#part') == 'https://shop.test/a'


def test_history_reopens_and_callback_errors_do_not_stop_crawl(tmp_path):
    uri = 'https://shop.test/item'
    scraper = Scrapper(ScrapperConfig(max_threads=1, max_hops=0, max_sites=1,
                                      max_retries=0, audit_dir=tmp_path,
                                      uri_to_html=capture,
                                      html_to_entities=lambda page: result(page.final_uri)))
    def broken_listener(_event):
        raise RuntimeError('consumer offline')
    summary = scraper.run([uri], broken_listener)
    reopened = AuditStore(tmp_path)
    visit_id = reopened.list_visits(summary.run_id)[0]['id']
    assert reopened.read_html(visit_id) == capture(uri).html
    assert reopened.get_visit(visit_id)['status'] == 'done'
    assert 'consumer offline' in reopened.list_events(summary.run_id)[0]['callback_errors_json']


def test_same_host_only_rejects_external_navigation(tmp_path):
    uri = 'https://shop.test/'
    scraper = Scrapper(ScrapperConfig(max_threads=1, max_hops=1, max_sites=3,
                                      max_retries=0, audit_dir=tmp_path,
                                      same_host_only=True, uri_to_html=capture,
                                      html_to_entities=lambda page: result(page.final_uri,
                                          next_uris=('https://elsewhere.test/other',))))
    summary = scraper.run([uri])
    assert summary.scheduled == 1
    assert [edge['disposition'] for edge in scraper.audit.list_edges(summary.run_id)] == [
        'scheduled', 'outside_seed_hosts']
