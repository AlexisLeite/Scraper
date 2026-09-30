import base64
import time

from fastapi.testclient import TestClient

from src.common.scrapper import (AuditStore, PageCapture, Scrapper, ScrapperConfig,
                                 WebPageEntities, WebPageEntity)
from src.server import main as server


def result(uri, next_uris=()):
    content = WebPageEntity('content', 'attributes', ('content',), 0.9, ('content',),
                            '#content', 'main', '', uri)
    navigation = WebPageEntity('navigation', 'categories', ('navigation',), 0.9,
                               ('navigation',), '#navigation', 'main', '', uri,
                               tuple({'url': link, 'text': link} for link in next_uris))
    return WebPageEntities('listing', 0.9, 'Jev', ('page:text',), (content, navigation))


def test_scrape_api_persists_discovery_capture_and_jev_results(tmp_path, monkeypatch):
    root, child = 'https://shop.test/', 'https://shop.test/child'
    history = AuditStore(tmp_path)
    monkeypatch.setattr(server, '_audit_store', history)
    real_scrapper = Scrapper

    def fake_scrapper(config, audit):
        def capture(uri):
            return PageCapture(uri, uri, uri, f'<html><body>{uri}</body></html>',
                               base64.b64encode(b'jpeg-bytes').decode())
        def classify(page):
            return result(page.final_uri, next_uris=(child,) if page.final_uri == root else ())
        return real_scrapper(ScrapperConfig(max_threads=config.max_threads,
                                           max_hops=config.max_hops, max_sites=config.max_sites,
                                           max_retries=0, audit_dir=tmp_path,
                                           uri_to_html=capture, html_to_entities=classify), audit=audit)

    monkeypatch.setattr(server, 'Scrapper', fake_scrapper)
    client = TestClient(server.app)
    assert 'id="page-scrape"' in client.get('/').text
    assert client.get('/scrape.js').status_code == 200
    assert client.post('/api/scrapes', json={'url': 'file:///etc/passwd'}).status_code == 400
    assert client.post('/api/scrapes', json={'url': root, 'max_sites': 101}).status_code == 422
    response = client.post('/api/scrapes', json={'url': root, 'max_sites': 2, 'max_hops': 1})
    assert response.status_code == 202
    job_id = response.json()['job_id']
    for _ in range(100):
        job = client.get(f'/api/scrapes/jobs/{job_id}').json()
        if job['status'] != 'running':
            break
        time.sleep(0.02)
    assert job['status'] == 'done'
    run_id = job['run_id']
    assert job['summary']['succeeded'] == 2
    assert client.get('/api/scrapes').json()[0]['id'] == run_id
    run = client.get(f'/api/scrapes/{run_id}').json()
    assert run['run']['usage']['calls'] == 0
    assert run['run']['usage']['exact']
    visits = run['visits']
    assert [visit['requested_uri'] for visit in visits] == [root, child]
    assert visits[0]['discovery_order'] < visits[1]['discovery_order']
    assert [edge['target_uri'] for edge in run['edges']] == [root, child]
    visit_id = visits[0]['id']
    detail = client.get(f'/api/scrapes/{run_id}/visits/{visit_id}').json()
    assert detail['usage']['calls'] == 0
    assert detail['result']['model'] == 'Jev'
    assert detail['result']['entities'][0]['role'] == 'attributes'
    assert client.get(f'/api/scrapes/{run_id}/visits/{visit_id}/screenshot').content == b'jpeg-bytes'
    assert root in client.get(f'/api/scrapes/{run_id}/visits/{visit_id}/html').text
    assert client.get(f'/api/scrapes/wrong/visits/{visit_id}').status_code == 404
