"""Read-only extraction checks, with reports persisted in PostgreSQL."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import time

from .store import Store, RULES
from ..common.scrapper import AuditStore
from ..common.scrapper.url_policy import URLPolicy


def quality(result):
    entities = (result or {}).get('entities', [])
    products = [e for e in entities if e['role'] == 'product']
    incomplete = []
    for product in products:
        present = {p['class'] for p in product.get('parts', []) if any(v.get('text') for v in p.get('values', []))}
        missing = {'name', 'price'} - present
        if missing:
            incomplete.append({'entity': product['id'], 'missing': sorted(missing)})
    coverage = (result or {}).get('classification', {}).get('coverage', {})
    warnings = []
    if not entities or all(e['role'] in {'metadata', 'site_menu', 'breadcrumb'} for e in entities):
        warnings.append('sin_contenido_principal')
    if incomplete:
        warnings.append('productos_sin_nombre_o_precio')
    if coverage.get('omitted_entities') or coverage.get('jsonld_errors'):
        warnings.append('cobertura_incompleta')
    return {'page_type': (result or {}).get('page_type'), 'roles': dict(Counter(e['role'] for e in entities)),
            'incomplete_products': incomplete, 'coverage': coverage, 'warnings': warnings}


def check(store, audit, seed_id):
    seed = next((s for s in store.seeds() if s['id'] == seed_id), None)
    if seed is None:
        raise ValueError('URL inicial no encontrada')
    now = time.time()
    with store.db() as db:
        rows = db.execute('''SELECT a.* FROM activities a JOIN discoveries d ON d.page_id=a.page_id
            WHERE d.seed_id=? ORDER BY a.created DESC''', (seed_id,)).fetchall()
        starts = db.execute('''SELECT count(*) FROM stages st JOIN activities a ON a.id=st.activity_id
            JOIN discoveries d ON d.page_id=a.page_id WHERE d.seed_id=? AND st.stage='starting' AND st.at>?''', (seed_id, now-60)).fetchone()[0]
        recent_errors = db.execute('''SELECT count(*) FROM failure_events f JOIN discoveries d ON d.page_id=f.page_id
            WHERE d.seed_id=? AND f.at>?''', (seed_id, now-600)).fetchone()[0]
        last_start = db.execute('''SELECT max(st.at) FROM stages st JOIN activities a ON a.id=st.activity_id
            JOIN discoveries d ON d.page_id=a.page_id WHERE d.seed_id=? AND st.stage='starting' ''', (seed_id,)).fetchone()[0]
        hb = db.execute('SELECT at FROM heartbeat WHERE id=1').fetchone()
    counts = dict(Counter(r['status'] for r in rows))
    warnings = []
    if recent_errors:
        warnings.append('errores_en_los_ultimos_10_minutos')
    if seed.get('max_urls_per_minute') and starts > seed['max_urls_per_minute']:
        warnings.append('limite_por_minuto_superado')
    settings = store.settings()
    if seed['enabled'] and settings['enabled'] and (counts.get('queued') or counts.get('retry_wait')):
        if not hb or now-hb[0] > 60:
            warnings.append('motor_sin_heartbeat')
        if now-(last_start or min(r['created'] for r in rows)) > 600 and not counts.get('running'):
            warnings.append('cola_sin_avance')
    samples = []
    completed = sorted((r for r in rows if r['status'] == 'done' and r['run_id']), key=lambda r:r['finished'] or 0, reverse=True)[:5]
    policy = URLPolicy(*(seed[k] for k in RULES))
    for activity in completed:
        for visit in audit.list_visits(activity['run_id'])[:1]:
            detail = audit.get_visit(visit['id'])
            sample = {'activity_id': activity['id'], 'visit_id': visit['id'], **quality(detail.get('result'))}
            final_uri = detail.get('final_uri') or detail.get('uri')
            sample['url'] = final_uri
            if final_uri and final_uri != seed['url'] and policy.disposition(final_uri):
                sample['warnings'].append('redireccion_fuera_del_alcance')
            samples.append(sample)
    if any(s['warnings'] for s in samples):
        warnings.append('revisar_calidad_de_muestras')
    return {'seed_id': seed_id, 'url': seed['url'], 'at': now,
            'checked_at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
            'counts': counts, 'starts_last_minute': starts, 'max_urls_per_minute': seed.get('max_urls_per_minute'),
            'errors_last_10_minutes': recent_errors, 'last_start': last_start,
            'enabled': seed['enabled'] and settings['enabled'], 'warnings': warnings, 'samples': samples}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', required=True)
    args = parser.parse_args()
    store = Store()
    report = check(store, AuditStore(), args.seed)
    with store.db() as db:
        db.execute('INSERT INTO extraction_checks(seed_id,at,body) VALUES(?,?,?)', (args.seed, report['at'], json.dumps(report)))
        db.execute('DELETE FROM extraction_checks WHERE at<?', (time.time()-30*86400,))
    print(json.dumps(report, ensure_ascii=False))

if __name__ == '__main__':
    main()
