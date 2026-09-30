from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from fastapi.testclient import TestClient

from src.common.scrapper import (JevHtmlToWebPageEntities, PageCapture,
                                 PlaywrightUriToHtml, Scrapper, ScrapperConfig)
from src.common.uri_to_html import verify_action
from src.server import main as server


@pytest.fixture
def local_page():
    servers = []
    def serve(html):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = html.encode()
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
                target = 'reviews-list' if key == 'records' else 'cards'
                choice = 'other' if abstain else next((g['id'] for g in state['groups'] if g['attrs'].get('id') == target), 'other')
            elif key.startswith('control_'):
                control = state['controls'][int(key.split('_')[1])]
                choice = ('load_reviews' if control['text'] == 'Cargar más' else
                          'advance_products' if control['text'] == 'Más productos' else 'other')
            else:
                group = next(g for g in state['groups'] if g['id'] == key)
                choice = ('reviews' if 'Opinión' in group['text'] or 'reseñas' in group['text'] else
                          'primary_products' if group['attrs'].get('id') == 'cards' else
                          'pagination' if group['attrs'].get('id') == 'more' else 'other')
            answers[key] = {'choice': choice, 'confidence': 0.99}
        return {'model': 'fixture', 'answers': answers}
    return predict


def test_primary_abstention_keeps_original_evidence_without_main_groups():
    html = '<main><div id="cards"><a href="/a">A</a><a href="/b">B</a></div></main>'
    output = JevHtmlToWebPageEntities(predict=predictor('listing', abstain=True))(
        PageCapture('https://fixture.test/', 'https://fixture.test/', 'Fixture', html))
    assert not any(e.role == 'primary_products' for e in output.entities)
    assert any(d.get('original_decision', {}).get('value') == 'primary_products' for d in output.decisions)


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
    scraper = Scrapper(ScrapperConfig(max_sites=1, max_hops=0, max_retries=0,
        audit_dir=tmp_path, uri_to_html=PlaywrightUriToHtml(),
        html_to_entities=JevHtmlToWebPageEntities(predict=predictor())))
    summary = scraper.run([uri])
    assert summary.succeeded == 1
    visit = scraper.audit.get_visit(scraper.audit.list_visits(summary.run_id)[0]['id'])
    assert visit['result']['reviews']['state']['value'] == 'published'
    action = visit['result']['actions'][0]
    assert action['effect'] == 'loaded_reviews'
    assert (action['before_reviews'], action['after_reviews']) == (3, 4)
    assert 'captures' not in action and {'before', 'after'} == set(action['evidence'])
    assert 'Opinión 4' in action['after_content']['reviews']['text']
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


def test_carousel_motion_is_not_product_pagination(local_page):
    uri = local_page('''<main><div id="cards"><a href=/a>A</a><a href=/b>B</a></div>
    <section id="carousel"><div>A</div><div>B</div></section>
    <button id="next" onclick="let el=document.querySelector('#carousel');el.append(el.firstElementChild)">Siguiente</button></main>''')
    action = verify_action(uri, {'selector': '#next', 'text': 'Siguiente',
        'predicted_role': 'carousel', 'collection_selector': '#carousel'}, '#cards')
    assert action['effect'] == 'moved_carousel'
    assert action['before_products'] == action['after_products'] == 2
