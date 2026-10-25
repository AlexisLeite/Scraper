"""Request a bounded semantic crawl, then independently inspect its seeds in Chromium.

No model calls. Production rules never receive this site list or its expectations.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from src.server import main as server
from src.common.scrapper import AuditStore
from src.common.uri_to_html import loaded_page

DEFAULT_URLS = [
    'https://books.toscrape.com/',
    'https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/article',
    'https://www.python.org/',
    'https://sibarirepublic.com/producto/long-lasting-hydration-calming-cream/',
    'https://www.nivea.es/productos/nivea-creme-250-ml-40058088010530244.html',
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iteration', type=int, required=True, choices=range(1, 11))
    parser.add_argument('--url', action='append')
    parser.add_argument('--max-sites', type=int, default=5)
    parser.add_argument('--max-hops', type=int, default=0)
    args = parser.parse_args()
    root = Path('data/semantic-regression') / f'iteration-{args.iteration}'
    root.mkdir(parents=True, exist_ok=True)
    # Never overwrite evidence of an earlier iteration.
    report_path = root / 'report.json'
    if report_path.exists():
        raise SystemExit('Esta iteración ya tiene informe; usa el siguiente número.')
    server._audit_store = AuditStore()
    client = TestClient(server.app)
    urls = args.url or DEFAULT_URLS
    response = client.post('/api/scrapes', json={'urls': urls, 'strategy': 'semantic',
        'max_sites': args.max_sites, 'max_hops': args.max_hops})
    response.raise_for_status()
    job_id = response.json()['job_id']
    print(f'Iteración {args.iteration}: scrap de {len(urls)} semillas, máximo {args.max_sites} visitas', flush=True)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        job = client.get(f'/api/scrapes/jobs/{job_id}').json()
        if job['status'] != 'running':
            break
        time.sleep(.25)
    else:
        raise SystemExit('El scrap excedió cinco minutos; consulta su histórico.')
    if job['status'] != 'done':
        raise SystemExit(str(job))
    run_id = job['run_id']
    run = server._audit_store.get_run(run_id)
    visits = server._audit_store.list_visits(run_id)
    report = {'iteration': args.iteration, 'timestamp': datetime.now(timezone.utc).isoformat(),
        'run_id': run_id, 'request': {'urls': urls, 'max_sites': args.max_sites, 'max_hops': args.max_hops},
        'summary': job['summary'], 'costs': run['costs'], 'usage': run['usage'], 'pages': []}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    for index, visit in enumerate(visits):
        detail = server._audit_store.get_visit(visit['id'])
        result = detail.get('result') or {}
        entities = result.get('entities', [])
        fields = [dict(part, entity=entity['id'], source=entity.get('attributes', {}).get('source'))
                  for entity in entities for part in entity.get('parts', [])]
        page_report = {'visit_id': visit['id'], 'url': visit['requested_uri'],
            'status': visit['status'], 'error': visit['error'], 'page_type': result.get('page_type'),
            'roles': dict(Counter(entity['role'] for entity in entities)),
            'fields': len(fields), 'coverage': result.get('classification', {}).get('coverage'),
            'costs': detail['costs']}
        report['pages'].append(page_report)
        if visit['requested_uri'] in urls and visit['status'] == 'done':
            print('Comparando con navegador:', visit['requested_uri'], flush=True)
            try:
                with loaded_page(visit['requested_uri']) as page:
                    visible = ' '.join(page.locator('body').inner_text().split())
                    screenshot = root / f'browser-{index}.jpg'
                    page.screenshot(path=str(screenshot), type='jpeg', quality=80, full_page=False)
                    visible_checks = []
                    for part in fields[:100]:
                        if part['class'] in {'image', 'product_link'}:
                            continue
                        for value in part.get('values', []):
                            text = ' '.join(str(value.get('text', '')).split())
                            visible_checks.append({'class': part['class'], 'text': text[:200],
                                'source': part['source'], 'visible': bool(text and text in visible)})
                    page_report['browser'] = {'title': page.title(), 'url': page.url,
                        'h1': page.locator('h1').all_text_contents(),
                        'articles': page.locator('article, [role="article"]').count(),
                        'semantic_scopes': page.locator('[itemscope][itemtype], [typeof]').count(),
                        'visible_text_chars': len(visible), 'visible_excerpt': visible[:500],
                        'screenshot': str(screenshot.resolve()), 'field_checks': visible_checks}
            except Exception as exc:
                page_report['browser_error'] = f'{type(exc).__name__}: {exc}'
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'report': str(report_path), 'run_id': run_id, 'summary': report['summary'],
                      'pages': [{k: p[k] for k in ('url', 'status', 'page_type', 'roles', 'fields')}
                                for p in report['pages']], 'costs': run['costs']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
