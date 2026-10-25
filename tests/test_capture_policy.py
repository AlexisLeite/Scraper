from contextlib import contextmanager
import pytest
from src.persistent.capture_policy import validate_destination, OutsideScopeRedirect
from src.persistent.store import Store
from src.persistent.worker import execute_activity
from src.common.scrapper import AuditStore
from src.common.scrapper.models import PageCapture

SEEDS=[{'url':'https://shop.test/catalog','allowed_domains':['shop.test'], 'allowed_url_patterns':[r'^https://shop[.]test/product/']}]


def test_redirect_must_match_an_origin_that_admitted_the_request():
    validate_destination(SEEDS,'https://shop.test/catalog','https://shop.test/product/42')
    with pytest.raises(OutsideScopeRedirect):
        validate_destination(SEEDS,'https://shop.test/product/42','https://shop.test/account?next=42')
    with pytest.raises(OutsideScopeRedirect):
        validate_destination(SEEDS+[{'url':'https://other.test','allowed_domains':['other.test']}],
                             'https://shop.test/product/42','https://other.test')


def test_login_redirect_keeps_capture_without_publishing_or_retry(tmp_path):
    store=Store(tmp_path/'store'); audit=AuditStore(tmp_path/'audit')
    store.save_settings({**store.settings(),'enabled':True})
    store.save_seed(SEEDS[0])
    activity=store.claim(1)
    @contextmanager
    def loader(url):
        yield PageCapture(url,'https://shop.test/account','Login','<main><form><input type="password"></form></main>')
    execute_activity(store,activity,audit,loader)
    a=store.activity(activity)
    assert a['status']=='error' and a['attempt']==1
    assert store.claim(1) is None
    visit=audit.get_visit(audit.list_visits(a['run_id'])[0]['id'])
    assert visit['status']=='error'
    assert visit['attempts'][0]['phase']=='access_blocked'
    assert 'password' in audit.read_html(visit['id'])
    assert not audit.list_events(a['run_id'])
