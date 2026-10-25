"""Standard markup fixtures and cross-site regressions, never production site rules."""
from contextlib import contextmanager

from tests.legacy import jev_connector
from src.common.scrapper import (PageCapture, Scrapper, ScrapperConfig,
                                 SemanticMetadataStrategy)
from src.server.main import ScrapeRequest


def run_html(tmp_path, html, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('The semantic strategy must never call a model')
    monkeypatch.setattr(jev_connector, 'predict', forbidden)
    @contextmanager
    def loader(uri):
        yield PageCapture(uri, uri, 'Fixture', html)
    crawler = Scrapper(ScrapperConfig(strategy=SemanticMetadataStrategy(), page_loader=loader,
        audit_dir=tmp_path, max_sites=1, max_hops=0))
    summary = crawler.run(['https://unrelated.test/item'])
    detail = crawler.audit.get_visit(crawler.audit.list_visits(summary.run_id)[0]['id'])
    assert detail['costs']['amount_usd'] == '0' and detail['costs']['exact']
    assert detail['usage']['calls'] == 0 and summary.succeeded == 1
    return detail['result']


def texts(entity, label):
    return [value['text'] for part in entity['parts'] if part['class'] == label for value in part['values']]


def test_jsonld_graph_product_and_prices_keep_provenance(tmp_path, monkeypatch):
    result = run_html(tmp_path, '''<head><meta property="og:title" content="Declared title">
        <script type="application/ld+json">{"@context":"https://schema.org","@graph":[
          {"@type":"Product","name":"A","image":"/a.jpg","offers":{"@type":"Offer","price":"12.50","priceCurrency":"USD"}},
          {"@type":"Review","reviewBody":"Useful","author":{"@type":"Person","name":"Ana"}}
        ]}</script></head><main><h1>A</h1></main>''', monkeypatch)
    product = next(entity for entity in result['entities'] if entity['role'] == 'product')
    assert texts(product, 'name') == ['A'] and texts(product, 'price') == ['12.50']
    assert product['attributes']['source'] == 'jsonld'
    assert 'no prueban visibilidad' in product['verification']
    review = next(entity for entity in result['entities'] if entity['role'] == 'reviews')
    assert texts(review, 'author') == ['Ana']
    assert result['classification']['metadata']['og:title'] == ['Declared title']


def test_microdata_and_rdfa_use_standard_properties(tmp_path, monkeypatch):
    result = run_html(tmp_path, '''<main><article itemscope itemtype="https://schema.org/Product">
        <h1 itemprop="name">Alpha</h1><meta itemprop="price" content="7.99"><a itemprop="url" href="/alpha">Open</a>
        </article><section vocab="https://schema.org/" typeof="Product">
        <h2 property="name">Beta</h2><span property="price" content="8.99">8,99</span></section></main>''', monkeypatch)
    products = [entity for entity in result['entities'] if entity['role'] == 'product']
    assert len(products) == 2
    assert texts(products[0], 'name') == ['Alpha'] and texts(products[1], 'price') == ['8.99']
    assert products[0]['links'][0]['url'] == 'https://unrelated.test/alpha'


def test_arbitrary_classes_and_marketing_words_do_not_classify_products(tmp_path, monkeypatch):
    result = run_html(tmp_path, '''<main><div class="product-pod">Buy Product Best Cream Price $9</div>
        <article><h2>Independent article</h2><p>Body</p></article>
        <nav><a href="/next">Next</a><a href="javascript:alert(1)">Bad</a></nav></main>''', monkeypatch)
    assert not any(entity['role'] == 'product' for entity in result['entities'])
    article = next(entity for entity in result['entities'] if entity['role'] == 'semantic_article')
    assert texts(article, 'name') == ['Independent article']
    navigation = next(entity for entity in result['entities'] if entity['role'] == 'site_menu')
    assert [link['url'] for link in navigation['links']] == ['https://unrelated.test/next']


def test_malformed_metadata_is_audited_without_losing_visible_content(tmp_path, monkeypatch):
    result = run_html(tmp_path, '<script type="application/ld+json">{invalid}</script><main><article><h1>Visible</h1></article></main>', monkeypatch)
    assert result['classification']['coverage']['jsonld_errors']
    assert any(entity['role'] == 'semantic_article' for entity in result['entities'])


def test_request_accepts_multiple_seeds_and_selects_zero_cost_strategy():
    request = ScrapeRequest(urls=['https://one.test/', 'https://two.test/'], strategy='semantic', max_sites=2)
    assert request.initial_urls == ['https://one.test/', 'https://two.test/']


def test_nested_product_scopes_do_not_mix_fields(tmp_path, monkeypatch):
    result = run_html(tmp_path, '''<main><section itemscope itemtype="https://schema.org/Product">
      <h1 itemprop="name">Parent</h1><div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
      <meta itemprop="price" content="12"></div><section itemscope itemtype="https://schema.org/Product">
      <h2 itemprop="name">Related</h2><meta itemprop="price" content="30"></section></section></main>''', monkeypatch)
    products = [entity for entity in result['entities'] if entity['role'] == 'product']
    assert texts(products[0], 'name') == ['Parent']
    assert texts(products[0], 'price') == ['12']
    assert texts(products[1], 'name') == ['Related']


def test_graph_references_resolve_offer_without_using_reference_as_price(tmp_path, monkeypatch):
    result = run_html(tmp_path, '''<script type="application/ld+json">{"@context":"https://schema.org", "@graph":[
      {"@type":"Product","name":"Referenced","offers":{"@id":"#offer"}},
      {"@id":"#offer","@type":"Offer","price":"19.99","priceCurrency":"EUR"}
    ]}</script>''', monkeypatch)
    product = next(entity for entity in result['entities'] if entity['role'] == 'product')
    assert texts(product, 'price') == ['19.99']
    price = next(part for part in product['parts'] if part['class'] == 'price')
    assert price['values'][0]['currency'] == 'EUR'


def test_foreign_vocabularies_and_article_cards_do_not_imply_page_kind(tmp_path, monkeypatch):
    result = run_html(tmp_path, '''<script type="application/ld+json">{"@context":"https://foreign.test/vocab", "@type":"Product","name":"Wrong"}</script>
      <main><div vocab="https://foreign.test/vocab" typeof="Product"><span property="name">Foreign</span></div>
      <article><h2>Card</h2></article><article><h2>Second</h2></article></main>''', monkeypatch)
    assert result['page_type'] == 'other'
    assert not any(entity['role'] == 'product' for entity in result['entities'])


def test_native_visibility_and_original_selectors_survive_cleaning(tmp_path):
    from playwright.sync_api import sync_playwright
    @contextmanager
    def loader(uri):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
                  <main><h1 hidden>Removed</h1><h1>Visible</h1><article style="display:none"><h2>Hidden card</h2></article>
                  <table><tr><th>Name</th><th>Value</th></tr><tr><td>A</td><td>2</td></tr></table></main>'''))
                page.goto(uri)
                yield page
            finally:
                browser.close()
    crawler = Scrapper(ScrapperConfig(strategy=SemanticMetadataStrategy(), page_loader=loader,
        audit_dir=tmp_path, max_sites=1, max_hops=0))
    summary = crawler.run(['https://neutral.test/'])
    result = crawler.audit.get_visit(crawler.audit.list_visits(summary.run_id)[0]['id'])['result']
    assert not any(entity['role'] == 'semantic_article' for entity in result['entities'])
    main = next(entity for entity in result['entities'] if entity['role'] == 'semantic_content')
    assert texts(main, 'name') == ['Visible']
    assert main['parts'][0]['values'][0]['selector'] == 'body > main > h1:nth-of-type(2)'
    assert main['parts'][0]['values'][0]['visible'] is True
    table = next(entity for entity in result['entities'] if entity['role'] == 'semantic_table')
    assert table['attributes']['rows'][1]['cells'] == ['A', '2']


def test_rdfa_declared_prefix_and_named_image_objects(tmp_path, monkeypatch):
    result = run_html(tmp_path, '''<section prefix="s: https://schema.org/" typeof="s:Product">
      <h1 property="s:name">Prefix product</h1><meta property="s:price" content="5.5"></section>
      <script type="application/ld+json">{"@context":"https://schema.org", "@type":"Product",
        "name":"Metadata product","image":{"@type":"ImageObject","name":"Caption","url":"/actual.jpg"}}</script>''', monkeypatch)
    dom = next(entity for entity in result['entities'] if entity['attributes']['source'] == 'semantic_dom' and entity['role'] == 'product')
    assert texts(dom, 'name') == ['Prefix product'] and texts(dom, 'price') == ['5.5']
    metadata = next(entity for entity in result['entities'] if entity['attributes']['source'] == 'jsonld' and entity['role'] == 'product')
    assert texts(metadata, 'image') == ['/actual.jpg']


def test_review_scores_identifiers_and_review_counts_remain_distinct(tmp_path, monkeypatch):
    result = run_html(tmp_path, '''<script type="application/ld+json">{
      "@context":"https://schema.org","@type":"Product","name":"Example","sku":"sku-17",
      "productID":"prod-17","aggregateRating":{"@type":"AggregateRating","ratingValue":4.5,"ratingCount":30,"reviewCount":12},
      "review":[{"@type":"Review","reviewBody":"Useful","reviewRating":{"@type":"Rating","ratingValue":4}}]
    }</script>''', monkeypatch)
    product = next(e for e in result['entities'] if e['role'] == 'product')
    assert texts(product, 'sku') == ['sku-17']
    assert texts(product, 'rating_count') == ['30']
    assert texts(product, 'review_count') == ['12']
    review = next(e for e in result['entities'] if texts(e, 'review_body'))
    assert texts(review, 'rating') == ['4']


def test_list_cards_and_search_results_are_not_mislabeled_as_single_product(tmp_path, monkeypatch):
    result = run_html(tmp_path, '''<script type="application/ld+json">{
      "@context":"https://schema.org","@graph":[{"@type":"SearchResultsPage"},
      {"@type":"Product","name":"Only structured item"}]}</script>
      <main><ol><li><h3><a href="/a">First record</a></h3><p>First details</p></li>
      <li><h3><a href="/b">Second record</a></h3><p>Second details</p></li></ol>
      <aside><ul><li><h3><a href="/filter">Filter</a></h3></li></ul></aside></main>''', monkeypatch)
    assert result['page_type'] == 'listing'
    cards = [e for e in result['entities'] if e['attributes'].get('list_member')]
    assert len(cards) == 2
    assert texts(cards[0], 'name') == ['First record']
    assert 'First details' in cards[0]['text']
    assert cards[1]['links'][0]['url'] == 'https://unrelated.test/b'
