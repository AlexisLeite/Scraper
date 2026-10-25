"""High concurrency and URL policy regression without external/model requests."""
from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.common.scrapper import AuditStore, PageCapture, Scrapper, ScrapperConfig, StrategyExpense
from src.common.scrapper.url_policy import URLPolicy
from src.server import main as server


class EmptyStrategy:
    def process(self,page,api):
        api.report_expense(StrategyExpense(0,provider='local'))


def capture(uri):
    return PageCapture(uri,uri,'Fixture','<main>Fixture</main>')


def test_fifty_workers_share_budget_without_mixing_visits_or_expenses(tmp_path):
    barrier=threading.Barrier(50,timeout=20)
    lock=threading.Lock()
    owners={},{}
    threads=set()
    peak=active=0

    @contextmanager
    def loader(uri):
        owner=threading.get_ident()
        with lock:
            owners[0][uri]=owner
        yield capture(uri)
        with lock:
            owners[1][uri]=threading.get_ident()

    class ConcurrentStrategy:
        def process(self,page,api):
            nonlocal active,peak
            with lock:
                active+=1;peak=max(peak,active);threads.add(threading.get_ident())
            try:
                api.report_expense(StrategyExpense(0,provider='local',usage={'uri':api.uri}))
                barrier.wait()
                api.emit_content({'uri':api.uri,'depth':api.depth})
                if api.consume_hop(key='expansion'):
                    api.report_expense(StrategyExpense(0,provider='local',usage={'uri':api.uri}))
                    api.emit_content({'uri':api.uri,'depth':api.depth})
            finally:
                with lock:
                    active-=1

    audit=AuditStore(tmp_path)
    summary=Scrapper(ScrapperConfig(max_threads=50,max_sites=60,max_hops=1,max_retries=0,
        strategy=ConcurrentStrategy(),page_loader=loader),audit=audit).run(
            [f'https://fixture.test/{i}' for i in range(50)])
    assert peak==len(threads)==50
    assert owners[0]==owners[1] and len(owners[0])==50
    assert summary.scheduled==summary.succeeded==summary.content_events==60
    assert summary.failed==0
    visits=audit.list_visits(summary.run_id)
    assert sum(v['kind']=='expansion' for v in visits)==10
    for visit in visits:
        detail=audit.get_visit(visit['id'])
        assert detail['expenses'][0]['usage']['uri']==visit['requested_uri']
        assert json.loads(detail['events'][0]['payload_json'])['entity']['uri']==visit['requested_uri']
    costs=audit.get_run(summary.run_id)['costs']
    assert costs['exact'] and costs['reports']==60 and costs['amount_usd']=='0'


def test_available_threads_continue_when_first_capture_is_slow(tmp_path):
    fast_done=threading.Event()
    lock=threading.Lock()
    finished=0
    def loader(uri):
        nonlocal finished
        if uri.endswith('/slow'):
            assert fast_done.wait(5),'El primer trabajo bloqueó la reposición de hilos.'
        else:
            with lock:
                finished+=1
                if finished==7:fast_done.set()
        return capture(uri)
    summary=Scrapper(ScrapperConfig(max_threads=3,max_sites=8,max_hops=0,max_retries=0,
        strategy=EmptyStrategy(),uri_to_html=loader,audit_dir=tmp_path)).run(
            ['https://fixture.test/slow',*[f'https://fixture.test/{i}' for i in range(7)]])
    assert summary.succeeded==8 and summary.failed==0


@pytest.mark.parametrize('rules,uri,expected',[
    ({},'https://any.test/path',None),
    ({'allowed_domains':['example.org']},'https://shop.example.org/a',None),
    ({'allowed_domains':['example.org']},'https://example.org.evil.test/a','outside_allowlist'),
    ({'allowed_domains':['example.org']},'https://notexample.org/a','outside_allowlist'),
    ({'allowed_domains':['EXAMPLE.ORG.']},'https://example.org/',None),
    ({'allowed_domains':['*.example.org']},'https://example.org/',None),
    ({'allowed_domains':['mañana.test']},'https://xn--maana-pta.test/',None),
    ({'allowed_domains':['shop.test'],'allowed_url_patterns':[r'/public/']},'https://other.test/public/a','outside_allowlist'),
    ({'allowed_url_patterns':[r'/catalog/']},'https://shop.test/login','outside_allowlist'),
    ({'allowed_domains':['shop.test'],'blocked_domains':['private.shop.test']},'https://private.shop.test/','blocked_url'),
    ({'allowed_url_patterns':['.*'],'blocked_url_patterns':[r'[?&]private=1']},'https://shop.test/a?private=1','blocked_url'),
    ({'blocked_domains':['shop.test']},'https://sub.shop.test/a','blocked_url'),
])
def test_url_rules_are_generic_and_blacklist_has_priority(rules,uri,expected):
    assert URLPolicy(**rules).disposition(uri)==expected


def test_filters_skip_seeds_and_links_before_consuming_visits(tmp_path):
    captured=[]
    class Links(EmptyStrategy):
        def process(self,page,api):
            super().process(page,api)
            for uri in ('https://shop.test/ok','https://shop.test/login','https://external.test/other'):
                api.enqueue(uri)
    def load(uri):
        captured.append(uri)
        return capture(uri)
    audit=AuditStore(tmp_path)
    summary=Scrapper(ScrapperConfig(max_threads=3,max_sites=10,max_hops=1,max_retries=0,
        strategy=Links(),uri_to_html=load,allowed_domains=('shop.test',),
        blocked_url_patterns=(r'/login$',)),audit=audit).run(
            ['https://shop.test/start','https://shop.test/login','https://external.test/seed'])
    assert captured==['https://shop.test/start','https://shop.test/ok']
    assert summary.scheduled==summary.succeeded==2
    dispositions={e['disposition'] for e in audit.list_edges(summary.run_id)}
    assert {'blocked_url','outside_allowlist','scheduled'}<=dispositions
    assert audit.get_run(summary.run_id)['config']['allowed_domains']==['shop.test']


@pytest.mark.parametrize('value',[0,51,1.5])
def test_thread_limit_is_validated_in_core_and_api(value):
    with pytest.raises(ValueError):ScrapperConfig(max_threads=value)
    response=TestClient(server.app).post('/api/scrapes',json={'url':'https://fixture.test/','max_threads':value})
    assert response.status_code==422


@pytest.mark.parametrize('rules',[{'allowed_url_patterns':['[']},{'blocked_url_patterns':['']},{'allowed_domains':['https://shop.test/a']}])
def test_invalid_rules_are_rejected_before_starting_a_crawl(rules):
    with pytest.raises(ValueError):ScrapperConfig(**rules)
    response=TestClient(server.app).post('/api/scrapes',json={'url':'https://fixture.test/',**rules})
    assert response.status_code==422


def test_api_passes_thread_count_and_url_rules_to_scraper(monkeypatch):
    seen=[]
    class Stub:
        def __init__(self,config,audit):seen.append(config)
        def run(self,*args,**kwargs):return SimpleNamespace(run_id='fixture')
    monkeypatch.setattr(server,'Scrapper',Stub)
    monkeypatch.setattr(server,'_scrape_jobs',{'test-thread-config':{}})
    server._run_scrape('test-thread-config',server.ScrapeRequest(url='https://shop.test/',max_threads=50,
        allowed_domains=['shop.test'],allowed_url_patterns=[r'/products/']))
    assert seen[0].max_threads==50
    assert seen[0].allowed_domains==('shop.test',)
    assert seen[0].allowed_url_patterns==(r'/products/',)


def test_real_playwright_sessions_stay_in_their_worker_threads(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from src.common.uri_to_html import loaded_page
    from src.common.scrapper import SemanticMetadataStrategy
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers()
            self.wfile.write(b'<html><head><title>Fixture</title></head><body><main><h1>Fixture</h1></main></body></html>')
        def log_message(self,*args):pass
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    service=threading.Thread(target=http.serve_forever,daemon=True);service.start()
    barrier=threading.Barrier(4,timeout=20)
    owners={}
    closed={}
    class CheckedSemantic(SemanticMetadataStrategy):
        def process(self,page,api):
            assert owners[api.uri]==threading.get_ident()
            assert page.title()=='Fixture'
            barrier.wait()
            super().process(page,api)
    @contextmanager
    def loader(uri):
        owners[uri]=threading.get_ident()
        with loaded_page(uri) as page:yield page
        closed[uri]=threading.get_ident()
    try:
        summary=Scrapper(ScrapperConfig(max_threads=4,max_sites=4,max_hops=0,max_retries=0,
            page_loader=loader,strategy=CheckedSemantic(),audit_dir=tmp_path)).run(
                [f'http://127.0.0.1:{http.server_port}/{i}' for i in range(4)])
        assert summary.succeeded==4 and summary.failed==0
        assert len(set(owners.values()))==4 and owners==closed
    finally:
        http.shutdown();http.server_close();service.join(timeout=2)
