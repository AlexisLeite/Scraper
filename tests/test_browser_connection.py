"""Driver-free connection uses the same sync page API and isolated ownership."""
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from playwright.sync_api import Error

from src.persistent.browser_connection import SharedBrowserConnection
from src.persistent.browser_pool import BrowserPool


@pytest.fixture
def shared(tmp_path):
    pool=BrowserPool(tmp_path)
    try:
        yield pool,pool.allocate(1,'one'),pool.allocate(2,'two')
    finally:pool.close()


def test_direct_sync_api_and_cleanup(shared):
    pool,one,two=shared
    with SharedBrowserConnection(one['SCRAPER_BROWSER_ENDPOINT']) as b1:
        c1=b1.new_context()
        c1.add_cookies([dict(name='private',value='one',url='https://example.org')])
        page=c1.new_page()
        page.route('**/*',lambda route:route.fulfill(body='<main><h1>Hello</h1><details><summary>Question</summary>Answer</details><button onclick="this.textContent=\'Clicked\'">Click</button></main>',content_type='text/html'))
        page.goto('https://example.org')
        assert page.locator('h1').inner_text()=='Hello'
        page.get_by_role('button').click()
        assert page.get_by_role('button').inner_text()=='Clicked'
        assert 'Answer' in page.content()
        assert page.screenshot().startswith(b'\x89PNG')
        page.evaluate('localStorage.setItem("private","one")')
        # Two independent sync dispatchers cannot be nested in one thread.
        # A subprocess mirrors real activity ownership instead.
        code='''import sys
from src.persistent.browser_connection import SharedBrowserConnection
with SharedBrowserConnection(sys.argv[1]) as b:
 c=b.new_context();assert c.cookies()==[]
 p=c.new_page();p.route('**/*',lambda r:r.fulfill(body='<h1>Other</h1>',content_type='text/html'));p.goto('https://example.org')
 assert p.evaluate('localStorage.getItem("private")') is None
 assert p.locator('h1').inner_text()=='Other'
'''
        subprocess.run([sys.executable,'-c',code,two['SCRAPER_BROWSER_ENDPOINT']],check=True,timeout=20)
        assert page.get_by_role('button').inner_text()=='Clicked'
        b1.close()
    with SharedBrowserConnection(one['SCRAPER_BROWSER_ENDPOINT']) as b:
        assert b.contexts==[]
        page=b.new_page();assert page.evaluate('6*7')==42


def test_direct_disconnection_reports_error_without_hanging(shared):
    pool,one,_=shared
    with SharedBrowserConnection(one['SCRAPER_BROWSER_ENDPOINT']) as browser:
        page=browser.new_page()
        os.kill(pool.hosts[0]['process'].pid,signal.SIGKILL)
        with pytest.raises(Error,match='close|disconnect|Connection'):
            page.evaluate('1+1')


def test_direct_connection_failure_closes_loop():
    with pytest.raises(Exception):
        with SharedBrowserConnection('ws://127.0.0.1:1/no-browser'):
            pytest.fail('Unexpected connection')


def test_direct_workers_pause_and_cancel_independently(shared,tmp_path,monkeypatch):
    import json
    import time
    from src.persistent.worker import interrupt_over_limit
    from types import SimpleNamespace
    pool,one,two=shared
    code='''import json,os,sys
from src.persistent.browser_connection import SharedBrowserConnection
with SharedBrowserConnection(os.environ['SCRAPER_BROWSER_ENDPOINT']) as b:
 p=b.new_page();p.goto('data:text/html,<title>Timer</title>')
 p.evaluate('(token)=>{window.ticks=0;setInterval(()=>{ticks++;console.timeStamp(token)},100)}',os.environ['SCRAPER_BROWSER_TOKEN'])
 print('ready',flush=True)
 for line in sys.stdin:
  print(json.dumps(p.evaluate(line.strip())),flush=True)
'''
    processes=[]
    try:
        for env in [one,two]:
            p=subprocess.Popen([sys.executable,'-c',code],env={**os.environ,**env},stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,start_new_session=True)
            processes.append(p)
            assert p.stdout.readline().strip()=='ready'
        a,b=processes
        def query(p,expression):
            p.stdin.write(expression+'\n');p.stdin.flush()
            return json.loads(p.stdout.readline())
        deadline=time.monotonic()+10
        while not pool.renderer_pids('one') and time.monotonic()<deadline:time.sleep(.1)
        assert pool.renderer_pids('one')
        before=query(a,'ticks');other=query(b,'ticks');pool.pause('one',True)
        time.sleep(1.5)
        assert query(b,'ticks')>=other+10
        pool.pause('one',False)
        assert query(a,'ticks')-before<6
        failures=[]
        monkeypatch.setattr('src.persistent.worker.recover_audit',lambda *args:None)
        store=SimpleNamespace(finish=lambda *args,**kwargs:failures.append((args,kwargs)))
        assert interrupt_over_limit(store,a,'one',257,256,{a.pid})
        assert failures[0][1]['retryable'] is False
        pool.release('one')
        assert query(b,'21*2')==42
        assert b.poll() is None
    finally:
        for p in processes:
            if p.poll() is None:os.killpg(p.pid,signal.SIGKILL);p.wait()
            p.stdin.close();p.stdout.close()
