"""The replacement portal talks to a real isolated API; extraction calls are free."""
from pathlib import Path
import re
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect

from src.persistent.store import Store
from src.persistent.api import router
from src.common.scrapper import AuditStore

WEB=Path(__file__).resolve().parents[1]/'src/web/out'

@pytest.fixture
def portal(tmp_path):
    if not (WEB/'index.html').exists():
        pytest.skip('Build the Next portal first')
    store=Store(tmp_path/'service')
    seed=store.save_seed({'url':'https://example.org/','allowed_domains':['example.org']})
    for i in range(24):store.discover(seed['id'],f'https://example.org/products/shoes-{i}',1)
    app=FastAPI();app.include_router(router(store,AuditStore(tmp_path/'audit')))
    client=TestClient(app)
    with sync_playwright() as p:
        browser=p.chromium.launch()
        page=browser.new_page(viewport={'width':1440,'height':1100})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        def serve(route):
            parsed=urlsplit(route.request.url)
            if parsed.path.startswith('/api/'):
                response=client.request(route.request.method,parsed.path+('?' + parsed.query if parsed.query else ''),content=route.request.post_data,headers={'Content-Type':'application/json'})
                route.fulfill(status=response.status_code,body=response.content,content_type='application/json')
                return
            file=(WEB/('index.html' if parsed.path=='/' else parsed.path.lstrip('/'))).resolve()
            if file.is_relative_to(WEB.resolve()) and file.is_file():route.fulfill(path=str(file))
            else:route.abort()
        page.route('**/*',serve)
        yield page,store,errors
        browser.close()


def test_extract_single_page_results_reload_and_no_origin_created(portal,tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from src.common.scrapper import Scrapper, ScrapperConfig, PageCapture
    from src.server import main as server
    page,store,errors=portal
    origins_before=store.seeds()
    history=AuditStore(tmp_path/'extract-audit')
    captured=[]
    def capture(uri):
        captured.append(uri)
        return PageCapture(uri,uri,'Extracción puntual','<main><h1>Informe puntual</h1><p>Solo esta página.</p><a href="/elsewhere">Más</a></main>')
    def scraper(config,audit):
        return Scrapper(ScrapperConfig(strategy=config.strategy,max_sites=config.max_sites,
            max_hops=config.max_hops,max_threads=config.max_threads,uri_to_html=capture),audit=audit)
    monkeypatch.setattr(server,'_audit_store',history)
    monkeypatch.setattr(server,'Scrapper',scraper)
    client=TestClient(server.app)
    restart=[False]
    def serve(route):
        parsed=urlsplit(route.request.url)
        if restart[0] and '/jobs/' in parsed.path:
            route.fulfill(status=404,json={'detail':'Recorrido no encontrado en este proceso.'})
            return
        response=client.request(route.request.method,parsed.path,content=route.request.post_data,
            headers={'Content-Type':'application/json'})
        route.fulfill(status=response.status_code,body=response.content,content_type='application/json')
    page.route('**/api/extract',serve)
    page.route('**/api/scrapes/**',serve)
    page.goto('http://studio.test/')
    page.get_by_role('link',name='Extraer',exact=True).click()
    expect(page.get_by_role('heading',name='Extraer',exact=True)).to_be_visible()
    page.get_by_label('URL de la página',exact=True).fill('https://isolated.example/one')
    page.get_by_role('button',name='Extraer página',exact=True).click()
    expect(page.get_by_role('status')).to_contain_text('Extracción finalizada.',timeout=15000)
    expect(page.locator('.results')).to_contain_text('Solo esta página.')
    assert captured==['https://isolated.example/one']
    assert 'extract_run=' in page.url
    restart[0]=True
    page.reload()
    expect(page.locator('.results')).to_contain_text('Solo esta página.')
    expect(page.get_by_role('button',name='Extraer página',exact=True)).to_be_enabled()
    assert store.seeds()==origins_before
    assert captured==['https://isolated.example/one']
    assert not errors


def test_extract_error_allows_retry(portal):
    page,store,errors=portal
    page.route('**/api/extract',lambda route:route.fulfill(status=503,json={'detail':'Espera a que termine la extracción activa.'}))
    page.goto('http://studio.test/?view=extract')
    page.get_by_label('URL de la página',exact=True).fill('https://example.org/one')
    page.get_by_role('button',name='Extraer página',exact=True).click()
    expect(page.locator('.error-banner[role="alert"]')).to_contain_text('Espera a que termine la extracción activa.')
    expect(page.get_by_role('button',name='Extraer página',exact=True)).to_be_enabled()
    assert not errors


def test_optional_page_limit_saved_reloaded_and_removed(portal):
    page,store,errors=portal
    page.goto('http://studio.test/?view=seeds&edit=new')
    page.get_by_label('URL',exact=True).fill('https://limited.example/')
    field=page.get_by_label('Límite de páginas (opcional)',exact=False)
    expect(field).to_have_value('')
    field.fill('10')
    page.get_by_role('button',name='Guardar URL inicial',exact=True).click()
    expect(page.get_by_role('status')).to_contain_text('URL inicial guardada.')
    saved=next(s for s in store.seeds() if s['url']=='https://limited.example/')
    assert saved['max_pages']==10
    page.goto('http://studio.test/?view=seeds&edit='+saved['id'])
    expect(field).to_have_value('10')
    field.fill('')
    page.get_by_role('button',name='Guardar URL inicial',exact=True).click()
    expect(page.get_by_role('status')).to_contain_text('URL inicial guardada.')
    assert next(s for s in store.seeds() if s['id']==saved['id'])['max_pages'] is None
    assert not errors


def test_rule_editor_validates_individual_items_and_saves_only_selected_list(portal):
    page,store,errors=portal
    page.goto('http://studio.test/?view=seeds')
    page.get_by_role('link',name='Nueva URL inicial',exact=True).click()
    page.get_by_label('URL',exact=True).fill('https://new.example/')
    page.get_by_label('Lista blanca',exact=True).check()
    domain=page.get_by_label('Agregar a dominios')
    domain.fill('https://bad.example/path')
    page.locator('.rule-list').nth(0).get_by_role('button',name='Agregar',exact=True).click()
    expect(page.locator('#error-domain')).to_contain_text('Dominio inválido')
    domain.fill('NEW.EXAMPLE')
    page.locator('.rule-list').nth(0).get_by_role('button',name='Agregar',exact=True).click()
    expect(page.locator('.rule-list code')).to_have_text('new.example')
    regex=page.get_by_label('Agregar a expresiones regulares')
    regex.fill('[broken')
    page.locator('.rule-list').nth(1).get_by_role('button',name='Agregar',exact=True).click()
    expect(page.locator('#error-regex')).to_contain_text('Expresión regular inválida')
    expect(page.get_by_role('button',name='Guardar URL inicial',exact=True)).to_be_disabled()
    regex.fill('(?i)/products/')
    page.locator('.rule-list').nth(1).get_by_role('button',name='Agregar',exact=True).click()
    expect(page.locator('.rule-list code')).to_have_count(2)
    page.get_by_label('Lista negra',exact=True).check()
    expect(page.locator('.rule-list code')).to_have_text('new.example')
    regex.fill('/login')
    page.locator('.rule-list').nth(1).get_by_role('button',name='Agregar',exact=True).click()
    expect(page.locator('.rule-list code')).to_have_text(['new.example','/login'])
    page.get_by_role('button',name='Guardar URL inicial',exact=True).click()
    expect(page.get_by_role('status')).to_contain_text('URL inicial guardada.')
    expect(page.get_by_label('URL',exact=True)).to_have_count(0)
    saved=next(s for s in store.seeds() if s['url']=='https://new.example/')
    assert saved['rule_mode']=='black'
    assert saved['allowed_domains']==['new.example'] and saved['allowed_url_patterns']==[]
    assert saved['blocked_url_patterns']==['/login']
    assert not errors


def test_domain_pagination_fuzzy_search_and_latest_activity_link(portal):
    page,store,errors=portal
    page.goto('http://studio.test/?view=domains')
    page.get_by_role('link',name='example.org',exact=True).click()
    expect(page.locator('.pager')).to_contain_text('25 resultados')
    page.get_by_role('link',name='Siguiente',exact=True).click()
    expect(page.locator('.pager')).to_contain_text('2 / 2')
    page.get_by_label('Buscar URL').fill('shos')
    expect(page.locator('.pager')).to_contain_text('24 resultados')
    page.get_by_role('link',name='https://example.org/products/shoes-0',exact=True).click()
    expect(page.get_by_role('heading',name='Actividad de extracción')).to_be_visible()
    assert '?activity=' in page.url
    expect(page.locator('.timeline')).to_contain_text('En cola')
    page.get_by_role('button',name='Solicitar re-scrap',exact=True).click()
    assert len(store.activities()['items'])==20 # fixed pagination
    assert not errors


def test_global_config_fifty_threads_survives_reload(portal):
    page,store,errors=portal
    page.goto('http://studio.test/?view=settings')
    page.get_by_label('Threads simultáneos',exact=True).fill('50')
    page.get_by_label('Tiempo entre reintentos (segundos)',exact=True).fill('45')
    page.get_by_role('button',name='Guardar configuración',exact=True).click()
    expect(page.get_by_role('status')).to_contain_text('Configuración guardada')
    page.reload()
    expect(page.get_by_label('Threads simultáneos',exact=True)).to_have_value('50')
    assert store.settings()['retry_delay']==45
    assert not errors


def test_narrow_window_does_not_block_portal(portal):
    page,store,errors=portal
    page.set_viewport_size({'width':390,'height':844})
    page.goto('http://studio.test/')
    expect(page.locator('.desktop-required')).to_have_count(0)
    expect(page.locator('.shell')).to_be_visible()
    assert not errors


def test_seed_pencil_opens_only_that_editor_and_regex_results(portal):
    page,store,errors=portal
    for i in range(21):store.save_seed({'url':f'https://other.example/{i}','allowed_domains':['other.example']})
    page.goto('http://studio.test/?view=seeds')
    expect(page.locator('.seed-list tbody tr')).to_have_count(20)
    expect(page.get_by_label('URL',exact=True)).to_have_count(0)
    page.get_by_role('link',name='Siguiente',exact=True).click()
    expect(page.locator('.seed-list tbody tr')).to_have_count(2)
    page.get_by_role('link',name='Anterior',exact=True).click()
    page.get_by_role('link',name='Editar https://example.org/',exact=True).click()
    expect(page.locator('.seed-list')).to_have_count(0)
    expect(page.get_by_label('URL',exact=True)).to_have_value('https://example.org/')
    page.get_by_role('button',name='Cargar URL conocidas',exact=True).click()
    expect(page.get_by_label('URL de prueba · una por línea',exact=True)).to_have_value(re.compile(r'https://example[.]org/'))
    page.get_by_label('Patrón a probar',exact=True).fill('/products/')
    page.get_by_label('URL de prueba · una por línea',exact=True).fill('https://example.org/products/42\nhttps://example.org/about')
    page.get_by_role('button',name='Probar regex',exact=True).click()
    expect(page.locator('.regex-tester [role=status]')).to_contain_text('1 coinciden · 1 no coinciden')
    expect(page.locator('.regex-tester tbody tr').nth(0)).to_contain_text('Coincide')
    expect(page.locator('.regex-tester tbody tr').nth(1)).to_contain_text('No coincide')
    page.get_by_role('link',name='Volver al listado',exact=True).click()
    expect(page.locator('.seed-list')).to_be_visible()
    assert not errors


def test_navigation_eye_and_pagination_scroll_with_reduced_motion(portal):
    page,store,errors=portal
    for i in range(21):store.save_seed({'url':f'https://paging.example/{i}','allowed_domains':['paging.example']})
    page.emulate_media(reduced_motion='reduce')
    page.goto('http://studio.test/?view=seeds')
    expect(page.locator('nav a')).to_have_text(['◉Análisis','↓Extraer','≡Actividad','◎Dominios y páginas','↗Configuración','⚙Historial','!Fallidas','⚑Dominios marcados'])
    page.get_by_role('link',name='Siguiente',exact=True).scroll_into_view_if_needed()
    assert page.evaluate('window.scrollY')>0
    page.get_by_role('link',name='Siguiente',exact=True).click()
    expect(page.locator('.seed-list tbody tr')).to_have_count(2)
    page.wait_for_function('window.scrollY===0')
    assert page.locator('.section-transition').first.evaluate('e=>getComputedStyle(e).animationName')=='none'
    page.get_by_role('link',name='Anterior',exact=True).click()
    page.get_by_role('link',name='Ver páginas de https://example.org/',exact=True).click()
    expect(page.get_by_role('heading',name='Páginas alcanzadas',exact=True)).to_be_visible()
    expect(page.get_by_role('heading',name='Actividad de extracción',exact=True)).to_have_count(0)
    expect(page.get_by_label('Buscar URL',exact=True)).to_be_visible()
    page.get_by_role('link',name='Actividad',exact=True).click()
    expect(page.get_by_role('heading',name='Actividad por slot',exact=True)).to_be_visible()
    assert not errors


def test_accordion_animates_both_directions_and_survives_polling(portal):
    from contextlib import contextmanager
    from src.persistent.worker import execute_activity
    from src.common.scrapper.models import PageCapture
    page,store,errors=portal
    store.save_settings({**store.settings(),'enabled':True})
    activity=store.claim(1)
    @contextmanager
    def loader(url):
        yield PageCapture(url,url,'Test product','''<html><head><title>Test product</title>
        <script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Test product","description":"A useful product","offers":{"@type":"Offer","price":"20","priceCurrency":"USD"}}</script></head><body><main><h1>Product</h1></main></body></html>''')
    execute_activity(store,activity,AuditStore(store.root.parent/'audit'),loader)
    page.goto('http://studio.test/?activity='+activity)
    accordion=page.locator('.results > .animated-details').first
    expect(accordion).to_have_attribute('open','')
    expanded=accordion.bounding_box()['height']
    accordion.locator(':scope > summary').click()
    measured=accordion.evaluate('''async el=>{const a=el.getAnimations()[0]; if(!a)return null; a.pause();a.currentTime=120;await new Promise(requestAnimationFrame);const height=el.getBoundingClientRect().height;a.finish();return height;}''')
    assert measured is not None and 50<measured<expanded
    expect(accordion).not_to_have_attribute('open','')
    collapsed=accordion.bounding_box()['height']
    accordion.locator(':scope > summary').press('Enter')
    measured=accordion.evaluate('''async el=>{const a=el.getAnimations()[0];if(!a)return null;a.pause();a.currentTime=120;await new Promise(requestAnimationFrame);const height=el.getBoundingClientRect().height;a.finish();return height;}''')
    assert measured is not None and collapsed<measured<expanded
    expect(accordion).to_have_attribute('open','')
    # Refreshes must preserve the user's accordion choice.
    accordion.locator(':scope > summary').click()
    page.wait_for_timeout(2400)
    expect(accordion).not_to_have_attribute('open','')
    assert not errors


def test_failed_activity_review_and_retry(portal):
    page,store,errors=portal
    store.save_settings({**store.settings(),'enabled':True})
    activity=store.claim(1);store.finish(activity,'Superó el umbral individual.',retryable=False)
    page.goto('http://studio.test/?view=failures')
    expect(page.get_by_role('heading',name='Actividades fallidas',exact=True)).to_be_visible()
    expect(page.locator('tbody tr')).to_have_count(1)
    expect(page.locator('tbody')).to_contain_text('Superó el umbral individual.')
    page.get_by_role('link',name='Ver actividad',exact=True).click()
    expect(page.get_by_role('heading',name='Actividad de extracción')).to_be_visible()
    page.get_by_role('button',name='Reintentar',exact=True).click()
    expect(page.locator('.activity-meta .status')).to_have_text('En cola')
    assert store.activity(activity)['status']=='queued'
    assert store.activity(activity)['errors']
    assert not errors


def test_activity_original_link_and_readable_questions(portal):
    from contextlib import contextmanager
    from src.persistent.worker import execute_activity
    from src.common.scrapper.models import PageCapture
    page,store,errors=portal
    store.save_settings({**store.settings(),'enabled':True})
    activity=store.claim(1)
    @contextmanager
    def loader(url):
        yield PageCapture(url,url,'Questions','''<html><head><title>Questions</title></head><body>
        <details><summary>How to apply?</summary><div>Apply a thin layer.</div></details>
        <script type="application/ld+json">{"@context":"https://schema.org","@type":"Question","name":"How to apply?","acceptedAnswer":{"@type":"Answer","text":"Apply a thin layer."}}</script></body></html>''')
    execute_activity(store,activity,AuditStore(store.root.parent/'audit'),loader)
    page.goto('http://studio.test/?activity='+activity)
    link=page.locator('.activity-original-url a')
    expect(link).to_have_attribute('href','https://example.org/')
    expect(link).to_have_attribute('target','_blank')
    expect(page.locator('.question-card')).to_have_count(1)
    expect(page.locator('.question-card h3')).to_have_text('How to apply?')
    expect(page.locator('.question-card .full-text')).to_have_text('Apply a thin layer.')
    audit=page.locator('.results > details').filter(has=page.locator('summary',has_text='Auditoría y evidencia técnica'))
    expect(audit).not_to_have_attribute('open','')
    assert not errors


def test_retry_button_in_failed_list(portal):
    page,store,errors=portal
    store.save_settings({**store.settings(),'enabled':True})
    activity=store.claim(1);store.finish(activity,'Historical failure',retryable=False)
    page.goto('http://studio.test/?view=failures')
    page.locator('tbody').get_by_role('button',name='Reintentar',exact=True).click()
    expect(page.get_by_role('heading',name='Actividad de extracción')).to_be_visible()
    assert activity in page.url
    assert store.activity(activity)['status']=='queued'
    assert store.activity(activity)['errors']
    assert not errors


def test_origin_routes_back_forward_reload_and_counts(portal):
    page,store,errors=portal
    for i in range(21):store.save_seed({'url':f'https://route.example/{i}','allowed_domains':['route.example']})
    page.goto('http://studio.test/?view=seeds&page=2')
    expect(page.locator('.pager')).to_contain_text('2 / 2')
    expect(page.locator('th',has_text='Páginas alcanzadas')).to_have_count(1)
    expect(page.get_by_role('columnheader',name='Lista',exact=True)).to_have_count(0)
    link=page.locator('a[title="Editar URL inicial"]').first
    href=link.get_attribute('href')
    assert 'edit=' in href and 'page=2' in href
    link.click();expect(page.get_by_label('URL',exact=True)).to_be_visible()
    page.reload();expect(page.get_by_label('URL',exact=True)).to_be_visible()
    page.go_back();expect(page.locator('.pager')).to_contain_text('2 / 2')
    page.go_forward();expect(page.get_by_label('URL',exact=True)).to_be_visible()
    page.get_by_role('link',name='Volver al listado',exact=True).click()
    page.get_by_role('link',name='Anterior',exact=True).click()
    row=page.locator('.seed-list tbody tr').filter(has_text='https://example.org/').first
    expect(row.locator('td').nth(2)).to_have_text('25')
    row.locator('a[title="Ver páginas alcanzadas"]').click()
    assert 'origin=' in page.url
    page.get_by_label('Buscar URL',exact=True).fill('shos')
    expect(page.locator('.pager')).to_contain_text('24 resultados')
    assert 'q=shos' in page.url
    page.get_by_role('link',name='Siguiente',exact=True).click()
    expect(page.locator('.pager')).to_contain_text('2 / 2')
    assert 'items_page=2' in page.url
    page.locator('tbody .text-link').first.click()
    expect(page.get_by_role('heading',name='Actividad de extracción')).to_be_visible()
    page.go_back()
    expect(page.get_by_label('Buscar URL',exact=True)).to_have_value('shos')
    expect(page.locator('.pager')).to_contain_text('2 / 2')
    assert not errors


def test_rescrape_updates_row_without_navigation(portal):
    page,store,errors=portal
    with store.db() as db:
        db.execute("UPDATE activities SET status='done',stage='done',finished=1")
    page.goto('http://studio.test/?view=domains&domain=example.org&items_page=1&q=shoes-0')
    row=page.locator('tbody tr').filter(has=page.get_by_role('link',name='https://example.org/products/shoes-0',exact=True))
    expect(row.locator('.status')).to_have_text('Finalizada')
    before=page.url
    row.get_by_role('button',name='Re-scrap',exact=True).click()
    expect(row.locator('.status')).to_have_text('En cola')
    assert page.url==before
    assert not errors


def test_rescrape_detail_preserves_historical_activity_url(portal):
    page,store,errors=portal
    activity=store.activities()['items'][0]
    with store.db() as db:
        db.execute("UPDATE activities SET status='done',stage='done',finished=1 WHERE id=?",(activity['id'],))
    page.goto('http://studio.test/?activity='+activity['id'])
    expect(page.locator('.activity-meta .status')).to_have_text('Finalizada')
    before=page.url
    page.get_by_role('button',name='Solicitar re-scrap',exact=True).click()
    confirmation=page.get_by_role('status')
    expect(confirmation).to_contain_text('Re-scrap solicitado.')
    expect(confirmation.locator('.status')).to_have_text('En cola')
    assert page.url==before
    expect(page.locator('.activity-meta .status')).to_have_text('Finalizada')
    link=confirmation.get_by_role('link',name='Ver actividad solicitada')
    assert activity['id'] not in link.get_attribute('href')
    assert not errors


def test_existing_regex_can_be_edited_validated_cancelled_and_saved(portal):
    page,store,errors=portal
    seed=store.seeds()[0]
    store.save_seed({**{k:v for k,v in seed.items() if k!='id'},'allowed_url_patterns':['/products/','/other/']},seed['id'])
    page.goto('http://studio.test/?view=seeds&edit='+seed['id'])
    rules=page.locator('.rule-list').nth(1)
    rules.get_by_role('button',name='Editar /products/',exact=True).click()
    field=page.get_by_label('Editar en expresiones regulares')
    field.fill('[broken')
    rules.get_by_role('button',name='Aplicar cambio').click()
    expect(rules.get_by_role('alert')).to_contain_text('Expresión regular inválida')
    expect(rules.locator('li').first.get_by_label('Editar en expresiones regulares')).to_have_value('[broken')
    field.fill('/other/')
    rules.get_by_role('button',name='Aplicar cambio').click()
    expect(rules.get_by_role('alert')).to_contain_text('ya está en la lista')
    field.fill('')
    expect(page.get_by_role('button',name='Guardar URL inicial',exact=True)).to_be_disabled()
    rules.get_by_role('button',name='Cancelar edición').click()
    expect(rules.locator('code').first).to_have_text('/products/')
    rules.get_by_role('button',name='Editar /products/',exact=True).click()
    field.fill('/catalog/.*')
    rules.get_by_role('button',name='Aplicar cambio').click()
    expect(rules.locator('code')).to_have_text(['/catalog/.*','/other/'])
    page.get_by_role('button',name='Guardar URL inicial',exact=True).click()
    expect(page.get_by_role('status')).to_contain_text('URL inicial guardada.')
    assert store.seeds()[0]['allowed_url_patterns']==['/catalog/.*','/other/']
    assert not errors


def test_seed_definition_external_link_and_delete_history(portal):
    page,store,errors=portal
    seed=store.seeds()[0]
    page.goto('http://studio.test/?view=seeds')
    link=page.get_by_role('link',name=seed['url'],exact=True)
    expect(link).to_have_attribute('href',re.compile('edit='+seed['id']))
    external=page.get_by_role('link',name='Abrir sitio externo '+seed['url'],exact=True)
    expect(external).to_have_attribute('target','_blank')
    expect(external).to_have_attribute('href',seed['url'])
    page.get_by_role('button',name='Eliminar historial de '+seed['url'],exact=True).click()
    expect(page.get_by_role('dialog',name='Eliminar historial de exploración')).to_be_visible()
    page.get_by_role('button',name='Confirmar eliminación del historial',exact=True).click()
    expect(page.get_by_role('status')).to_contain_text('Historial eliminado.')
    assert store.pages(seed_id=seed['id'])['total']==0
    assert not store.seeds()[0]['enabled']
    assert page.url=='http://studio.test/?view=seeds'
    expect(page.get_by_role('dialog')).not_to_be_visible()
    page.get_by_role('button',name='Iniciar exploración de '+seed['url'],exact=True).click()
    expect(page.get_by_role('status')).to_contain_text('Exploración encolada.')
    assert store.seeds()[0]['enabled']
    assert store.pages(seed_id=seed['id'])['total']==1
    assert store.activities()['items'][0]['status']=='queued'
    assert not errors


def test_seed_pause_and_play_in_list(portal):
    page,store,errors=portal
    seed=store.seeds()[0]
    page.goto('http://studio.test/?view=seeds')
    pause=page.get_by_role('button',name='Pausar exploración de '+seed['url'],exact=True)
    pause.click()
    expect(pause).to_have_count(0)
    assert not store.seeds()[0]['enabled']
    page.get_by_role('button',name='Iniciar exploración de '+seed['url'],exact=True).click()
    expect(pause).to_be_enabled()
    expect(page.get_by_role('button',name='Iniciar exploración de '+seed['url'],exact=True)).to_have_count(0)
    assert store.seeds()[0]['enabled']
    assert page.url=='http://studio.test/?view=seeds'
    assert not errors


def test_dashboard_hourly_lines_without_workers_section(portal):
    page,store,errors=portal
    page.goto('http://studio.test/?view=dashboard')
    expect(page.locator('.chart-series')).to_have_count(3)
    expect(page.get_by_role('heading',name='Workers en vivo')).to_have_count(0)
    assert not errors


def test_slot_progress_animates_stage_changes(portal):
    page,store,errors=portal
    store.save_settings({**store.settings(),'enabled':True})
    activity=store.claim(1)
    page.goto('http://studio.test/?view=active')
    bar=page.get_by_role('progressbar').first
    expect(bar).to_have_attribute('aria-valuenow','10')
    fill=bar.locator('.progress-fill')
    width=bar.bounding_box()['width']
    store.stage(activity,'saving')
    expect(bar).to_have_attribute('aria-valuenow','90')
    page.wait_for_timeout(100)
    intermediate=fill.bounding_box()['width']
    assert width*.1<intermediate<width*.9
    page.wait_for_timeout(650)
    assert abs(fill.bounding_box()['width']-width*.9)<2
    assert not errors


def test_activity_links_all_origins_to_definition(portal):
    page,store,errors=portal
    first=store.seeds()[0]
    second=store.save_seed({'url':'https://example.org/second','allowed_domains':['example.org']})
    activity=store.discover(first['id'],'https://example.org/shared',1)
    page.goto('http://studio.test/?activity='+activity)
    page.get_by_text('Procedencia del enlace',exact=False).first.click()
    origins=page.locator('.activity-origins')
    expect(origins.get_by_role('link')).to_have_count(2)
    origins.get_by_role('link',name=second['url'],exact=True).click()
    expect(page.get_by_label('URL',exact=True)).to_have_value(second['url'])
    page.go_back()
    expect(page.get_by_role('heading',name='Actividad de extracción')).to_be_visible()
    assert not errors


def test_chart_popups_and_smooth_lines(portal):
    page,store,errors=portal
    page.goto('http://studio.test/?view=dashboard')
    chart=page.locator('.hourly-chart')
    expect(chart.locator('.chart-series path')).to_have_count(3)
    expect(chart.locator('circle')).to_have_count(0)
    chart.locator('rect').last.hover()
    popup=chart.get_by_role('tooltip')
    expect(popup).to_contain_text('Exploradas')
    expect(popup).to_contain_text('Extraídas')
    expect(popup).to_contain_text('Errores')
    page.get_by_role('heading',name='Análisis',exact=True).hover()
    expect(popup).to_have_count(0)
    daily=page.locator('.chart').nth(1)
    daily.locator('.bar-column').first.focus()
    expect(daily.get_by_role('tooltip')).to_contain_text('Extraídas')
    page.keyboard.press('Escape')
    expect(daily.get_by_role('tooltip')).to_have_count(0)
    assert not errors


def test_non_json_api_error_is_readable(portal):
    page,store,errors=portal
    page.route('**/api/service/seeds?*',lambda route:route.fulfill(status=500,body='Internal Server Error',content_type='text/plain'))
    page.goto('http://studio.test/?view=seeds')
    expect(page.get_by_role('alert').filter(has_text='No se pudo completar la solicitud (HTTP 500).')).to_be_visible()
    expect(page.get_by_text("Unexpected token",exact=False)).to_have_count(0)
    assert not errors


def test_modal_backdrop_exit_animation_and_stable_toggle(portal):
    page,store,errors=portal
    page.goto('http://studio.test/?view=seeds')
    toggle=page.locator('.seed-toggle').first
    before=toggle.bounding_box()
    toggle.click()
    expect(toggle).to_have_text('▶ Iniciar')
    assert toggle.bounding_box()['width']==before['width']
    page.get_by_role('button',name='Eliminar historial de',exact=False).first.click()
    dialog=page.get_by_role('dialog')
    expect(dialog).to_be_visible()
    dialog.evaluate('''el=>{const original=el.animate.bind(el);el.animate=(frames,options)=>{const animation=original(frames,options);animation.pause();window.modalExitAnimation=animation;return animation;};}''')
    page.mouse.click(10,10)
    page.evaluate('window.modalExitAnimation.currentTime=80')
    opacity=float(dialog.evaluate('(el)=>getComputedStyle(el).opacity'))
    assert 0<opacity<1
    page.evaluate('window.modalExitAnimation.finish()')
    expect(dialog).not_to_be_visible()
    assert store.pages()['total']==25
    assert not errors


def test_busy_slots_are_displayed_before_idle(portal):
    page,store,errors=portal
    store.save_settings({**store.settings(),'enabled':True,'threads':5})
    store.claim(4)
    page.goto('http://studio.test/?view=active')
    expect(page.locator('.slot-table tbody tr').first.locator('td').first).to_have_text('#04')
    expect(page.locator('.slot-table tbody tr').nth(1).locator('td').first).to_have_text('#01')
    assert not errors


def test_origin_status_filter_restores_on_back(portal):
    page,store,errors=portal
    store.save_settings({**store.settings(),'enabled':True})
    activity=store.claim(1)
    store.finish(activity)
    seed=store.seeds()[0]
    page.goto(f'http://studio.test/?view=seeds&origin={seed["id"]}&items_page=2')
    page.get_by_label('Estado de las páginas').select_option('done')
    expect(page.locator('tbody tr')).to_have_count(1)
    assert 'page_status=done' in page.url and 'items_page=1' in page.url
    page.locator('tbody .text-link').first.click()
    expect(page.get_by_role('heading',name='Actividad de extracción')).to_be_visible()
    page.go_back()
    expect(page.get_by_label('Estado de las páginas')).to_have_value('done')
    expect(page.locator('tbody tr')).to_have_count(1)
    expect(page.locator('.origin-actions .seed-toggle')).to_be_visible()
    expect(page.get_by_text('Historial de este origen',exact=True)).to_have_count(0)
    assert not errors


def test_live_activity_counters(portal):
    page,store,errors=portal
    store.save_settings({**store.settings(),'enabled':True})
    first=store.claim(1);store.finish(first)
    second=store.claim(2);store.finish(second,error='Failure')
    store.claim(3)
    expected=store.dashboard()['activity_counters']
    page.goto('http://studio.test/?view=active')
    for key,title in [('pages_per_hour','Páginas por hora'),('queued','Encoladas'),('running','En curso'),('errors_per_hour','Errores por hora')]:
        item=page.locator('.activity-counters>div').filter(has=page.locator('span',has_text=re.compile('^'+title+'$')))
        expect(item.locator('strong')).to_have_text(str(expected[key]))
    assert not errors
