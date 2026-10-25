"""Run with SCRAPER_TEST_DSN pointing to a disposable PostgreSQL database."""
import os
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
import psycopg
from src.persistent.store import Store,Settings
from src.common.scrapper import AuditStore
from src.common.scrapper.models import PageCapture
from src.storage.subscriptions import Subscriptions


@pytest.fixture
def repositories(tmp_path):
    dsn=os.environ.get('SCRAPER_TEST_DSN')
    if not dsn:pytest.skip('Requires isolated PostgreSQL test database')
    with psycopg.connect(dsn) as db:
        db.execute(Path('deploy/postgres.sql').read_text())
        db.execute('TRUNCATE service.config,service.seeds,service.domains,service.pages,service.discoveries,service.activities,service.activity_runs,service.stages,service.failure_events,service.heartbeat,audit.runs,audit.visits,audit.edges,audit.events,audit.attempts,audit.model_calls,audit.strategy_expenses,audit.artifacts,public.subscribers,public.contents,public.subscriber_contents RESTART IDENTITY CASCADE')
    store=Store(tmp_path/'persistent',dsn=dsn);audit=AuditStore(tmp_path/'audit',dsn=dsn)
    with store.db() as db:db.execute('INSERT INTO config VALUES(1,?)',(Settings(enabled=True).model_dump_json(),))
    return store,audit,Subscriptions(dsn)


def source(store,audit):
    seed=store.save_seed({'url':'https://example.org/','allowed_domains':['example.org']})
    activity=store.claim(1)
    run=audit.start_run({});store.attach_run(activity,run)
    visit=audit.start_visit(run,seed['url'],seed['url'],0,None,None,'')
    return seed,activity,run,visit


def publish(audit,run,visit):
    from datetime import datetime,timezone
    id=uuid.uuid4().hex
    with audit._connect() as db:
        db.execute('INSERT INTO events(id,run_id,visit_id,entity_id,payload_json,created_at) VALUES(?,?,?,?,?,?)',(id,run,visit,'entity','{"entity":{"text":"hello"}}',datetime.now(timezone.utc).isoformat()))
    return id


def test_pg_artifacts_history_and_producer(repositories):
    store,audit,registry=repositories
    seed,activity,run,visit=source(store,audit)
    audit.save_capture(visit,1,PageCapture(seed['url'],seed['url'],'Title','<p>Hello</p>',None))
    assert audit.read_html(visit)=='<p>Hello</p>'
    assert not list(audit.captures.iterdir())
    first=publish(audit,run,visit)
    subscriber=registry.create_subscriber('Consumer')
    second=publish(audit,run,visit)
    contents=registry.contents(subscriber_id=subscriber['id'])
    assert {r['id'] for r in contents['items']}=={first,second}
    assert all(r['delivered'] is False and r['delivered_at'] is None for r in contents['items'])
    store.finish(activity)
    store.clear_seed_history(seed['id'],audit)
    assert registry.contents()['total']==0
    assert registry.subscribers()[0]['pending']==0
    with audit._connect() as db:assert db.execute('SELECT count(*) FROM artifacts').fetchone()[0]==0


def test_pg_subscriber_content_race_and_rollback(repositories):
    store,audit,registry=repositories
    _,_,run,visit=source(store,audit)
    barrier=Barrier(12)
    def insert(i):
        barrier.wait()
        return registry.create_subscriber('Consumer '+str(i)) if i%2 else publish(audit,run,visit)
    with ThreadPoolExecutor(max_workers=12) as pool:list(pool.map(insert,range(12)))
    assert len(registry.subscribers())==6
    assert all(row['pending']==6 for row in registry.subscribers())
    with pytest.raises(RuntimeError):
        with audit._connect() as db:
            db.execute('INSERT INTO public.subscribers(id,name) VALUES(?,?)',('rollback','Rolled back'))
            raise RuntimeError('rollback')
    assert len(registry.subscribers())==6


def test_pg_queue_concurrent_claims(repositories):
    store,audit,_=repositories
    seed=store.save_seed({'url':'https://example.org/','allowed_domains':['example.org']})
    store.save_settings({**store.settings(),'threads':50})
    for i in range(10):store.discover(seed['id'],f'https://example.org/{i}',1)
    with ThreadPoolExecutor(max_workers=20) as pool:claimed=list(pool.map(store.claim,range(1,21)))
    assert len(set(filter(None,claimed)))==11
    assert len(list(filter(None,claimed)))==11


def test_pg_real_direct_browser_extraction(repositories,tmp_path):
    from contextlib import contextmanager
    from src.persistent.browser_pool import BrowserPool
    from src.persistent.browser_connection import SharedBrowserConnection
    from src.persistent.worker import execute_activity
    store,audit,registry=repositories
    seed=store.save_seed({'url':'https://example.org/','allowed_domains':['example.org']})
    activity=store.claim(1)
    pool=BrowserPool(tmp_path/'browsers')
    try:
        env=pool.allocate(1,activity)
        @contextmanager
        def loader(url):
            with SharedBrowserConnection(env['SCRAPER_BROWSER_ENDPOINT']) as browser:
                page=browser.new_page()
                page.route('**/*',lambda route:route.fulfill(content_type='text/html',body='<title>PG regression</title><main><article><h1>Content</h1><p>Stored in PostgreSQL</p></article><details><summary>Question</summary><p>Hidden answer</p></details><a href="/next">Next</a></main>'))
                page.goto(url)
                yield page
        execute_activity(store,activity,audit=audit,loader=loader)
        final=store.activity(activity)
        assert final['status']=='done'
        visit=audit.list_visits(final['run_id'])[0]
        assert 'Hidden answer' in audit.read_html(visit['id'])
        assert audit.read_screenshot(visit['id']).startswith(b'\xff\xd8')
        assert registry.contents()['total']>0
        assert store.pages(seed_id=seed['id'])['total']==2
    finally:pool.close()


def test_pg_seed_rate_limit_is_atomic_and_persistent(repositories):
    store, _, _ = repositories
    seed = store.save_seed({'url': 'https://rate.example/', 'allowed_domains': ['rate.example'], 'max_urls_per_minute': 2})
    store.save_settings({**store.settings(), 'threads': 50})
    for i in range(8):
        store.discover(seed['id'], f'https://rate.example/{i}', 1)
    with ThreadPoolExecutor(max_workers=10) as pool:
        claimed = list(pool.map(store.claim, range(1, 11)))
    assert len([a for a in claimed if a]) == 1
    reopened = Store(store.root, dsn=store.dsn)
    assert reopened.claim(20) is None
    assert not reopened.has_pending_work()
    with store.db() as db:
        db.execute("UPDATE stages SET at=at-31 WHERE stage='starting'")
    assert reopened.claim(21)
    assert reopened.claim(22) is None
    with store.db() as db:
        db.execute("UPDATE stages SET at=at-61 WHERE stage='starting'")
    assert reopened.has_pending_work()
    assert reopened.claim(20)
