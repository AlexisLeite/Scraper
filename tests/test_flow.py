from fastapi.testclient import TestClient

from src.server import main as server
from src.common import jev_extractor as analyzer, uri_to_html as browser
from src.common.dom import candidate_groups, clean_document, group_values

HTML = '''<html><head><title>Tienda</title><script type="application/ld+json">{"@type":"Product","name":"Uno"}</script>
<script>secret()</script><style>.x{}</style></head><body><div data-studio-overlay><button>Aceptar</button></div>
<nav><a href="/categorias">Categorías</a><a href="/ofertas">Ofertas</a></nav>
<main><h1>Crema</h1><div id="cards"><article><a href="/uno">Uno</a></article><article><a href="/dos">Dos</a></article></div>
<details><summary>Ingredientes</summary><p>Agua y glicerina</p></details><section id="reviews"><h2>Reseñas</h2>
<p>No hay reseñas publicadas</p><button id="more">Cargar más</button></section></main></body></html>'''


def fake_predict(state, questions):
    answers = {}
    for key, question in questions.items():
        criteria = question.get('criteria', {})
        if key == 'page_kind':
            value = 'product'
        elif key == 'state':
            value = 'empty_widget'
        elif key == 'mode':
            value = 'unknown'
        elif key.startswith('control_'):
            value = 'other'
        elif key.startswith('route_'):
            answers[key] = {'type': 'noul', 'noul': 0.4}
            continue
        else:
            group = next((g for g in (state.get('groups', []) if isinstance(state, dict) else []) if g['id'] == key), {})
            value = 'reviews' if 'reseñas' in group.get('text', '').lower() else 'other'
        answers[key] = {'type': 'choice', 'choice': value, 'confidence': 0.9,
                        'probabilities': {name: (0.9 if name == value else 0) for name in criteria}}
    return {'model': 'jev-test', 'answers': answers}


def test_clean_dom_retains_evidence_and_opens_details():
    soup, structured = clean_document(HTML)
    assert 'secret()' not in str(soup)
    assert 'Aceptar' not in soup.get_text()
    assert soup.details.has_attr('open')
    assert 'Agua y glicerina' in soup.get_text()
    assert structured[0]['name'] == 'Uno'
    assert soup.find('a', href='/uno') is not None


def test_candidates_are_structural_and_exact_values():
    soup, _ = clean_document(HTML)
    groups = candidate_groups(soup, 'https://example.org/crema')
    cards = next(g for g in groups if g['selector'] == '#cards')
    assert cards['shape'] == 'repeated_siblings'
    assert [x['url'] for x in group_values(cards, soup, 'https://example.org/crema')['links']] == [
        'https://example.org/uno', 'https://example.org/dos']
    assert not any('role' in g for g in groups)


def test_decisions_abstain_and_keep_evidence():
    soup, structured = clean_document(HTML)
    groups = candidate_groups(soup, 'https://example.org/crema')
    page, decisions = analyzer.decide_groups('Tienda', 'https://example.org/crema', soup, structured, groups, fake_predict)
    assert page['value'] == 'product' and page['model'] == 'Jev'
    assert any(d['value'] == 'other' for d in decisions)
    assert all(d['evidence_ids'] == [d['group_id']] for d in decisions)


def test_import_analysis_and_api(monkeypatch):
    monkeypatch.setattr(server.jev_connector, 'predict', fake_predict)
    client = TestClient(server.app)
    assert client.get('/').status_code == 200
    assert client.get('/api/health').json()['jev']['configured']
    response = client.post('/api/classify', json={'content': 'Hola', 'schema': {'questions': {
        'tema': {'type': 'choice', 'instructions': 'Clasifica', 'criteria': {'a': 'A', 'b': 'B'}}}}})
    assert response.status_code == 200
    assert client.post('/api/classify', json={'content': 'Hola', 'schema': {'questions': {}}}).status_code == 400
    result = server.inspect_url('https://example.org/crema', captured=(HTML, '', 'https://example.org/crema', ''),
                                predict=fake_predict)
    assert result['classification']['value'] == 'product'
    assert result['analysis']['reviews']['state']['value'] == 'empty_widget'
    assert result['analysis']['actions'] == []
    assert 'Agua y glicerina' in result['clean_html']


def test_playwright_verifies_added_products():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'''<html><body><div id="cards"><article><a href="/one">One</a></article><article><a href="/two">Two</a></article></div><button id="more" onclick="document.querySelector('#cards').insertAdjacentHTML('beforeend','<article><a href=/three>Three</a></article>')">More</button></body></html>'''
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        result = browser.verify_action(f'http://127.0.0.1:{httpd.server_port}/',
                                      {'selector': '#more', 'text': 'More'}, primary_selector='#cards')
        assert result['effect'] == 'added_products'
        assert (result['before_products'], result['after_products']) == (2, 3)
    finally:
        httpd.shutdown()
        thread.join()


def test_direct_anchor_cards_and_numeric_id_selectors():
    soup, _ = clean_document('''<main><div id="123-grid"><a href="/one">One</a><a href="/two">Two</a></div></main>''')
    groups = candidate_groups(soup, 'https://example.org/')
    cards = next(g for g in groups if g['shape'] == 'repeated_siblings')
    assert cards['selector'].startswith('main > div')
    assert len(group_values(cards, soup, 'https://example.org/')['links']) == 2
    assert analyzer._choice({'choice': 'invalid', 'confidence': 1.0}, {'a': 'A', 'other': 'Otro'}) == ('other', None)
