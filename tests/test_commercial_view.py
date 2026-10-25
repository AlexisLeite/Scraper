"""Browser regressions for structural association and taxonomy presentation."""
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

SCRIPT = Path(__file__).resolve().parents[1] / 'tests/legacy/app/commercial.js'


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<main id="view"></main>')
        page.add_script_tag(path=str(SCRIPT))
        yield page
        browser.close()


def part(kind, selector, text, path=()):
    return {'class': kind, 'confidence': .9, 'template_path': list(path),
            'values': [{'selector': selector, 'text': text}]}


def render(page, html, entities):
    return page.evaluate('''({html,entities}) => {
      const dom=new DOMParser().parseFromString(html,'text/html');
      ScrapeCommercial.render(document.querySelector('#view'),
        {page_type:'listing',result:{entities}},dom);
      return document.querySelector('#view').innerText;
    }''', {'html': html, 'entities': entities})


def test_product_fields_stay_together_and_nested_collection_does_not_split(page):
    html = '<article id="card"><a id="photo">Image</a><div id="info"><strong><span id="name">Alpha</span></strong><div><span id="price">$20</span><span id="old">$25</span></div></div></article>'
    entities = [
        {'id': 'p', 'role': 'product', 'selector': '#card', 'parts': [
            part('name', '#name', 'Alpha', [0]), part('price', '#price', '$20'),
            part('previous_price', '#old', '$25')]},
        {'id': 'nested', 'role': 'secondary_products', 'selector': '#info', 'parts': [
            part('name', '#name', 'Alpha', [0]), part('price', '#price', '$20')]},
    ]
    text = render(page, html, entities)
    assert page.locator('.commercial-product').count() == 1
    card = page.locator('.commercial-product').inner_text()
    assert all(value in card for value in ['Alpha', '$20', '$25', 'Precio anterior'])
    assert page.locator('.commercial-product dd').filter(has_text='$20').count() == 1
    assert 'Clasificaciones de colecciones por revisar' in text


def test_collection_members_do_not_mix_fields_and_other_is_discarded(page):
    html = '<ul id="list"><li><b id="a">Alpha</b><span id="ap">$20</span></li><li><b id="b">Beta</b><span id="bp">$30</span></li></ul>'
    entities = [{'id': 'list', 'role': 'primary_products', 'selector': '#list', 'parts': [
        part('name', '#a', 'Alpha', [0]), part('price', '#ap', '$20', [0]),
        part('name', '#b', 'Beta', [0]), part('price', '#bp', '$30', [0])]},
        {'id': 'ignored', 'role': 'other', 'selector': '#list', 'text': 'IGNORE'}]
    text = render(page, html, entities)
    cards = page.locator('.commercial-product')
    assert cards.count() == 2
    assert '$20' in cards.nth(0).inner_text() and '$30' not in cards.nth(0).inner_text()
    assert '$30' in cards.nth(1).inner_text() and '$20' not in cards.nth(1).inner_text()
    assert 'Lista de productos principales' in text and 'IGNORE' not in text


def test_missing_dom_keeps_values_unassociated_and_navigation_has_own_view(page):
    entities = [
        {'id': 'p', 'role': 'product', 'selector': '#missing', 'parts': [part('price', '#price', '$20')]},
        {'id': 'nav', 'role': 'categories', 'selector': '#nav', 'text': 'Care',
         'links': [{'url': 'https://example.com/care', 'text': 'Care'}]},
    ]
    text = render(page, '<nav id="nav">Care</nav>', entities)
    assert page.locator('.commercial-product').count() == 0
    assert 'Campos sin asociación DOM verificada' in text
    assert page.locator('[data-role="categories"] a').get_attribute('href') == 'https://example.com/care'


def test_rating_statistics_are_not_presented_as_individual_reviews(page):
    entities = [
        {'id': 'stats', 'role': 'reviews', 'selector': '#stats', 'parts': [part('rating_count', '#count', '91', [0])]},
        {'id': 'review', 'role': 'reviews', 'selector': '#review', 'parts': [part('author', '#author', 'Ana', [0]), part('review_body', '#body', 'Excelente', [0])]},
    ]
    render(page, '<div id="stats"><span id="count">91</span></div><article id="review"><span id="author">Ana</span><p id="body">Excelente</p></article>', entities)
    assert page.locator('.commercial-review').count() == 1
    assert 'Ana' in page.locator('.commercial-review').inner_text()
    assert '91' not in page.locator('.commercial-review').inner_text()
    assert 'Estadísticas de valoraciones' in page.locator('#view').inner_text()


def test_commercial_view_does_not_attribute_semantic_fields_to_jev(page):
    text = render(page, '<head><title>Catalog</title></head>', [
        {'id':'head', 'role':'metadata', 'selector':'head',
         'parts':[part('name', 'head > title', 'Catalog')]},
    ])
    assert 'Jev' not in text
    assert 'tipos y campos extraídos' in text
