from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.persistent.store import Seed, Store
from src.persistent.api import router
from src.common.scrapper import AuditStore


def ready(tmp_path, limit=3):
    store = Store(tmp_path)
    store.save_settings({**store.settings(), 'enabled': True, 'threads': 50})
    seed = store.save_seed(dict(url='https://example.org/', allowed_domains=['example.org'], max_pages=limit))
    for i in range(10):
        store.discover(seed['id'], f'https://example.org/{i}', 1)
    return store, seed


def test_distinct_pages_reserved_atomically_and_persisted(tmp_path):
    store, seed = ready(tmp_path)
    # Discovery does not spend the budget, including the initial URL.
    assert store.seed_page()['items'][0]['max_pages'] == 3
    with ThreadPoolExecutor(max_workers=10) as pool:
        claims = [a for a in pool.map(store.claim, range(1, 11)) if a]
    assert len(claims) == 3
    assert seed['url'] in [store.activity(a)['url'] for a in claims]
    for a in claims:
        store.finish(a)
    assert Store(tmp_path).claim(20) is None
    assert not Store(tmp_path).has_pending_work()
    assert store.dashboard()['counts']['queued'] == 8


def test_retry_and_refresh_reuse_budget_but_other_origin_can_continue(tmp_path):
    store, seed = ready(tmp_path, 1)
    activity = store.claim(1)
    store.finish(activity, 'failed', retryable=False)
    assert store.claim(1) is None
    assert store.discover(seed['id'], seed['url'], 0, manual=True) == activity
    assert store.has_pending_work()
    assert store.claim(1) == activity
    store.finish(activity)
    refreshed = store.discover(seed['id'], seed['url'], 0, manual=True)
    assert refreshed != activity
    assert store.claim(1) == refreshed
    store.finish(refreshed)
    other = store.save_seed(dict(url='https://other.org/', allowed_domains=['other.org']))
    assert store.activity(store.claim(2))['seed_id'] == other['id']
    assert store.claim(3) is None


def test_shared_origin_cannot_bypass_budget_and_raising_or_removing_releases_queue(tmp_path):
    store, seed = ready(tmp_path, 1)
    store.finish(store.claim(1))
    store.save_seed(dict(url='https://example.org/new', allowed_domains=['example.org']))
    assert store.claim(1) is None
    body = {k: v for k, v in seed.items() if k != 'id'}
    store.save_seed({**body, 'max_pages': 2}, seed['id'])
    assert store.has_pending_work()
    store.finish(store.claim(1))
    assert store.claim(1) is None
    store.save_seed({**body, 'max_pages': None}, seed['id'])
    assert store.claim(1)


@pytest.mark.parametrize('value', [0, -1, 1.5, True, '3'])
def test_invalid_page_limit(value):
    with pytest.raises(ValidationError):
        Seed(url='https://example.org/', max_pages=value)


def test_optional_limit_round_trips_through_api(tmp_path):
    store = Store(tmp_path / 'store')
    app = FastAPI()
    app.include_router(router(store, AuditStore(tmp_path / 'audit')))
    with TestClient(app) as client:
        created = client.post('/api/service/seeds', json={'url': 'https://example.org/', 'max_pages': 7})
        assert created.status_code == 201
        seed = created.json()
        assert client.get('/api/service/seeds/' + seed['id']).json()['max_pages'] == 7
        body = {k: v for k, v in seed.items() if k != 'id'}
        assert client.put('/api/service/seeds/' + seed['id'], json={**body, 'max_pages': None}).json()['max_pages'] is None
        assert client.post('/api/service/seeds', json={'url': 'https://other.org/'}).json()['max_pages'] is None
        assert client.post('/api/service/seeds', json={'url': 'https://other.org/', 'max_pages': 0}).status_code == 422
