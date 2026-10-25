from src.persistent.monitor import quality, check
from src.persistent.store import Store
from src.common.scrapper import AuditStore


def test_quality_flags_empty_results_and_missing_product_price():
    assert quality(None)['warnings'] == ['sin_contenido_principal']
    result={'entities':[{'id':'p1','role':'product','parts':[{'class':'name','values':[{'text':'A'}]}]}]}
    assert quality(result)['incomplete_products'] == [{'entity':'p1','missing':['price']}]
    result['entities'][0]['parts'].append({'class':'price','values':[{'text':'12'}]})
    assert quality(result)['warnings'] == []


def test_monitor_observes_rate_errors_and_queue_without_mutating(tmp_path):
    store=Store(tmp_path/'store')
    seed=store.save_seed({'url':'https://example.org','max_urls_per_minute':2})
    store.save_settings({**store.settings(),'enabled':True})
    activity=store.claim(1)
    store.finish(activity,'failed request')
    before=store.activity(activity)
    report=check(store,AuditStore(tmp_path/'audit'),seed['id'])
    assert report['starts_last_minute']==1
    assert report['errors_last_10_minutes']==1
    assert report['counts']=={'retry_wait':1}
    assert 'errores_en_los_ultimos_10_minutos' in report['warnings']
    assert store.activity(activity)==before
