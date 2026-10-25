"""Read live service resources independently of the extraction supervisor loop."""
from pathlib import Path
import threading
import time


class WorkerResources:
    def __init__(self, proc=Path('/proc'), cgroups=Path('/sys/fs/cgroup'), clock=time.monotonic):
        self.proc=proc
        self.cgroups=cgroups
        self.clock=clock
        self.lock=threading.Lock()
        self.last=None
        self.cpu=None

    def read(self, pid):
        try:
            process=self.proc/str(int(pid))
            args=(process/'cmdline').read_bytes().split(b'\0')
            if b'src.persistent.worker' not in args or b'--activity' in args:
                return {}
            relative=next(line.split('::',1)[1] for line in (process/'cgroup').read_text().splitlines() if line.startswith('0::'))
            group=self.cgroups/relative.lstrip('/')
            if group.name!='scrapper-worker.service':return {}
            memory=int((group/'memory.current').read_text())/1024**2
            cpu=dict(line.split() for line in (group/'cpu.stat').read_text().splitlines())
            sample=(pid,self.clock(),int(cpu['usage_usec']))
            with self.lock:
                if self.last is None or self.last[0]!=pid:
                    self.last=sample;self.cpu=None
                elif sample[1]-self.last[1]>=1:
                    self.cpu=round(max(0,sample[2]-self.last[2])/(sample[1]-self.last[1])/10000,1)
                    self.last=sample
                result=dict(memory_mb=round(memory,1),sampled_at=time.time())
                if self.cpu is not None:result['cpu_used_percent']=self.cpu
                return result
        except (OSError,ValueError,TypeError,StopIteration,KeyError):
            return {}


worker_resources=WorkerResources()
