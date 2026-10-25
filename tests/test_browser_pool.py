"""Real Chromium checks, local pages only; no extraction or Jev calls."""
import os
import signal
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

from src.persistent.browser_pool import BrowserPool, process_memory


def test_shared_browser_isolation_pause_and_accounting(tmp_path):
    pool=BrowserPool(tmp_path,idle_seconds=60)
    try:
        one=pool.allocate(1,'one');two=pool.allocate(2,'two')
        assert one['SCRAPER_BROWSER_ENDPOINT']==two['SCRAPER_BROWSER_ENDPOINT']
        with sync_playwright() as p:
            b1=p.chromium.connect(one['SCRAPER_BROWSER_ENDPOINT'])
            b2=p.chromium.connect(two['SCRAPER_BROWSER_ENDPOINT'])
            c1=b1.new_context();c2=b2.new_context()
            c1.add_cookies([dict(name='private',value='one',url='https://example.org')])
            assert c2.cookies()==[]
            pages=[c1.new_page(),c2.new_page()]
            for page,env in zip(pages,[one,two]):
                page.goto('data:text/html,<title>Local test</title>')
                page.evaluate('(token)=>{window.ticks=0;setInterval(()=>{ticks++;console.timeStamp(token)},100)}',env['SCRAPER_BROWSER_TOKEN'])
            deadline=time.monotonic()+15
            while not all(pool.renderer_pids(a) for a in ['one','two']) and time.monotonic()<deadline:
                pages[1].wait_for_timeout(100)
            pids1,pids2=pool.renderer_pids('one'),pool.renderer_pids('two')
            assert pids1 and pids2 and not pids1&pids2
            assert process_memory(pids1)>0
            before=pages[0].evaluate('ticks');other=pages[1].evaluate('ticks')
            pool.pause('one',True)
            pages[1].wait_for_timeout(1800)
            assert pages[1].evaluate('ticks')>=other+12
            pool.pause('one',False)
            after=pages[0].evaluate('ticks')
            assert after-before<8
            pages[0].wait_for_timeout(400)
            assert pages[0].evaluate('ticks')>after
            b1.close();pool.release('one')
            assert pages[1].evaluate('2+2')==4
            pool.reap(pressure=True)
            assert len(pool.hosts)==1  # A live context must never be reclaimed.
            again=pool.allocate(50,'three')
            assert again['SCRAPER_BROWSER_ENDPOINT']==two['SCRAPER_BROWSER_ENDPOINT']
            pool.release('three');b2.close();pool.release('two')
        pool.reap()
        assert pool.hosts  # Warm host is retained without pressure.
        pool.reap(pressure=True)
        assert not pool.hosts
    finally:
        pool.close()


def test_killed_client_does_not_close_other_activity(tmp_path):
    pool=BrowserPool(tmp_path)
    child=None
    try:
        one=pool.allocate(1,'one');two=pool.allocate(2,'two')
        code='''import os,time
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
 b=p.chromium.connect(os.environ['SCRAPER_BROWSER_ENDPOINT'])
 page=b.new_page();page.goto('data:text/html,<title>Child</title>')
 print('ready',flush=True)
 time.sleep(60)
'''
        child=subprocess.Popen([sys.executable,'-c',code],env={**os.environ,**one},stdout=subprocess.PIPE,text=True,start_new_session=True)
        assert child.stdout.readline().strip()=='ready'
        with sync_playwright() as p:
            browser=p.chromium.connect(two['SCRAPER_BROWSER_ENDPOINT'])
            page=browser.new_page()
            cdp=browser.new_browser_cdp_session()
            count=lambda:len([t for t in cdp.send('Target.getTargets')['targetInfos'] if t['type']=='page'])
            assert count()==2
            os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=5)
            pool.release('one')
            deadline=time.monotonic()+5
            while count()!=1 and time.monotonic()<deadline:page.wait_for_timeout(100)
            assert count()==1
            assert page.evaluate('21*2')==42
            browser.close();pool.release('two')
    finally:
        if child and child.poll() is None:
            os.killpg(child.pid,signal.SIGKILL);child.wait()
        pool.close()


def test_prepare_does_not_wait_for_browser_startup(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import io
    now=[10.0]
    process=SimpleNamespace(poll=lambda:None,stdin=io.StringIO())
    monkeypatch.setattr('src.persistent.browser_pool.subprocess.Popen',lambda *a,**k:process)
    monkeypatch.setattr('src.persistent.browser_pool.time.monotonic',lambda:now[0])
    pool=BrowserPool(tmp_path)
    try:
        # A pending host must return to the supervisor rather than sleep until
        # it writes its status. No activity lease exists before readiness.
        assert pool.prepare() is False
        assert pool.prepare() is False
        assert len(pool.hosts)==1 and pool.leases=={}
        pool.hosts[0]['status'].write_text('{"endpoint":"ws://127.0.0.1/test"}')
        assert pool.prepare() is True
        assert pool.allocate(1,'one',wait=False)['SCRAPER_BROWSER_ENDPOINT']=='ws://127.0.0.1/test'
    finally:
        pool.hosts[0]['log'].close()
