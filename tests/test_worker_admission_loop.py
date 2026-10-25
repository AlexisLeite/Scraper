"""Supervisor admissions must not wait for browser startup or per-page timers."""
import signal
from types import SimpleNamespace

from src.persistent.store import Store
from src.persistent import worker


def test_heartbeat_while_browser_starts_then_fill_all_free_slots(tmp_path,monkeypatch):
    store=Store(tmp_path/'service')
    store.save_settings({**store.settings(),'enabled':True,'threads':8,'memory_mb':6144})
    seed=store.save_seed({'url':'https://example.org/','allowed_domains':['example.org']})
    for i in range(7):store.discover(seed['id'],f'https://example.org/{i}',1)
    signals={};spawned=[];heartbeats=[]
    class Pool:
        def __init__(self,*args):self.calls=0
        def reap(self,**kwargs):pass
        def needs_host(self):return False
        def prepare(self):self.calls+=1;return self.calls>1
        def allocate(self,slot,activity,wait):
            assert wait is False
            return {}
        def renderer_pids(self,activity):return set()
        def release(self,activity):pass
        def close(self):pass
    class Limits:
        error=None
        def apply(self,settings):return True
        def usage(self):return {'memory_mb':100}
        def memory_mb(self):return 100
    def spawn(*args,**kwargs):
        spawned.append(args)
        return SimpleNamespace(pid=1000000+len(spawned),poll=lambda:None,wait=lambda:0)
    original_heartbeat=store.heartbeat
    def heartbeat(data):
        original_heartbeat(data)
        heartbeats.append(len(spawned))
        if len(heartbeats)==2:signals[signal.SIGTERM]()
    monkeypatch.setattr(worker,'BrowserPool',Pool)
    monkeypatch.setattr(worker,'ResourceLimits',Limits)
    monkeypatch.setattr(worker,'recover_audit',lambda *a:None)
    monkeypatch.setattr(worker,'process_snapshot',lambda:{})
    monkeypatch.setattr(worker.subprocess,'Popen',spawn)
    monkeypatch.setattr(worker.signal,'signal',lambda sig,handler:signals.update({sig:handler}))
    monkeypatch.setattr(worker.os,'killpg',lambda *args:None)
    monkeypatch.setattr(worker.time,'sleep',lambda _:None)
    monkeypatch.setattr(worker.time,'monotonic',lambda:100)
    monkeypatch.setattr(store,'heartbeat',heartbeat)
    worker.daemon(store)
    assert heartbeats==[0,8]
    assert len(spawned)==8
