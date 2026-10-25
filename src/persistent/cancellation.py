"""Stop only worker process groups belonging to the activities being erased."""
import os
import signal
import time
from pathlib import Path


def cancel_workers(activity_ids, root):
    wanted=set(activity_ids)
    if not wanted:
        return
    # The caller holds the admission transaction. A process already claimed just
    # before the transaction may still be starting; scan through that launch gap.
    for _ in range(6):
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                args=(entry/'cmdline').read_bytes().decode().split('\0')
                if 'src.persistent.worker' not in args or '--activity' not in args or '--root' not in args:
                    continue
                if args[args.index('--activity')+1] not in wanted or Path(args[args.index('--root')+1]).resolve()!=Path(root).resolve():
                    continue
                pid=int(entry.name)
                if os.getpgid(pid)==pid:
                    os.killpg(pid,signal.SIGKILL)
            except (FileNotFoundError,ProcessLookupError,PermissionError,ValueError,IndexError):
                continue
        time.sleep(.1)
