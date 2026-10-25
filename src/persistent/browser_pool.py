"""Shared Chromium hosts, private contexts, and per-activity renderer accounting."""
import json
import os
import secrets
import signal
import subprocess
import time
from pathlib import Path
import playwright


class BrowserPool:
    def __init__(self, root, group_size=8, idle_seconds=60):
        self.root=Path(root)/'browser-pool'
        self.root.mkdir(parents=True,exist_ok=True)
        self.group_size=group_size
        self.idle_seconds=idle_seconds
        self.hosts={}
        self.leases={}

    def _send(self, host, value):
        host['process'].stdin.write(json.dumps(value)+'\n')
        host['process'].stdin.flush()

    def available_group(self):
        counts={}
        for group in self.leases.values():counts[group]=counts.get(group,0)+1
        candidates=[group for group,host in self.hosts.items()
                    if host['process'].poll() is None and counts.get(group,0)<self.group_size]
        return max(candidates,key=lambda group:(counts.get(group,0),-group)) if candidates else None

    def needs_host(self):
        return self.available_group() is None

    def prepare(self):
        """Start a host if necessary, but never wait for Chromium to initialize."""
        group=self.available_group()
        if group is None:
            group=0
            while group in self.hosts and (group in self.leases.values() or self.hosts[group]['process'].poll() is None):group+=1
            previous=self.hosts.get(group)
            if previous:self._stop(previous)
            driver=Path(playwright.__file__).parent/'driver'
            status=self.root/f'{group}.json'
            status.unlink(missing_ok=True)
            log=open(self.root/f'{group}.log','a')
            process=subprocess.Popen([str(driver/'node'),str(Path(__file__).with_name('browser_host.cjs')),str(driver/'package'),str(status)],stdin=subprocess.PIPE,stdout=log,stderr=log,text=True,start_new_session=True)
            self.hosts[group]=dict(process=process,status=status,last_used=time.monotonic(),log=log,deadline=time.monotonic()+30)
            return False
        host=self.hosts[group]
        if self._status(host).get('endpoint'):
            return True
        if time.monotonic()>host['deadline']:
            self._stop(host)
            del self.hosts[group]
            raise RuntimeError('No se pudo iniciar el navegador compartido.')
        return False

    def allocate(self, slot, activity, wait=True):
        # Slot identity belongs to the activity, not to a fixed browser group.
        # The daemon calls prepare before claiming an activity; only standalone
        # callers wait here. Heartbeats and supervision keep running at startup.
        while not self.prepare():
            if not wait:raise RuntimeError('El navegador aún no está listo.')
            time.sleep(.05)
        group=self.available_group()
        host=self.hosts[group]
        token=secrets.token_hex(24)
        self._send(host,dict(type='register',activity=activity,token=token))
        self.leases[activity]=group
        host['last_used']=time.monotonic()
        return dict(SCRAPER_BROWSER_ENDPOINT=self._status(host)['endpoint'],SCRAPER_BROWSER_TOKEN=token,SCRAPER_DIRECT_PLAYWRIGHT='1')

    @staticmethod
    def _status(host):
        try:return json.loads(host['status'].read_text())
        except (OSError,ValueError):return {}

    def renderer_pids(self, activity):
        host=self.hosts.get(self.leases.get(activity))
        if not host or host['process'].poll() is not None:return set()
        return set(self._status(host).get('activities',{}).get(activity,[]))

    def pause(self, activity, paused):
        host=self.hosts.get(self.leases.get(activity))
        if host:
            try:self._send(host,dict(type='pause' if paused else 'resume',activity=activity))
            except (BrokenPipeError,OSError):pass

    def release(self, activity):
        group=self.leases.pop(activity,None)
        host=self.hosts.get(group)
        if host:
            host['last_used']=time.monotonic()
            try:self._send(host,dict(type='release',activity=activity))
            except (BrokenPipeError,OSError):pass

    def reap(self, pressure=False):
        leased=set(self.leases.values())
        for group,host in list(self.hosts.items()):
            if group not in leased and (pressure or time.monotonic()-host['last_used']>=self.idle_seconds):
                self._stop(host);del self.hosts[group]

    @staticmethod
    def _stop(host):
        process=host['process']
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.wait()
        # Clean up Chromium children even if the Node host crashed first.
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        if process.stdin:process.stdin.close()
        host['log'].close()
        host['status'].unlink(missing_ok=True)

    def close(self):
        for host in self.hosts.values():self._stop(host)
        self.hosts.clear();self.leases.clear()


def process_memory(pids):
    total=0
    for pid in pids:
        try:
            text=(Path('/proc')/str(pid)/'smaps_rollup').read_text()
            total+=next(int(line.split()[1]) for line in text.splitlines() if line.startswith('Pss:'))/1024
        except (OSError,ValueError,StopIteration):pass
    return total
