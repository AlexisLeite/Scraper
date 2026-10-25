import json
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.common import scrapper
from src.persistent.store import Settings, Store
from src.persistent.api import router
from src.common.scrapper import AuditStore
from src.server.main import app, ScrapeRequest


def test_semantic_is_the_only_registered_extractor():
    assert not hasattr(scrapper,'JevStrategy')
    assert not hasattr(scrapper,'JevHtmlToWebPageEntities')
    assert ScrapeRequest(url='https://example.org/').strategy=='semantic'
    with pytest.raises(ValidationError):ScrapeRequest(url='https://example.org/',strategy='jev')
    with pytest.raises(ValidationError):Settings(strategy='jev')
    client=TestClient(app)
    assert client.get('/api/health').json()['extractors']==['semantic']
    for path in ['/api/analyze','/api/analyze-html','/api/capture','/api/classify','/api/generate-schema']:
        assert client.post(path,json={}).status_code==410
    assert client.post('/api/scrapes',json={'url':'https://example.org/','strategy':'jev'}).status_code==422


def test_legacy_configuration_and_queued_tasks_switch_to_semantic(tmp_path):
    store=Store(tmp_path)
    settings={**store.settings(),'enabled':True,'strategy':'jev'}
    with store.db() as db:db.execute('UPDATE config SET body=? WHERE id=1',(json.dumps(settings),))
    assert store.settings()['strategy']=='semantic'
    seed=store.save_seed({'url':'https://example.org/','allowed_domains':['example.org']})
    with store.db() as db:db.execute("UPDATE activities SET strategy='jev'")
    activity=store.claim(1)
    assert store.activity(activity)['strategy']=='semantic'
    # Completed historical runs are not rewritten by this normalization.
