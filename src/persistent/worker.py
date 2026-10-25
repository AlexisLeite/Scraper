"""One supervised process per logical worker; Playwright remains thread-local."""
import argparse
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from .store import Store, ROOT
from .capture_policy import validate_destination
from .browser_pool import BrowserPool, process_memory
from ..common.scrapper import AuditStore, SemanticMetadataStrategy
from ..common.scrapper.runtime import PageExecutor
from ..common.scrapper.models import RunSummary
from ..common.scrapper.orchestrator import canonical_uri

UNIT = os.environ.get('SCRAPPER_WORKER_UNIT','scrapper-worker.service')
LOG = logging.getLogger(__name__)


def execute_activity(store, activity_id, audit=None, loader=None):
    activity = store.activity(activity_id)
    audit = audit or AuditStore()
    strategy = SemanticMetadataStrategy()
    run_id = audit.start_run(dict(persistent_activity=activity_id, initial_uris=[activity['url']],
        strategy='semantic', usage_tracking=True, expense_tracking=True, expansion_visits=True))
    store.attach_run(activity_id,run_id)
    edge = audit.record_edge(run_id,None,None,activity['url'],activity['url'],activity['depth'],None,'queued')
    visit_id = audit.start_visit(run_id,activity['url'],activity['url'],activity['depth'],None,None,edge)
    expansions = set()
    scheduled = 1

    def stage(name, effects=False):
        if effects:
            store.effects(activity_id)
        store.stage(activity_id,name)

    def discover(request):
        try:
            target = canonical_uri(request.uri)
            accepted = store.discover(activity['seed_id'],target,request.depth)
            decision = 'discovered' if accepted else 'outside_seed_policies'
        except ValueError:
            target,decision = None,'invalid_uri'
        audit.record_edge(run_id,request.parent_visit_id,request.via,request.uri,target,
                          request.depth,request.confidence,decision)

    def reserve(uri,depth,parent,key,via,confidence,blocked):
        nonlocal scheduled
        uri = canonical_uri(uri)
        decision = blocked or (None if store.matching_seeds(uri) else 'outside_seed_policies')
        if not decision and scheduled >= 10:
            decision = 'max_sites'
        if not decision and (uri,key) in expansions:
            decision = 'duplicate'
        edge = audit.record_edge(run_id,parent,via,uri,uri,depth,confidence,decision or 'queued')
        if decision:
            return None,decision
        # Persist the interaction marker before allowing a click. Interrupted
        # interactions are retained for manual review and never replayed.
        store.effects(activity_id)
        expansions.add((uri,key))
        scheduled += 1
        return audit.start_visit(run_id,uri,uri,depth,parent,via,edge,kind='expansion'),'scheduled'

    def validate_capture(capture):
        validate_destination(store.seeds(), activity['url'], capture.final_uri)

    executor = PageExecutor(strategy,audit,max_retries=0,validate_capture=validate_capture,**({'page_loader':loader} if loader else {}))
    try:
        outcome = executor.execute(SimpleNamespace(uri=activity['url'],depth=activity['depth']),
            visit_id,run_id,reserve,on_navigation=discover,on_stage=stage)
        audit.finish_run(RunSummary(run_id,scheduled,outcome.succeeded,outcome.failed,
                                  outcome.doubtful,len(outcome.events),0))
        failed = [v for v in audit.list_visits(run_id) if v['status']=='error']
        if any(any(attempt['phase']=='excluded' for attempt in audit.get_visit(v['id'])['attempts']) for v in failed):
            store.exclude(activity_id,failed[-1]['error'])
        else:
            blocked = any(attempt['phase'] == 'access_blocked'
                          for visit in failed for attempt in audit.get_visit(visit['id'])['attempts'])
            store.finish(activity_id,failed[-1]['error'] if failed else None,retryable=not blocked)
    except Exception as exc:
        audit.fail_visit(visit_id,str(exc))
        store.finish(activity_id,f'{type(exc).__name__}: {exc}')
        raise


class ResourceLimits:
    """CPU throttling and aggregate measurements, never a global memory kill.

    Global RAM is an admission threshold. Individual activity memory is monitored
    separately, so one heavy page cannot cause the whole worker group to die.
    """
    def __init__(self):
        self.applied = None
        self.error = None
        self.previous_cpu = None
        relative = next((s.split('::',1)[1].strip() for s in Path('/proc/self/cgroup').read_text().splitlines() if s.startswith('0::')),None)
        self.cgroup = Path('/sys/fs/cgroup') / relative.lstrip('/') if relative else None

    def apply(self, settings):
        wanted = (settings['cpu_percent'],settings['memory_mb'])
        if self.applied == wanted:
            return True
        if not self.cgroup or self.cgroup.name != UNIT:
            self.error = 'El motor requiere scrapper-worker.service para aplicar límites reales de CPU y memoria.'
            return False
        result = subprocess.run(['systemctl','set-property','--runtime',UNIT,
             f'CPUQuota={wanted[0]}%','MemoryMax=infinity','MemoryHigh=infinity','MemorySwapMax=infinity'],capture_output=True,text=True,timeout=10)
        if result.returncode:
            self.error = result.stderr.strip()
            return False
        self.applied = wanted
        self.error = None
        return True

    def memory_mb(self):
        return int((self.cgroup/'memory.current').read_text())/1024**2

    def usage(self):
        try:
            memory = self.memory_mb()
            cpu = dict(line.split() for line in (self.cgroup/'cpu.stat').read_text().splitlines())
            sample = (time.monotonic(),int(cpu['usage_usec']))
            usage = 0 if self.previous_cpu is None else (sample[1]-self.previous_cpu[1])/(sample[0]-self.previous_cpu[0])/10000
            self.previous_cpu = sample
            return dict(memory_mb=round(memory,1),cpu_used_percent=round(usage,1),cpu_percent=self.applied[0] if self.applied else None,
                        memory_limit_mb=self.applied[1] if self.applied else None)
        except (OSError,TypeError):
            return {}


def recover_audit(store, activity_id=None, reason='Proceso interrumpido; evidencia conservada.'):
    audit = AuditStore()
    with store.db() as db:
        rows = [dict(r) for r in db.execute("SELECT id,run_id,strategy,stage FROM activities WHERE slot IS NOT NULL AND run_id IS NOT NULL AND (? IS NULL OR id=?)",(activity_id,activity_id))]
    for row in rows:
        visits = audit.list_visits(row['run_id'])
        for visit in visits:
            if visit['status'] == 'running':
                if row['strategy'] == 'jev' and row['stage'] not in ('starting','loading','capture'):
                    audit.mark_usage_incomplete(visit['id'],'Interrupción durante la estrategia; puede faltar consumo.')
                audit.fail_visit(visit['id'],reason)
        if visits:
            audit.finish_run(RunSummary(row['run_id'],len(visits),sum(v['status']=='done' for v in visits),
                sum(v['status']!='done' for v in visits),sum(bool(v['doubtful']) for v in visits),0,0))


def can_start_worker(settings, running, memory_mb):
    # User policy: observed memory only, no predicted reserve or margin.
    return memory_mb < settings['memory_mb']


def process_snapshot():
    result = {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().rsplit(')',1)[1].split()
            result[int(path.parent.name)] = (int(fields[1]),int(fields[2]))
        except (OSError,ValueError,IndexError):
            continue
    return result


def activity_memory(pid, snapshot):
    # Include Playwright, Chromium, renderers and orphaned members of the group.
    members = {pid} | {p for p,(_,group) in snapshot.items() if group == pid}
    while True:
        children = {p for p,(parent,_) in snapshot.items() if parent in members}
        if children <= members:
            break
        members |= children
    total = 0
    for member in members:
        try:
            data = (Path('/proc')/str(member)/'smaps_rollup').read_text()
            total += next(int(line.split()[1]) for line in data.splitlines() if line.startswith('Pss:'))/1024
        except (OSError,ValueError,StopIteration):
            continue
    return total,members


def interrupt_over_limit(store, process, activity_id, memory_mb, threshold, members):
    if memory_mb <= threshold or process.poll() is not None:
        return False
    reason = f'La actividad superó su umbral de memoria: {memory_mb:.0f} MB > {threshold} MB.'
    for pid in members:
        try:
            os.kill(pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)
    recover_audit(store,activity_id,reason)
    store.finish(activity_id,reason,retryable=False)
    return True


def daemon(store):
    lock = open(store.root/'daemon.lock','w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    recover_audit(store)
    store.recover()
    limits = ResourceLimits()
    running = {}
    browsers = BrowserPool(store.root)
    stopping = False
    draining = False
    next_start = 0
    next_memory_sample = 0
    browser_error = None
    memory_samples = {}
    def stop(*_):
        nonlocal stopping
        stopping = True
    def drain(*_):
        nonlocal draining
        draining = True
    signal.signal(signal.SIGUSR1,drain)
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    try:
        while not stopping:
            browsers.reap()
            settings = store.settings()
            allowed = limits.apply(settings)
            resources = limits.usage()
            snapshot = process_snapshot() if time.monotonic() >= next_memory_sample else None
            if snapshot is not None:
                next_memory_sample = time.monotonic()+1
            suspects = {d['name'] for d in store.domains() if d['suspicious']}
            for slot, item in list(running.items()):
                process, activity_id, suspended = item
                activity = store.activity(activity_id)
                if process.poll() is not None:
                    # Child usually finishes its own record. A signal, OOM or crash
                    # is recovered here without losing its earlier attempt history.
                    store.finish(activity_id,f'Worker terminó con código {process.returncode}.')
                    browsers.release(activity_id)
                    memory_samples.pop(activity_id,None)
                    del running[slot]
                    continue
                if not activity or activity['status'] not in ('running','paused'):
                    continue
                if snapshot is not None:
                    memory,members = activity_memory(process.pid,snapshot)
                    memory += process_memory(browsers.renderer_pids(activity_id))
                    memory_samples[activity_id] = round(memory,1)
                    if interrupt_over_limit(store,process,activity_id,memory,settings['activity_memory_mb'],members):
                        browsers.release(activity_id)
                        memory_samples.pop(activity_id,None)
                        del running[slot]
                        continue
                pause = not settings['enabled'] or activity['domain'] in suspects or not store.matching_seeds(activity['url'])
                if pause != suspended:
                    browsers.pause(activity_id,pause)
                    try:
                        os.killpg(process.pid,signal.SIGSTOP if pause else signal.SIGCONT)
                    except ProcessLookupError:
                        continue
                    with store.db() as db:
                        db.execute('UPDATE activities SET status=? WHERE id=?',('paused' if pause else 'running',activity_id))
                    # Preserve the actual task stage while paused.
                    store.stage(activity_id,activity['stage'],'Pausa aplicada.' if pause else 'Actividad reanudada.')
                    running[slot] = (process,activity_id,pause)
            resource_wait = False
            if allowed and settings['enabled'] and not draining:
                batch_started=time.monotonic()
                for slot in range(1,settings['threads']+1):
                    if slot in running:
                        continue
                    resources['memory_mb']=round(limits.memory_mb(),1)
                    if not can_start_worker(settings,len(running),resources['memory_mb']):
                        resource_wait = True
                        browsers.reap(pressure=True)
                        break
                    if time.monotonic() < next_start:
                        break
                    if browsers.needs_host() and not store.has_pending_work():
                        break
                    try:
                        prepared=browsers.prepare()
                        browser_error=None
                    except Exception as exc:
                        prepared=False
                        browser_error=str(exc)
                        next_start=time.monotonic()+5
                    if not prepared:
                        break
                    activity_id = store.claim(slot)
                    if not activity_id:
                        break
                    try:
                        browser_env = browsers.allocate(slot,activity_id,wait=False)
                        process = subprocess.Popen([sys.executable,'-m','src.persistent.worker','--root',str(store.root),'--activity',activity_id],start_new_session=True,env={**os.environ,**browser_env})
                    except Exception as exc:
                        browsers.release(activity_id)
                        store.finish(activity_id,str(exc))
                        next_start = time.monotonic()+1
                        break
                    running[slot] = (process,activity_id,False)
                    # Fill ready hosts in batches, returning regularly to
                    # supervision/heartbeat instead of a fixed per-page delay.
                    if time.monotonic()-batch_started>=.25:
                        break
            store.heartbeat(dict(pid=os.getpid(),draining=draining,limits_applied=allowed,error=limits.error or browser_error,resource_wait=resource_wait,resources=resources,activity_memory={a:memory_samples.get(a) for _,a,_ in running.values()}))
            if draining and not running:
                stopping = True
            time.sleep(.3)
    finally:
        for process,activity_id,_ in running.values():
            try:
                os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        browsers.close()
        recover_audit(store)
        store.recover()
        lock.close()


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument('--root',default=str(ROOT))
    parser.add_argument('--activity')
    args = parser.parse_args()
    store = Store(args.root)
    if args.activity:
        execute_activity(store,args.activity)
    else:
        daemon(store)

if __name__ == '__main__':
    main()
