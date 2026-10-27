import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.persistent.store import Store
from src.persistent.api import router
from src.common.scrapper import AuditStore
from src.common.scrapper.models import PageCapture


def exercise_delete(store, audit):
    store.save_settings({**store.settings(), 'enabled': True})
    seed = store.save_seed({'url': 'https://delete.example/', 'allowed_domains': ['delete.example']})
    activity = store.claim(1)
    run = audit.start_run({})
    store.attach_run(activity, run)
    visit = audit.start_visit(run, seed['url'], seed['url'], 0, None, None, '')
    audit.save_capture(visit, 1, PageCapture(seed['url'], seed['url'], 'Title', '<p>Delete</p>', None))
    store.finish(activity)
    other = store.save_seed({'url': 'https://keep.example/', 'allowed_domains': ['keep.example']})
    shared = store.discover(seed['id'], 'https://delete.example/shared', 1)
    with store.db() as db:
        page_id = db.execute('SELECT page_id FROM activities WHERE id=?', (shared,)).fetchone()[0]
        db.execute('INSERT INTO discoveries VALUES(?,?,1)', (other['id'], page_id))
        for origin in (seed, other):
            db.execute('INSERT INTO extraction_checks VALUES(?,?,?)', (origin['id'], 1, '{}'))
    app = FastAPI()
    app.include_router(router(store, audit))
    client = TestClient(app)
    response = client.delete('/api/service/seeds/' + seed['id'])
    assert response.status_code == 200, response.text
    assert response.json() == {'deleted_activities': 1, 'file_errors': []}
    assert [item['id'] for item in store.seeds()] == [other['id']]
    assert store.activity(activity) is None
    assert store.activity(shared)['seed_id'] == other['id']
    assert store.pages(seed_id=other['id'])['total'] == 2
    assert audit.get_run(run) is None
    with store.db() as db:
        assert db.execute('SELECT count(*) FROM discoveries WHERE seed_id=?', (seed['id'],)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM activities WHERE seed_id=?', (seed['id'],)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM extraction_checks WHERE seed_id=?', (seed['id'],)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM extraction_checks WHERE seed_id=?', (other['id'],)).fetchone()[0] == 1
    assert client.get('/api/service/seeds/' + seed['id']).status_code == 404
    assert client.delete('/api/service/seeds/' + seed['id']).status_code == 404


def test_delete_seed_removes_origin_and_exclusive_history(tmp_path):
    exercise_delete(Store(tmp_path/'service'), AuditStore(tmp_path/'audit'))


def test_delete_seed_cancels_active_exclusive_work(tmp_path):
    store = Store(tmp_path/'service')
    store.save_settings({**store.settings(), 'enabled': True})
    seed = store.save_seed({'url': 'https://delete.example/'})
    activity = store.claim(1)
    result = store.delete_seed(seed['id'], AuditStore(tmp_path/'audit'))
    assert result['deleted_activities'] == 1
    assert store.activity(activity) is None
    assert store.seeds() == []


def test_delete_seed_rolls_back_when_cleanup_fails(tmp_path, monkeypatch):
    store = Store(tmp_path/'service')
    seed = store.save_seed({'url': 'https://delete.example/'})
    def fail(*args):
        raise ValueError('Cancellation failed')
    monkeypatch.setattr('src.persistent.cancellation.cancel_workers', fail)
    with pytest.raises(ValueError, match='Cancellation failed'):
        store.delete_seed(seed['id'], AuditStore(tmp_path/'audit'))
    assert store.seeds()[0]['id'] == seed['id']
    assert store.pages(seed_id=seed['id'])['total'] == 1
