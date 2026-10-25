from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from src.common.scrapper import AuditStore, PageCapture, Scrapper, ScrapperConfig, SemanticMetadataStrategy


@pytest.fixture
def dynamic_site():
    requests=[]
    html='''<html><head><title>Neutral catalogue</title></head><body>
    <header><nav><ul><li><button aria-expanded="false" onmouseenter="
    this.setAttribute('aria-expanded','true');this.nextElementSibling.innerHTML=
    '<ul><li><button aria-expanded=false>Group Alpha</button><ul><li><a href=/alpha>Alpha destination</a></li></ul></li><li><button aria-expanded=false>Group Beta</button><ul><li><a href=/beta>Beta destination</a></li></ul></li></ul>'">Explore</button><div></div></li>
    <li><a aria-haspopup="menu" href="/never" onmouseenter="location.href='/never'">Unsafe hover</a></li>
    </ul></nav></header><main>
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"ItemList","itemListElement":[{"@type":"ListItem","position":1,"item":{"@id":"/one"}},{"@type":"ListItem","position":2,"url":"/two"}]}</script>
    <section><div><a href="/one"><img src="/one.png"><h2>First item</h2></a><p>First description</p></div>
    <div><a href="/two"><img src="/two.png"><h2>Second item</h2></a><p>Second description</p></div></section>
    </main></body></html>'''
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body=html.encode() if self.path=='/' else b'not followed'
            self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    Thread(target=server.serve_forever,daemon=True).start()
    try:yield f'http://127.0.0.1:{server.server_port}/',requests
    finally:server.shutdown();server.server_close()


def test_hover_extracts_hierarchy_and_items_without_navigation(tmp_path,dynamic_site):
    uri,requests=dynamic_site
    scraper=Scrapper(ScrapperConfig(max_sites=1,max_hops=0,max_retries=0,strategy=SemanticMetadataStrategy(follow_navigation=False)),audit=AuditStore(tmp_path))
    summary=scraper.run([uri])
    assert (summary.scheduled,summary.succeeded,summary.failed)==(1,1,0)
    visits=scraper.audit.list_visits(summary.run_id)
    detail=scraper.audit.get_visit(visits[0]['id']);result=detail['result']
    items=[e for e in result['entities'] if e['role']=='semantic_item']
    assert len(items)==2
    assert {e['parts'][0]['values'][0]['text'] for e in items}=={'First item','Second item'}
    assert all(e['attributes']['dom_html'] and e['links'] for e in items)
    paths=[e['attributes']['navigation_path'] for e in result['entities'] if e['role']=='categories']
    assert ['Explore','Group Alpha'] in paths and ['Explore','Group Beta'] in paths
    assert '/alpha' not in requests and '/beta' not in requests and '/one' not in requests and '/two' not in requests and '/never' not in requests
    report=result['classification']['coverage']['navigation_observation']
    assert report['blocked_navigations'] and not report['complete']
    assert all(a['method']=='hover' for a in result['actions'])
    action=next(a for a in result['actions'] if a['status']=='observed')
    assert action['evidence']['before']['html_path'] and action['evidence']['after']['html_path']
    from bs4 import BeautifulSoup
    original=BeautifulSoup(scraper.audit.read_html(detail['id']),'html.parser')
    assert not any(b.get_text(strip=True)=='Group Alpha' for b in original.select('button'))
    assert scraper.audit.list_edges(summary.run_id)[0]['target_uri']==uri


def test_static_input_reports_unobserved_menus_and_preserves_collection(tmp_path):
    capture=PageCapture('https://neutral.test/','https://neutral.test/','Static','''<main>
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"ItemList","itemListElement":[{"@type":"ListItem","item":{"@type":"Product","url":"/x","name":"Declared item"}}]}</script>
    <div><a href="/x"><h2>Visible item</h2></a><p>Detail</p></div></main>''')
    @contextmanager
    def loader(uri):yield capture
    scraper=Scrapper(ScrapperConfig(max_sites=1,max_hops=0,page_loader=loader,strategy=SemanticMetadataStrategy()),audit=AuditStore(tmp_path))
    summary=scraper.run([capture.requested_uri]);detail=scraper.audit.get_visit(scraper.audit.list_visits(summary.run_id)[0]['id'])
    assert detail['result']['classification']['coverage']['navigation_observation']['supported'] is False
    assert detail['result']['classification']['coverage']['navigation_observation']['complete'] is False
    assert detail['result']['classification']['value']=='listing'
    assert scraper.audit.read_html(detail['id'])==capture.html
