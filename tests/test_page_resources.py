from contextlib import contextmanager
from types import SimpleNamespace

from src.common.scrapper.page_urls import is_page_url
from src.common.scrapper.strategies.discovery import discover_links
from src.common.scrapper.models import PageCapture
from src.common.scrapper import AuditStore, Scrapper, ScrapperConfig, SemanticMetadataStrategy
from src.persistent.store import Store
from src.persistent.worker import execute_activity
from src.common.uri_to_html import NonPageResponse


def test_page_links_exclude_resource_hints_downloads_and_versioned_scripts():
    urls=[]
    html='''<link rel="preload" as="script" href="/assets/js/chunk-vendors.js?ver=1.61.0-release00000003">
    <link rel="stylesheet" href="/style"><link rel="modulepreload" href="/module">
    <link rel="alternate" type="application/rss+xml" href="/feed"><link rel="next" href="/page/2">
    <a href="/products?variant=2">Product</a><a href="/assets/app.JS?v=1">JS</a>
    <a href="/manual.pdf">PDF</a><a download href="/export">Download</a><area href="/map-page">'''
    discover_links(PageCapture('https://example.org/','https://example.org/','Test',html),SimpleNamespace(enqueue=lambda url,**kwargs:urls.append(url)))
    assert urls==['https://example.org/page/2','https://example.org/products?variant=2','https://example.org/map-page']
    assert not is_page_url('https://www.vichy.com.uy/assets/js/chunk-vendors.js?ver=1.61.0-release00000003')
    assert not is_page_url('https://example.org/app%2Ejs?x=1')
    assert is_page_url('https://example.org/products.php?ref=app.js')


def test_resource_urls_never_enter_persistent_queue(tmp_path):
    store=Store(tmp_path)
    seed=store.save_seed({'url':'https://example.org/','allowed_domains':['example.org']})
    assert store.discover(seed['id'],'https://example.org/script.js?v=2',1) is None
    assert store.pages('example.org')['total']==1


def test_non_html_response_is_excluded_without_error_retry(tmp_path):
    store=Store(tmp_path/'service')
    store.save_settings({**store.settings(),'enabled':True})
    store.save_seed({'url':'https://example.org/unknown','allowed_domains':['example.org']})
    activity=store.claim(1)
    @contextmanager
    def loader(url):
        raise NonPageResponse('Recurso excluido: application/javascript')
        yield
    execute_activity(store,activity,AuditStore(tmp_path/'audit'),loader)
    result=store.activity(activity)
    assert result['status']=='excluded'
    assert result['errors']==[]
    assert result['attempt']==1
