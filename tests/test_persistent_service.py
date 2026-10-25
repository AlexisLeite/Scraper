import concurrent.futures
import time
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.persistent.store import Store, Seed
from src.persistent.api import router
from src.persistent.worker import execute_activity
from src.common.scrapper import AuditStore
from src.common.scrapper.models import PageCapture


def setup(tmp_path, **settings):
    store = Store(tmp_path/'persistent')
    store.save_settings({**store.settings(),'enabled':True,**settings})
    seed = store.save_seed(dict(url='https://example.org/',allowed_domains=['example.org']))
    return store,seed


def test_atomic_claim_dedup_and_fifty_workers(tmp_path):
    store,seed = setup(tmp_path,threads=50)
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
        list(pool.map(lambda i:store.discover(seed['id'],f'https://example.org/{i%60}?utm_source=x',1),range(120)))
        ids=list(pool.map(store.claim,range(1,51)))
    assert len(set(ids))==50 and None not in ids
    assert store.dashboard()['counts']=={'queued':11,'running':50}
    assert store.claim(51) is None
    reopened=Store(store.root)
    assert len(reopened.dashboard()['workers'])==50
    reopened.recover()
    assert reopened.dashboard()['counts']=={'queued':11,'retry_wait':50}
    assert reopened.claim(1) is not None  # one of the eleven untouched URLs


def test_retries_survive_restart_respect_delay_and_effects(tmp_path):
    store,seed=setup(tmp_path,attempts=2,retry_delay=100)
    activity=store.claim(1)
    store.finish(activity,'timeout')
    assert store.activity(activity)['status']=='retry_wait'
    assert Store(store.root).claim(1) is None
    with store.db() as db: db.execute('UPDATE activities SET due=0')
    assert store.claim(1)==activity
    store.finish(activity,'timeout')
    assert store.activity(activity)['status']=='error'
    manual=store.rescrape(store.activity(activity)['page_id'])
    assert manual==activity and store.claim(1)==manual
    store.effects(manual)
    Store(store.root).recover()
    assert store.activity(manual)['status']=='error'
    assert len(store.activity(manual)['stages'])>=3


def test_domain_trap_pauses_queue_and_caps_growth(tmp_path):
    store,seed=setup(tmp_path,suspicious_pages=3)
    running=store.claim(1)
    store.discover(seed['id'],'https://example.org/a',1)
    store.discover(seed['id'],'https://example.org/b',1)
    assert store.domains()[0]['suspicious']==1
    assert store.claim(2) is None
    for i in range(10):store.discover(seed['id'],f'https://example.org/trap/{i}',2)
    assert store.domains()[0]['pages']==3
    assert store.dashboard()['counts']=={'running':1,'paused':2}
    with pytest.raises(ValueError):store.domain_action('example.org',3)
    store.domain_action('example.org',5)
    assert store.claim(2)
    assert not store.domains()[0]['suspicious']


def test_policy_changes_paused_seeds_and_fuzzy_pagination(tmp_path):
    store,seed=setup(tmp_path)
    for i in range(25):store.discover(seed['id'],f'https://example.org/products/shoes-{i}',1)
    assert len(store.pages('example.org')['items'])==20
    assert len(store.pages('example.org',page=2)['items'])==6
    assert store.pages('example.org','shos')['total']==25
    assert store.discover(seed['id'],'https://outside.org/',1) is None
    store.save_seed({k:v for k,v in {**seed,'enabled':False}.items() if k!='id'},seed['id'])
    assert store.claim(1) is None
    other=store.save_seed(dict(url='https://second.org/',allowed_domains=['second.org']))
    assert store.activity(store.claim(1))['seed_id']==other['id']


def test_worker_reuses_semantic_extractor_and_persists_partial_content(tmp_path):
    store,seed=setup(tmp_path)
    activity=store.claim(1)
    audit=AuditStore(tmp_path/'audit')
    @contextmanager
    def loader(url):
        yield PageCapture(url,url,'A product','''<html><head><title>A product</title>
          <script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Test shoe","offers":{"@type":"Offer","price":"20","priceCurrency":"USD"}}</script></head>
          <body><main><h1>Test shoe</h1></main><nav><a href="/next">Next</a></nav></body></html>''')
    execute_activity(store,activity,audit,loader)
    result=store.activity(activity)
    assert result['status']=='done'
    assert {'loading','capture','extracting','saving','done'}<={s['stage'] for s in result['stages']}
    assert store.pages('example.org')['total']==2
    visits=audit.list_visits(result['run_id'])
    visit=audit.get_visit(visits[0]['id'])
    assert visit['result']['model'].startswith('semantic')
    assert visit['costs']['amount_usd']=='0'
    assert visit['costs']['exact']
    assert any(e['role']=='product' for e in visit['result']['entities'])
    assert sum(b['count'] for b in store.dashboard()['hourly'])==1


def test_api_configuration_and_activity_links(tmp_path):
    store,seed=setup(tmp_path)
    app=FastAPI();app.include_router(router(store,AuditStore(tmp_path/'audit')))
    client=TestClient(app)
    assert client.put('/api/service/settings',json={**store.settings(),'threads':51}).status_code==422
    assert client.post('/api/service/seeds',json={'url':'https://example.org','blocked_url_patterns':['[']}).status_code==422
    response=client.get('/api/service/pages?domain=example.org')
    page=response.json()['items'][0]
    assert client.get('/api/service/activities/'+page['activity_id']).status_code==200
    assert client.post('/api/service/pages/'+page['id']+'/rescrape').json()['activity_id']==page['activity_id']
    assert client.get('/api/service/pages?domain=example.org&page=0').status_code==422
    assert client.post('/api/service/domains/action',json={'domain':'example.org'}).status_code==200


def test_rules_validate_each_item_and_enforce_exclusive_modes(tmp_path):
    store=Store(tmp_path/'persistent')
    app=FastAPI();app.include_router(router(store,AuditStore(tmp_path/'audit')))
    client=TestClient(app)
    assert client.post('/api/service/rules/validate',json={'kind':'domain','value':' HTTPS://bad.example/a '}).status_code==422
    assert client.post('/api/service/rules/validate',json={'kind':'domain','value':' *.EXAMPLE.ORG '}).json()=={'value':'example.org'}
    assert client.post('/api/service/rules/validate',json={'kind':'regex','value':'[broken'}).status_code==422
    # Python syntax is checked server-side, not by the browser's JS RegExp engine.
    assert client.post('/api/service/rules/validate',json={'kind':'regex','value':'(?i)/products/'}).status_code==200
    seed={'url':'https://example.org/','rule_mode':'white','allowed_domains':['example.org']}
    assert client.post('/api/service/seeds',json={**seed,'blocked_url_patterns':['/login']}).status_code==422
    assert client.post('/api/service/seeds',json={**seed,'rule_mode':'black'}).status_code==201
    invalid=client.post('/api/service/seeds',json={**seed,'allowed_url_patterns':['/ok','[bad']})
    assert invalid.status_code==422
    assert invalid.json()['detail'][0]['loc']==['body','allowed_url_patterns',1]
    created=client.post('/api/service/seeds',json=seed)
    assert created.status_code==201
    identifier=created.json()['id']
    switched=client.put('/api/service/seeds/'+identifier,json={'url':seed['url'],'rule_mode':'black','blocked_url_patterns':['/login']})
    assert switched.status_code==200
    assert switched.json()['allowed_domains']==[]
    assert client.post('/api/service/seeds',json={'url':seed['url'],'rule_mode':'white'}).status_code==422


def test_url_regex_has_no_delimiters_and_preserves_pattern_exactly(tmp_path):
    from src.persistent.rules import validate_rule
    from src.common.scrapper.url_policy import URLPolicy
    pattern=r'https://example[.]org/products/\d+/?$'
    assert validate_rule('regex',pattern)==pattern
    policy=URLPolicy(allowed_url_patterns=[pattern])
    assert policy.disposition('https://example.org/products/123/') is None
    assert policy.disposition('https://example.org/products/abc/')=='outside_allowlist'
    assert validate_rule('regex','/products/')=='/products/'
    assert validate_rule('regex',' leading and trailing ')==' leading and trailing '
    store=Store(tmp_path/'service')
    app=FastAPI();app.include_router(router(store,AuditStore(tmp_path/'audit')))
    client=TestClient(app)
    assert client.post('/api/service/rules/validate',json={'kind':'regex','value':pattern}).json()['value']==pattern
    seed=client.post('/api/service/seeds',json={'url':'https://example.org/products/123/','rule_mode':'white','allowed_url_patterns':[pattern]})
    assert seed.status_code==201
    assert store.seeds()[0]['allowed_url_patterns']==[pattern]


def test_shared_pages_union_of_seed_policies_and_global_dedup(tmp_path):
    store,first=setup(tmp_path)
    second=store.save_seed({'url':'https://example.net/','rule_mode':'black','blocked_domains':['example.org']})
    # The second origin rejects this URL; the first allows it. Discovery from
    # either origin must admit it, with the same activity and single execution.
    shared='https://example.org/shared?utm_source=one'
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        ids=list(pool.map(lambda i:store.discover([first,second][i%2]['id'],shared,1),range(40)))
    assert len(set(ids))==1 and ids[0]
    with store.db() as db:
        row=db.execute('SELECT * FROM activities WHERE id=?',(ids[0],)).fetchone()
        assert db.execute('SELECT count(*) FROM activities WHERE page_id=?',(row['page_id'],)).fetchone()[0]==1
    # Now both origins allow the page. Adding the second association is not a
    # reason to extract it again, including after completion and restart.
    second=store.save_seed({'url':'https://example.net/','allowed_domains':['example.org','example.net']},second['id'])
    assert store.discover(second['id'],shared,2)==ids[0]
    with store.db() as db:
        db.execute("UPDATE activities SET status='running' WHERE id=?",(ids[0],))
    store.finish(ids[0])
    assert Store(store.root).discover(second['id'],shared,2)==ids[0]
    with store.db() as db:
        assert db.execute('SELECT count(*) FROM discoveries WHERE page_id=?',(row['page_id'],)).fetchone()[0]==2
    manual=store.rescrape(row['page_id'])
    assert manual!=ids[0]
    assert store.rescrape(row['page_id'])==manual


def test_claim_uses_any_current_origin_not_only_first(tmp_path):
    store,first=setup(tmp_path)
    shared=store.discover(first['id'],'https://example.org/shared',1)
    second=store.save_seed({'url':'https://example.net/','allowed_domains':['example.net','example.org']})
    store.save_seed({'url':first['url'],'allowed_domains':['example.org'],'enabled':False},first['id'])
    claimed=[]
    while activity:=store.claim(1):
        claimed.append(activity)
        store.finish(activity)
    assert shared in claimed
    assert store.activity(shared)['seed_id']==second['id']


def test_seed_pagination_and_regex_trial_urls(tmp_path):
    store=Store(tmp_path/'service')
    for i in range(23):store.save_seed({'url':f'https://example.org/{i}','allowed_domains':['example.org']})
    app=FastAPI();app.include_router(router(store,AuditStore(tmp_path/'audit')))
    client=TestClient(app)
    page=client.get('/api/service/seeds?page=2').json()
    assert len(page['items'])==3 and page['total']==23
    assert client.get('/api/service/seeds?page=0').status_code==422
    response=client.post('/api/service/rules/test',json={'pattern':r'https://example[.]org/products/\d+/?$','urls':['https://example.org/products/42/?utm_source=x','https://example.org/articles/42','bad url']})
    assert response.status_code==200
    assert [r['matched'] for r in response.json()['results']]==[True,False,False]
    assert response.json()['results'][2]['error']
    assert client.post('/api/service/rules/test',json={'pattern':'[broken','urls':['https://example.org/']}).status_code==422


def test_discovery_includes_links_outside_semantic_regions_without_cap(tmp_path):
    store,seed=setup(tmp_path,suspicious_pages=1000)
    activity=store.claim(1)
    audit=AuditStore(tmp_path/'audit')
    @contextmanager
    def loader(url):
        html='<html><head><title>Links</title><base href="https://example.org/sub/"></head><body><div style="display:none">'
        html+=''.join(f'<a href="page-{i}">Page</a>' for i in range(305))
        html+='<area href="map"><a href="mailto:a@example.org">Mail</a></div></body></html>'
        yield PageCapture(url,url,'Links',html)
    execute_activity(store,activity,audit,loader)
    assert store.pages('example.org')['total']==307
    assert store.activity(activity)['status']=='done'


def test_slots_are_stable_and_idle_and_seed_pages_are_isolated(tmp_path):
    store,seed=setup(tmp_path,threads=4)
    other=store.save_seed({'url':'https://other.example/','allowed_domains':['other.example']})
    store.discover(seed['id'],'https://example.org/one',1)
    slots=store.dashboard()['slots']
    assert [s['slot'] for s in slots]==[1,2,3,4]
    assert all(s['status']=='idle' and s['activity'] is None for s in slots)
    activity=store.claim(2)
    assert store.claim(2) is None
    slots=store.dashboard()['slots']
    assert slots[1]['activity']['id']==activity
    assert slots[1]['status']=='running'
    assert slots[0]['status']=='idle'
    store.finish(activity)
    assert store.dashboard()['slots'][1]['status']=='idle'
    assert store.pages(seed_id=seed['id'])['total']==2
    assert store.pages(seed_id=other['id'])['total']==1
    app=FastAPI();app.include_router(router(store,AuditStore(tmp_path/'audit')))
    result=TestClient(app).get('/api/service/seeds/'+seed['id']+'/pages').json()
    assert {p['url'] for p in result['items']}=={'https://example.org/','https://example.org/one'}


def test_expired_extractions_allow_one_new_shared_activity(tmp_path):
    store,first=setup(tmp_path,freshness_hours=2)
    second=store.save_seed({'url':'https://example.net/','allowed_domains':['example.net','example.org']})
    activity=store.discover(first['id'],'https://example.org/shared',1)
    with store.db() as db:
        db.execute("UPDATE activities SET status='done',finished=? WHERE id=?",(time.time()-3600,activity))
    assert store.discover(second['id'],'https://example.org/shared',1)==activity
    with store.db() as db:
        db.execute('UPDATE activities SET finished=? WHERE id=?',(time.time()-7201,activity))
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        ids=list(pool.map(lambda i:store.discover([first,second][i%2]['id'],'https://example.org/shared',1),range(30)))
    assert len(set(ids))==1 and ids[0]!=activity
    assert store.activity(ids[0])['status']=='queued'
    assert store.activity(activity)['status']=='done'


def test_resource_admission_leaves_idle_slots_when_memory_is_insufficient():
    from src.persistent.worker import can_start_worker
    assert can_start_worker({'memory_mb':2048},2,900)
    assert can_start_worker({'memory_mb':2048},3,900)
    assert can_start_worker({'memory_mb':2048},1,1700)
    assert not can_start_worker({'memory_mb':2048},1,2048)
    assert not can_start_worker({'memory_mb':2048},1,2200)
    assert can_start_worker({'memory_mb':32768},49,25000)


def test_individual_memory_threshold_only_interrupts_that_activity(tmp_path):
    import subprocess
    import sys
    from src.persistent.worker import activity_memory, process_snapshot, interrupt_over_limit
    store,seed=setup(tmp_path)
    store.discover(seed['id'],'https://example.org/second',1)
    first=store.claim(1);second=store.claim(2)
    processes=[]
    try:
        for _ in range(2):
            p=subprocess.Popen([sys.executable,'-c',"import time; data=bytearray(24*1024*1024); print('ready',flush=True); time.sleep(30)"],stdout=subprocess.PIPE,text=True,start_new_session=True)
            processes.append(p);assert p.stdout.readline().strip()=='ready'
        p,other=processes
        memory,members=activity_memory(p.pid,process_snapshot())
        assert memory>20 and other.pid not in members
        assert not interrupt_over_limit(store,p,first,memory,1024,members)
        assert p.poll() is None
        assert interrupt_over_limit(store,p,first,memory,1,members)
        assert other.poll() is None
        assert store.activity(first)['status']=='error'
        assert 'umbral de memoria' in store.activity(first)['error']
        assert store.activity(second)['status']=='running'
    finally:
        for p in processes:
            if p.poll() is None:p.kill()
            p.wait()


def test_global_memory_deferral_does_not_claim_or_consume_attempts(tmp_path):
    from src.persistent.worker import can_start_worker
    store,seed=setup(tmp_path,memory_mb=256)
    activity=store.activities()['items'][0]
    if can_start_worker(store.settings(),0,300):store.claim(1)
    unchanged=store.activity(activity['id'])
    assert unchanged['status']=='queued' and unchanged['attempt']==0
    assert unchanged['error'] is None
    assert can_start_worker(store.settings(),0,100)
    assert store.claim(1)==activity['id']


def test_service_memory_is_admission_only_not_a_cgroup_kill(monkeypatch):
    from types import SimpleNamespace
    from pathlib import Path
    from src.persistent.worker import ResourceLimits, UNIT
    limits=ResourceLimits();limits.cgroup=Path('/sys/fs/cgroup')/UNIT
    commands=[]
    monkeypatch.setattr('src.persistent.worker.subprocess.run',lambda command,**kwargs:commands.append(command) or SimpleNamespace(returncode=0))
    assert limits.apply({'memory_mb':2048,'cpu_percent':100})
    assert 'MemoryMax=infinity' in commands[0]
    assert 'MemoryHigh=infinity' in commands[0]
    assert 'CPUQuota=100%' in commands[0]


def test_semantic_reads_closed_details_and_aria_answers_without_hops(tmp_path):
    from src.common.scrapper import Scrapper, ScrapperConfig, SemanticMetadataStrategy
    html='''<html><head><title>Questions</title></head><body>
    <details><summary>How is it used?</summary><div>Use a small amount every morning.</div></details>
    <button aria-expanded="false" aria-controls="answer">Where is it stored?</button>
    <section id="answer" hidden>Keep in a dry place.</section>
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"Question","name":"When?","acceptedAnswer":{"@type":"Answer","text":"Every day."}}</script>
    </body></html>'''
    @contextmanager
    def loader(url):yield PageCapture(url,url,'Questions',html)
    audit=AuditStore(tmp_path/'audit')
    scraper=Scrapper(ScrapperConfig(strategy=SemanticMetadataStrategy(),page_loader=loader,max_sites=1),audit=audit)
    result=scraper.run(['https://example.org/'])
    assert result.scheduled==1
    visit=audit.get_visit(audit.list_visits(result.run_id)[0]['id'])
    questions=[e for e in visit['result']['entities'] if e['role'] in ('qa','semantic_disclosure')]
    answers=[v for e in questions for part in e['parts'] if part['class']=='answer' for v in part['values']]
    assert {a['text'] for a in answers}=={'Use a small amount every morning.','Keep in a dry place.','Every day.'}
    assert [a['visible'] for a in answers if a['source']=='semantic_dom']==[False,False]
    assert audit.read_html(visit['id'])==html
    assert visit['costs']['amount_usd']=='0'


def test_failed_retry_preserves_history_and_shares_new_attempt(tmp_path):
    store,seed=setup(tmp_path)
    activity=store.claim(1)
    store.finish(activity,'Historical interruption',retryable=False)
    app=FastAPI();app.include_router(router(store,AuditStore(tmp_path/'audit')))
    client=TestClient(app)
    first=client.post('/api/service/activities/'+activity+'/retry')
    assert first.status_code==202
    retry=first.json()['activity_id']
    assert retry==activity
    assert client.post('/api/service/activities/'+activity+'/retry').json()['activity_id']==retry
    assert store.activity(activity)['status']=='queued'
    assert store.activity(activity)['errors'][-1]['error']=='Historical interruption'
    assert store.activity(retry)['attempt']==1
    assert client.post('/api/service/activities/'+retry+'/retry').status_code==202


def test_rediscovery_retries_same_activity_and_caps_three_errors_in_24h(tmp_path):
    store,seed=setup(tmp_path)
    activity=store.claim(1)
    for i in range(3):
        store.finish(activity,f'Failure {i+1}',retryable=False)
        assert len(store.activity(activity)['errors'])==i+1
        assert store.discover(seed['id'],seed['url'],1)==activity
        if i<2:
            assert store.activity(activity)['status']=='queued'
            assert store.claim(1)==activity
        else:
            assert store.activity(activity)['status']=='error'
            assert store.claim(1) is None
    with store.db() as db:
        db.execute('UPDATE failure_events SET at=? WHERE id=(SELECT min(id) FROM failure_events)',(time.time()-86401,))
    assert store.discover(seed['id'],seed['url'],1)==activity
    assert store.claim(1)==activity
    assert len(store.activity(activity)['errors'])==3
    assert store.activity(activity)['errors_last_24h']==2


def test_manual_retry_can_override_automatic_error_window(tmp_path):
    store,seed=setup(tmp_path)
    activity=store.claim(1)
    for i in range(3):
        store.finish(activity,'failed',retryable=False)
        if i<2:
            assert store.rescrape(store.activity(activity)['page_id'])==activity
            assert store.claim(1)==activity
    assert store.discover(seed['id'],seed['url'],1)==activity
    assert store.activity(activity)['status']=='error'
    assert store.rescrape(store.activity(activity)['page_id'])==activity
    assert store.activity(activity)['status']=='queued'
    assert len(store.activity(activity)['errors'])==3


def test_seed_status_reflects_work_not_enabled_flag(tmp_path):
    store,seed=setup(tmp_path)
    def current():
        return next(item for item in store.seed_page()['items'] if item['id']==seed['id'])
    assert current()['status']=='queued'
    activity=store.claim(1)
    assert current()['status']=='running'
    store.finish(activity,error='Interrupted',retryable=False)
    assert current()['status']=='completed_with_errors'
    assert current()['activity_counts']=={'error':1}
    store.rescrape(current()['page_id'])
    assert current()['status']=='queued'
    assert store.claim(1)==activity
    store.finish(activity)
    assert current()['status']=='idle'
    assert current()['activity_counts']=={'done':1}


def test_whitelist_domain_does_not_bypass_path_regex(tmp_path):
    store=Store(tmp_path/'service')
    store.save_settings({'enabled':True})
    seed=store.save_seed({'url':'https://example.org/catalog', 'allowed_domains':['example.org'],
                          'allowed_url_patterns':[r'https://example\.org/catalog/.*']})
    assert store.matching_seeds(seed['url'])==[seed['id']]
    assert store.discover(seed['id'],'https://example.org/catalog/item',1)
    assert store.discover(seed['id'],'https://example.org/about',1) is None
    assert store.discover(seed['id'],'https://elsewhere.org/catalog/item',1) is None
    other=store.save_seed({'url':'https://example.org/about','allowed_url_patterns':['/about']})
    assert store.discover(seed['id'],'https://example.org/about',1)
    assert store.matching_seeds('https://example.org/about')==[other['id']]


def test_clear_origin_history_deletes_exclusive_captures_preserves_shared(tmp_path):
    store,seed=setup(tmp_path)
    audit=AuditStore(tmp_path/'audit')
    activity=store.claim(1)
    run=audit.start_run({})
    store.attach_run(activity,run)
    visit=audit.start_visit(run,seed['url'],seed['url'],0,None,None,'')
    paths=audit.save_capture(visit,1,PageCapture(seed['url'],seed['url'],'Title','<html>Content</html>',None))
    store.finish(activity)
    other=store.save_seed({'url':'https://other.example/','allowed_domains':['other.example']})
    shared=store.discover(seed['id'],'https://example.org/shared',1)
    with store.db() as db:
        shared_page=db.execute('SELECT page_id FROM activities WHERE id=?',(shared,)).fetchone()[0]
        db.execute('INSERT INTO discoveries VALUES(?,?,1)',(other['id'],shared_page))
    result=store.clear_seed_history(seed['id'],audit)
    assert result['deleted_activities']==1 and not result['file_errors']
    assert not (audit.root/paths[0]).exists()
    with audit._connect() as db:
        assert db.execute('SELECT count(*) FROM runs').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM visits').fetchone()[0]==0
    assert store.activity(activity) is None
    assert store.activity(shared)['seed_id']==other['id']
    assert store.pages(seed_id=seed['id'])['total']==0
    assert not next(s for s in store.seeds() if s['id']==seed['id'])['enabled']
    assert store.clear_seed_history(seed['id'],audit)['deleted_activities']==0


def test_clear_history_cancels_active_work(tmp_path):
    store,seed=setup(tmp_path)
    activity=store.claim(1)
    audit=AuditStore(tmp_path/'audit')
    app=FastAPI();app.include_router(router(store,audit))
    response=TestClient(app).delete('/api/service/seeds/'+seed['id']+'/history')
    assert response.status_code==200
    assert store.activity(activity) is None
    assert not store.seeds()[0]['enabled']


def test_start_after_clearing_history_requeues_initial_url(tmp_path):
    store,seed=setup(tmp_path)
    store.clear_seed_history(seed['id'],AuditStore(tmp_path/'audit'))
    result=store.start_seed(seed['id'])
    assert result['engine_enabled']
    assert store.seeds()[0]['enabled']
    assert store.activity(result['activity_id'])['status']=='queued'
    assert store.start_seed(seed['id'])['activity_id']==result['activity_id']
    assert store.claim(1)==result['activity_id']


def test_seed_pause_keeps_queue_and_start_resumes(tmp_path):
    store,seed=setup(tmp_path)
    activity=store.activities()['items'][0]['id']
    store.pause_seed(seed['id'])
    assert store.claim(1) is None
    assert store.activity(activity)['status']=='queued'
    assert store.start_seed(seed['id'])['activity_id']==activity
    assert store.claim(1)==activity


def test_hourly_chart_counts_attempts_success_and_failures(tmp_path):
    store,seed=setup(tmp_path,retry_delay=1)
    activity=store.claim(1)
    store.finish(activity,error='Temporary failure')
    with store.db() as db:
        db.execute('UPDATE activities SET due=0 WHERE id=?',(activity,))
    assert store.claim(1)==activity
    store.finish(activity)
    points=store.dashboard()['hourly']
    assert sum(p['explored'] for p in points)==2
    assert sum(p['extracted'] for p in points)==1
    assert sum(p['errors'] for p in points)==1


def test_task_progress_retains_stage_on_error_and_resets_on_retry(tmp_path):
    store,seed=setup(tmp_path)
    activity=store.claim(1)
    assert store.activity(activity)['progress']==10
    store.stage(activity,'saving')
    store.stage(activity,'interaction')
    assert store.dashboard()['workers'][0]['progress']==90
    store.finish(activity,error='Failure',retryable=False)
    assert store.activities()['items'][0]['progress']==90
    store.rescrape(store.activity(activity)['page_id'])
    assert store.activity(activity)['progress']==0
    store.claim(1)
    assert store.activity(activity)['progress']==10
    store.finish(activity)
    assert store.activity(activity)['progress']==100


def test_failed_activity_reports_referring_page_and_activity(tmp_path):
    store,seed=setup(tmp_path)
    audit=AuditStore(tmp_path/'audit')
    source=store.claim(1)
    run=audit.start_run({})
    store.attach_run(source,run)
    visit=audit.start_visit(run,seed['url'],seed['url'],0,None,None,'')
    target='https://example.org/missing'
    activity=store.discover(seed['id'],target,1)
    audit.record_edge(run,visit,None,target,target,1,None,'discovered')
    audit.record_edge(run,visit,None,target,target,1,None,'discovered')
    store.finish(source)
    assert store.claim(1)==activity
    store.finish(activity,error='HTTP 404',retryable=False)
    app=FastAPI();app.include_router(router(store,audit))
    data=TestClient(app).get('/api/service/activities/'+activity).json()
    assert len(data['referrers'])==1
    assert data['referrers'][0]['url']==seed['url']
    assert data['referrers'][0]['activity_id']==source
    assert data['referrers'][0]['visit_id']==visit


def test_history_deletion_terminates_only_exclusive_worker(tmp_path):
    import subprocess,sys
    store,seed=setup(tmp_path)
    activity=store.claim(1)
    process=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)','src.persistent.worker','--root',str(store.root),'--activity',activity],start_new_session=True)
    try:
        store.clear_seed_history(seed['id'],AuditStore(tmp_path/'audit'))
        assert process.wait(timeout=3)==-9
        assert store.activity(activity) is None
    finally:
        if process.poll() is None:process.kill();process.wait()


def test_black_regex_mode_keeps_domains_as_scope(tmp_path):
    store=Store(tmp_path/'service')
    seed=store.save_seed({'url':'https://www.laroche-posay.com.uy/','rule_mode':'black','allowed_domains':['www.laroche-posay.com.uy'],'blocked_url_patterns':['/login']})
    assert store.discover(seed['id'],'https://www.laroche-posay.com.uy/products',1)
    assert store.discover(seed['id'],'https://www.laroche-posay.com.uy/login',1) is None
    assert store.discover(seed['id'],'https://elsewhere.example/products',1) is None


def test_activity_counters_use_rolling_hour(tmp_path):
    store,seed=setup(tmp_path)
    first=store.claim(1);store.finish(first)
    store.discover(seed['id'],'https://example.org/two',1)
    second=store.claim(2);store.finish(second,error='Failure')
    assert store.dashboard()['activity_counters']==dict(pages_per_hour=1,queued=1,running=0,errors_per_hour=1)
    with store.db() as db:
        db.execute("UPDATE stages SET at=? WHERE stage='done'",(time.time()-3601,))
        db.execute('UPDATE failure_events SET at=?',(time.time()-3601,))
    assert store.dashboard()['activity_counters']==dict(pages_per_hour=0,queued=1,running=0,errors_per_hour=0)


def test_admission_has_no_margin_at_six_gib():
    from src.persistent.worker import can_start_worker
    assert can_start_worker({'memory_mb':6144},49,6143.9)
    assert not can_start_worker({'memory_mb':6144},49,6144)


def test_pending_work_does_not_warm_browser_without_eligible_pages(tmp_path):
    store,seed=setup(tmp_path)
    assert store.has_pending_work()
    activity=store.claim(1)
    assert not store.has_pending_work()
    store.finish(activity,error='Temporary')
    assert not store.has_pending_work()  # Retry delay has not elapsed.
    with store.db() as db:db.execute('UPDATE activities SET due=0')
    assert store.has_pending_work()
    store.save_seed({**{k:v for k,v in seed.items() if k!='id'},'enabled':False},seed['id'])
    assert not store.has_pending_work()
