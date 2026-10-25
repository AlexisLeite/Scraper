import time

from fastapi.testclient import TestClient

from src.common.scrapper import AuditStore, PageCapture, Scrapper, ScrapperConfig
from src.persistent.store import Store
from src.server import main as server


def test_extract_visits_only_requested_page_and_keeps_persistent_queue_unchanged(tmp_path, monkeypatch):
    history = AuditStore(tmp_path / 'audit')
    store = Store(tmp_path / 'service')
    before = store.dashboard()['counts']
    visited = []
    configs = []

    def capture(uri):
        visited.append(uri)
        return PageCapture(uri, uri, 'Página aislada', '''<html><head><title>Página aislada</title></head>
            <body><main><h1>Contenido de prueba</h1><p>Texto extraído de una sola página.</p>
            <a href="https://other.example/outside">Otra página</a>
            <a href="/next">Página siguiente</a></main></body></html>''', None)

    def scraper(config, audit):
        configs.append(config)
        return Scrapper(ScrapperConfig(max_threads=config.max_threads,
            max_sites=config.max_sites, max_hops=config.max_hops, max_retries=0,
            strategy=config.strategy, uri_to_html=capture), audit=audit)

    monkeypatch.setattr(server, '_audit_store', history)
    monkeypatch.setattr(server, 'Scrapper', scraper)
    with TestClient(server.app) as client:
        response = client.post('/api/extract', json={'url': 'https://example.org/only'})
        assert response.status_code == 202
        job_id = response.json()['job_id']
        for _ in range(150):
            job = client.get('/api/scrapes/jobs/' + job_id).json()
            if job['status'] != 'running':
                break
            time.sleep(.02)
        assert job['status'] == 'done'
        assert job['summary']['succeeded'] == 1
        assert visited == ['https://example.org/only']
        assert configs[0].max_sites == 1 and configs[0].max_hops == 0 and configs[0].max_threads == 1
        run = client.get('/api/scrapes/' + job['run_id']).json()
        assert len(run['visits']) == 1
        visit_id = run['visits'][0]['id']
        base = f'/api/scrapes/{job["run_id"]}/visits/{visit_id}'
        visit = client.get(base).json()
        assert visit['result']['entities']
        assert 'Texto extraído' in client.get(base + '/html').text
        assert store.dashboard()['counts'] == before
        assert store.seeds() == []


def test_extract_rejects_non_web_urls_and_traversal_overrides():
    with TestClient(server.app) as client:
        assert client.post('/api/extract', json={'url': 'file:///etc/passwd'}).status_code == 400
        for key, value in [('max_sites', 5), ('max_hops', 2), ('urls', ['https://other.org/'])]:
            response = client.post('/api/extract', json={'url': 'https://example.org/', key: value})
            assert response.status_code == 422
