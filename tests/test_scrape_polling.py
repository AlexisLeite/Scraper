"""Browser regression for live history updates preserving the selected commercial view."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1] / 'tests/legacy/app'


def test_polling_preserves_commercial_view_until_selected_content_changes():
    state = {'stage': 0}
    visits = [dict(id=f'v{i}', depth=i-1, status='done', kind='page',
                   title=f'Page {i}', requested_uri=f'https://example.org/{i}',
                   discovery_order=i, page_type='product') for i in (1, 2)]
    run = dict(id='r', started_at='2026-10-03T12:00:00Z', finished_at=None,
               config={'initial_uris':['https://example.org/1'], 'max_sites':3,
                       'strategy':'src.common.scrapper.strategies.semantic.SemanticMetadataStrategy'},
               usage=None, costs=None)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script("localStorage.setItem('markup-studio:scrape:v1', JSON.stringify({jobId:'j'}))")

        def serve(route):
            path = route.request.url.removeprefix('http://studio.test')
            if path == '/commercial.js':
                route.fulfill(content_type='text/javascript', body='''
                  window.commercialRenders=0;
                  window.ScrapeCommercial={render(target,detail){
                    ++window.commercialRenders;
                    const view=document.createElement('details');
                    view.innerHTML='<summary>'+detail.title+'</summary><p>'+detail.result.version+'</p>';
                    target.append(view);
                  }};
                ''')
                return
            if path.startswith('/api/'):
                selected = visits[:1 if state['stage'] == 0 else 2]
                if path == '/api/health':
                    data = {'jev':True, 'codex':True}
                elif path == '/api/scrapes':
                    data = [run]
                elif path == '/api/scrapes/jobs/j':
                    data = {'status':'done' if state['stage'] == 2 else 'running', 'run_id':'r'}
                elif path == '/api/scrapes/r':
                    data = {'run':run, 'visits':selected, 'edges':[]}
                elif path.startswith('/api/scrapes/r/visits/'):
                    visit = next(v for v in visits if v['id'] == path.rsplit('/',1)[-1])
                    data = {**visit, 'result':{'entities':[{'id':'metadata', 'role':'metadata',
                            'selector':'head', 'purposes':['content'], 'parts':[{'class':'name',
                            'confidence':1, 'values':[{'text':visit['title']}]}]}],
                            'model':'semantic-v3', 'version':state['stage'] if visit['id']=='v2' else 0},
                            'outgoing_edges':[], 'attempts':[], 'model_calls':[], 'html_path':None,
                            'screenshot_path':None, 'usage':None, 'error':None}
                else:
                    raise AssertionError(path)
                route.fulfill(content_type='application/json', body=json.dumps(data))
                return
            file = ROOT / ('index.html' if path == '/' else path.lstrip('/'))
            if file.is_file():
                route.fulfill(path=str(file))
            else:
                route.abort()

        page.route('**/*', serve)
        page.goto('http://studio.test/')
        page.locator('[data-tab="scrape"]').click()
        page.wait_for_function('window.commercialRenders===1')
        assert 'estrategia Semántica' in page.locator('#scrape-run-meta').inner_text()
        assert 'Jev' not in page.locator('#scrape-entities').text_content()
        assert 'Campos extraídos (1)' in page.locator('#scrape-entities').text_content()
        assert 'Jev' not in page.locator('#scrape-page-summary').inner_text()
        page.locator('#scrape-commercial details').evaluate('(node)=>node.open=true')
        state['stage'] = 1
        page.wait_for_function("document.querySelectorAll('.scrape-visit').length===2")
        # The poll's detail request must also finish before checking preservation.
        page.wait_for_timeout(200)
        assert page.evaluate('window.commercialRenders') == 1
        assert page.locator('#scrape-commercial details').get_attribute('open') is not None
        assert page.locator('.scrape-visit.selected .scrape-visit-title').inner_text() == 'Page 1'
        page.locator('.scrape-visit').nth(1).click()
        page.wait_for_function('window.commercialRenders===2')
        assert page.locator('#scrape-commercial summary').inner_text() == 'Page 2'
        state['stage'] = 2
        page.wait_for_function('window.commercialRenders===3')
        assert page.locator('#scrape-commercial p').text_content() == '2'
        assert page.locator('.scrape-visit.selected .scrape-visit-title').inner_text() == 'Page 2'
        page.wait_for_function("document.querySelector('#scrape-progress-title').textContent==='Recorrido finalizado'")
        # Revisit A after B with A's first response still pending: only the latest
        # selection may render, even though an older response has the same visit ID.
        page.evaluate('''() => {
          const original=window.fetch;
          window.detailResponses=[];
          window.fetch=(url,options)=>{
            if (!String(url).includes('/visits/')) return original(url,options);
            return new Promise(resolve=>original(url,options).then(response=>
              window.detailResponses.push(()=>resolve(response))));
          };
        }''')
        for index in (0, 1, 0):
            page.locator('.scrape-visit').nth(index).click()
        page.wait_for_function('window.detailResponses.length===3')
        page.evaluate('window.detailResponses[2]()')
        page.wait_for_function('window.commercialRenders===4')
        page.evaluate('window.detailResponses[0]();window.detailResponses[1]()')
        page.wait_for_timeout(200)
        assert page.evaluate('window.commercialRenders') == 4
        assert page.locator('#scrape-commercial summary').inner_text() == 'Page 1'
        assert not errors
        browser.close()
