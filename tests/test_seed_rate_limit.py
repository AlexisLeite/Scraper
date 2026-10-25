from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from src.persistent.store import Seed, Store


def origin(store, host, limit=None):
    return store.save_seed(dict(url=f'https://{host}/', allowed_domains=[host], max_urls_per_minute=limit))


def ready(tmp_path):
    store = Store(tmp_path)
    store.save_settings({**store.settings(), 'enabled': True, 'threads': 50, 'retry_delay': 1})
    return store


def test_atomic_window_survives_restart_and_releases_at_boundary(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr('src.persistent.store.time.time', lambda: now[0])
    store = ready(tmp_path)
    seed = origin(store, 'example.org', 3)
    for i in range(12):
        store.discover(seed['id'], f'https://example.org/{i}', 1)
    with ThreadPoolExecutor(max_workers=10) as pool:
        claims = list(pool.map(store.claim, range(1, 11)))
    assert len([a for a in claims if a]) == 1
    assert not Store(tmp_path).has_pending_work()
    assert Store(tmp_path).claim(20) is None
    now[0] = 1020.0
    assert Store(tmp_path).claim(21)
    now[0] = 1040.0
    assert Store(tmp_path).claim(22)
    assert store.dashboard()['counts']['queued'] == 10
    now[0] = 1059.999
    assert store.claim(20) is None
    now[0] = 1060.0
    assert Store(tmp_path).has_pending_work()
    assert Store(tmp_path).claim(20)


def test_retries_consume_quota_and_other_origins_continue(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr('src.persistent.store.time.time', lambda: now[0])
    store = ready(tmp_path)
    origin(store, 'limited.org', 1)
    activity = store.claim(1)
    store.finish(activity, 'timeout')
    now[0] = 1002.0
    assert store.claim(1) is None
    other = origin(store, 'other.org', 1)
    other_activity = store.claim(2)
    assert store.activity(other_activity)['seed_id'] == other['id']
    now[0] = 1060.0
    assert store.claim(1) == activity
    store.finish(activity, 'timeout')
    now[0] = 1062.0
    assert store.claim(1) is None
    assert store.activity(activity)['attempt'] == 2


def test_shared_page_cannot_bypass_limit_through_unlimited_origin(tmp_path):
    store = ready(tmp_path)
    seed = origin(store, 'example.org', 1)
    activity = store.claim(1)
    store.finish(activity)
    other = store.save_seed(dict(url='https://example.org/other', allowed_domains=['example.org']))
    assert store.claim(1) is None
    assert not store.has_pending_work()
    body = {k:v for k,v in seed.items() if k != 'id'}
    store.save_seed({**body, 'max_urls_per_minute': None}, seed['id'])
    assert store.has_pending_work()
    assert store.claim(1)
    assert store.claim(2) is None  # shared extraction remains deduplicated


@pytest.mark.parametrize('value', [0, -1, 1.5, True, '3', 60001])
def test_invalid_limit(value):
    with pytest.raises(ValidationError):
        Seed(url='https://example.org', max_urls_per_minute=value)


def test_legacy_unlimited_and_saved_limit(tmp_path):
    assert Seed(url='https://example.org').max_urls_per_minute is None
    store = ready(tmp_path)
    seed = origin(store, 'example.org', 5)
    assert Store(tmp_path).seeds()[0]['max_urls_per_minute'] == 5
    assert Store(tmp_path).seed_page()['items'][0]['max_urls_per_minute'] == 5
