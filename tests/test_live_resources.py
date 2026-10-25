from src.persistent.resources import WorkerResources


def test_live_memory_refreshes_without_heartbeat_and_cpu_has_stable_window(tmp_path):
    proc=tmp_path/'proc';process=proc/'123';process.mkdir(parents=True)
    process.joinpath('cmdline').write_bytes(b'python\0-m\0src.persistent.worker\0')
    process.joinpath('cgroup').write_text('0::/system.slice/scrapper-worker.service\n')
    cgroups=tmp_path/'cgroups';group=cgroups/'system.slice/scrapper-worker.service';group.mkdir(parents=True)
    group.joinpath('memory.current').write_text(str(1024**3))
    group.joinpath('cpu.stat').write_text('usage_usec 1000000\n')
    clock=[10.0];probe=WorkerResources(proc,cgroups,lambda:clock[0])
    assert probe.read(123)['memory_mb']==1024
    group.joinpath('memory.current').write_text(str(2*1024**3))
    clock[0]=12;group.joinpath('cpu.stat').write_text('usage_usec 3000000\n')
    value=probe.read(123)
    assert value['memory_mb']==2048 and value['cpu_used_percent']==100
    clock[0]=12.01
    assert probe.read(123)['cpu_used_percent']==100
    process.joinpath('cmdline').write_bytes(b'python\0unrelated-process\0')
    assert probe.read(123)=={}
