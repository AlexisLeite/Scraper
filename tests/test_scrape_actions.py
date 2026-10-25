from tests.legacy.config import ScrapperConfig as LegacyConfig
from tests.legacy.processors import JevHtmlToWebPageEntities
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from fastapi.testclient import TestClient

from src.common.scrapper import (PageCapture,
                                 PlaywrightUriToHtml, Scrapper, ScrapperConfig)
from src.common.uri_to_html import verify_action
from src.server import main as server


@pytest.fixture
def local_page():
    servers = []
    def serve(html, pages=None):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = (pages or {}).get(self.path.split('?')[0], html).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        servers.append((httpd, thread))
        return f'http://127.0.0.1:{httpd.server_port}/'
    yield serve
    for httpd, thread in servers:
        httpd.shutdown()
        thread.join()


def predictor(kind='product', published=True, abstain=False):
    def predict(state, questions):
        answers = {}
        for key in questions:
            if key == 'page_kind':
                choice = kind
            elif key == 'state':
                choice = 'published' if published else 'empty_widget'
            elif key == 'mode':
                choice = 'load_more' if published else 'unknown'
            elif key in ('records', 'primary_group'):
                if key == 'records':
                    candidates = [g['id'] for g in state['groups'] if g['attrs'].get('id') == 'reviews-list']
                else:
                    candidates = [g['id'] for g in state['summary']['blocks'] if g['class'] == 'products']
                choice = 'other' if abstain else next(iter(candidates), 'other')
            elif key.startswith('f') and key[1:].isdigit():
                choice = 'other'
            elif key.startswith('control_'):
                control = state['controls'][int(key.split('_')[1])]
                assert control['id'] == key and key in questions[key]['instructions']
                choice = ('load_reviews' if control['text'] == 'Cargar más' else
                          'advance_products' if control['text'] == 'Más productos' else 'other')
            else:
                group = next(g for g in state['groups'] if g['id'] == key)
                choice = ('reviews' if 'Opinión' in group['text'] or 'reseñas' in group['text'] else
                          'products' if group['attrs'].get('id') == 'cards' else
                          'collection_control' if group['attrs'].get('id') == 'more' else 'other')
            answers[key] = {'choice': choice, 'confidence': 0.99}
        return {'model': 'fixture', 'answers': answers}
    return predict


def test_primary_abstention_keeps_original_evidence_without_main_groups():
    html = '<main><div id="cards"><a href="/a">A</a><a href="/b">B</a></div></main>'
    output = JevHtmlToWebPageEntities(predict=predictor('listing', abstain=True))(
        PageCapture('https://fixture.test/', 'https://fixture.test/', 'Fixture', html))
    assert not any(e.role == 'primary_products' for e in output.entities)
    assert any(d.get('original_decision', {}).get('value') == 'products' for d in output.decisions)


def test_empty_widget_is_distinguished_and_not_clicked():
    html = '<main><section><h2>Opinión</h2>No hay reseñas<button>Cargar más</button></section></main>'
    calls = []
    output = JevHtmlToWebPageEntities(predict=predictor(published=False),
        verify=lambda *args, **kwargs: calls.append(1))(
        PageCapture('https://fixture.test/', 'https://fixture.test/', 'Fixture', html, interactive=True))
    assert output.reviews['state']['value'] == 'empty_widget'
    assert not calls and not output.actions


def test_real_reviews_action_is_archived_and_exposed_by_api(local_page, tmp_path, monkeypatch):
    html = '''<html><title>Ficha</title><body><main><section><h2>Opinión</h2>
    <div id="reviews-list"><article><h3>Opinión 1</h3>Buena</article><article><h3>Opinión 2</h3>Excelente</article><article><h3>Opinión 3</h3>Útil</article></div>
    <button onclick="document.querySelector('#reviews-list').insertAdjacentHTML('beforeend','<article><h3>Opinión 4</h3>Nueva</article>')">Cargar más</button>
    </section></main></body></html>'''
    uri = local_page(html)
    scraper = Scrapper(LegacyConfig(max_sites=2, max_hops=1, max_retries=0, follow_navigation=False,
        audit_dir=tmp_path, uri_to_html=PlaywrightUriToHtml(),
        html_to_entities=JevHtmlToWebPageEntities(predict=predictor())))
    summary = scraper.run([uri])
    assert summary.succeeded == 2
    visit = scraper.audit.get_visit(scraper.audit.list_visits(summary.run_id)[0]['id'])
    assert visit['result']['reviews']['state']['value'] == 'published'
    action = visit['result']['actions'][0]
    assert action['effect'] == 'loaded_reviews'
    assert (action['before_reviews'], action['after_reviews']) == (3, 4)
    assert 'captures' not in action and {'before', 'after'} == set(action['evidence'])
    assert 'Opinión 4' in action['after_content']['reviews']['text']
    child = scraper.audit.get_visit(scraper.audit.list_visits(summary.run_id)[1]['id'])
    updated = next(e for e in child['result']['entities'] if e['source_action'] == 0 and e['role'] == 'reviews')
    assert 'Opinión 4' in updated['text']
    assert 'Opinión 4' not in action['before_content']['reviews']['text']
    after = scraper.audit.read_action_artifact(visit['id'], 0, 'after', 'html')
    assert 'Nueva</article>' in after
    assert scraper.audit.read_action_artifact(visit['id'], 0, 'before', 'screenshot')
    assert scraper.audit.read_action_artifact(visit['id'], -1, 'after', 'html') is None
    monkeypatch.setattr(server, '_audit_store', scraper.audit)
    client = TestClient(server.app)
    base = f'/api/scrapes/{summary.run_id}/visits/{visit["id"]}/actions/0/after'
    assert client.get(base + '/html').text == after
    assert client.get(base + '/screenshot').status_code == 200
    assert client.get(base.replace(summary.run_id, 'wrong') + '/html').status_code == 404


def test_real_product_action_compares_main_collection(local_page):
    uri = local_page('''<main><div id="cards"><article><a href="/a">A</a></article><article><a href="/b">B</a></article></div>
    <button id="more" onclick="document.querySelector('#cards').insertAdjacentHTML('beforeend','<article><a href=/c>C</a></article>')">Más productos</button></main>''')
    capture = PlaywrightUriToHtml()(uri)
    output = JevHtmlToWebPageEntities(predict=predictor('listing'))(capture)
    action = output.actions[0]
    assert action['effect'] == 'added_products'
    assert (action['before_products'], action['after_products']) == (2, 3)
    assert action['captures']['after']['html']
    main = next(e for e in output.entities if e.role == 'primary_products')
    assert main.source_action == 0 and any(link['url'] == uri + 'c' for link in main.links)


def test_carousel_motion_is_not_product_pagination(local_page):
    uri = local_page('''<main><div id="cards"><a href=/a>A</a><a href=/b>B</a></div>
    <section id="carousel"><div>A</div><div>B</div></section>
    <button id="next" onclick="let el=document.querySelector('#carousel');el.append(el.firstElementChild)">Siguiente</button></main>''')
    action = verify_action(uri, {'selector': '#next', 'text': 'Siguiente',
        'predicted_role': 'carousel', 'collection_selector': '#carousel'}, '#cards')
    assert action['effect'] == 'moved_carousel'
    assert action['before_products'] == action['after_products'] == 2


def test_counter_and_display_changes_do_not_replace_products(local_page):
    uri = local_page('''<main><div id="cards"><a href="/a">A 10 EUR</a><a href="/b">B</a>
    <div id="count">2/10</div><button id="more" onclick="document.querySelector('#count').textContent='2/20';document.querySelector('#cards a').textContent='A 9 EUR'">Más productos</button></div></main>''')
    action = verify_action(uri, {'selector': '#more', 'text': 'Más productos'}, '#cards', timeout_ms=600)
    assert action['effect'] == 'no_effect'
    assert (action['before_products'], action['after_products']) == (2, 2)
    assert action['before_product_nodes'] == 4
    assert action['product_identities']['added'] == action['product_identities']['removed'] == []


def test_same_count_replacement_records_changed_product_destinations(local_page):
    uri = local_page('''<div id="cards"><a href="/a">A</a><a href="/b">B</a></div>
    <button id="next" onclick="document.querySelector('#cards a').href='/c'">Siguiente</button>''')
    action = verify_action(uri, {'selector': '#next', 'text': 'Siguiente'}, '#cards')
    assert action['effect'] == 'replaced_products'
    assert (action['before_products'], action['after_products']) == (2, 2)
    assert action['product_identities']['added'] == [uri + 'c']
    assert action['product_identities']['removed'] == [uri + 'a']


def test_product_identity_ignores_order_and_fragments_but_preserves_queries():
    from src.common.uri_to_html import _items_at, collection_change
    from src.common.dom import clean_document
    before, _ = clean_document('<div id="cards"><a href="/a#one">A</a><a href="/b?v=1">B</a><button>2/2</button></div>')
    after, _ = clean_document('<div id="cards"><a href="/b?v=1">New B</a><a href="/a#two">New A</a><button>4/4</button></div>')
    old = _items_at(before, '#cards', 'https://fixture.test/')
    new = _items_at(after, '#cards', 'https://fixture.test/')
    assert len(old) == len(new) == 2
    assert collection_change(old, new, 'added', 'replaced') is None
    assert collection_change(old, ['https://fixture.test/a', 'https://fixture.test/b?v=2'], 'added', 'replaced') == 'replaced'
    assert collection_change(['a', 'a'], ['a', 'b'], 'added', 'replaced') == 'replaced'


def test_lazy_control_initializes_after_scrolling_before_click(local_page):
    uri = local_page('''<div style="height:3000px"></div>
    <div id="cards"><a href="/a">A</a><a href="/b">B</a></div><button id="more">Más productos</button>
    <script>const button=document.querySelector('#more');
    const observer=new IntersectionObserver(entries=>{if(entries.some(e=>e.isIntersecting)){
      observer.disconnect();setTimeout(()=>button.onclick=()=>document.querySelector('#cards').insertAdjacentHTML('beforeend','<a href=/c>C</a>'),900);
    }});observer.observe(button);</script>''')
    action = verify_action(uri, {'selector': '#more', 'text': 'Más productos'}, '#cards')
    assert action['effect'] == 'added_products'
    assert (action['before_products'], action['after_products']) == (2, 3)


def test_hidden_control_is_reported_without_click(local_page):
    uri = local_page('<div id="cards"><a href=/a>A</a></div><button id="more" style="display:none">Más</button>')
    action = verify_action(uri, {'selector': '#more', 'text': 'Más'}, '#cards')
    assert action['effect'] == 'not_interactable'


def test_loading_markup_is_not_mistaken_for_completed_product_action(local_page):
    uri = local_page("""<div id=cards><a href=/a>A</a><a href=/b>B</a></div>
    <button id=more onclick="document.querySelector('#cards').insertAdjacentHTML('beforeend','<span>Cargando...</span>');setTimeout(()=>document.querySelector('#cards').insertAdjacentHTML('beforeend','<a href=/c>C</a>'),3500)">Más</button>""")
    action = verify_action(uri, {'selector': '#more', 'text': 'Más'}, '#cards')
    assert action['effect'] == 'added_products'
    assert (action['before_products'], action['after_products']) == (2, 3)


def test_fetch_completion_is_recorded_and_query_values_are_not_saved(local_page):
    uri = local_page('''<div id=cards><a href=/a>A</a><a href=/b>B</a></div>
    <button id=more onclick="fetch('/records?session=private-test-value').then(r=>r.text()).then(()=>document.querySelector('#cards').insertAdjacentHTML('beforeend','<a href=/c>C</a>'))">Más</button>''')
    action = verify_action(uri, {'selector': '#more', 'text': 'Más'}, '#cards')
    assert action['effect'] == 'added_products' and action['network_settled']
    request = next(r for r in action['network'] if r['url'] == uri + 'records')
    assert request['status'] == 'finished' and request['http_status'] == 200
    assert request['type'] == 'fetch'
    assert 'private-test-value' not in str(action['network'])


def test_failed_fetch_does_not_prove_product_pagination(local_page):
    uri = local_page('''<div id=cards><a href=/a>A</a><a href=/b>B</a></div>
    <button id=more onclick="fetch('http://127.0.0.1:1/missing').catch(()=>{})">Más</button>''')
    action = verify_action(uri, {'selector': '#more', 'text': 'Más'}, '#cards', timeout_ms=800)
    assert action['effect'] == 'no_effect' and action['network_settled']
    assert any(r['status'] == 'failed' for r in action['network'])


def test_url_pagination_preserves_before_and_after_base_urls(local_page):
    uri = local_page('<div id=cards><a href=a>A</a><a href=b>B</a></div><a id=next href="catalog/page2/">Siguiente</a>',
        pages={'/catalog/page2/': '<div id=cards><a href=c>C</a><a href=d>D</a></div>'})
    action = verify_action(uri, {'selector': '#next', 'text': 'Siguiente'}, '#cards')
    assert action['effect'] == 'replaced_products'
    assert action['before_url'] == uri and action['after_url'] == uri + 'catalog/page2/'
    assert action['before_content']['products']['links'][0]['url'] == uri + 'a'
    assert action['after_content']['products']['links'][0]['url'] == uri + 'catalog/page2/c'
    assert any(r['type'] == 'document' and r['http_status'] == 200 for r in action['network'])
