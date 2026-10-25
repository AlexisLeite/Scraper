from tests.legacy.config import ScrapperConfig as LegacyConfig
from tests.legacy.processors import JevHtmlToWebPageEntities
"""Five live commercial scenarios, isolated test credentials and complete crawl audit.

Run: .venv/bin/python tests/live_scenarios.py [--case 'NIVEA listado']
All attempts and input cost are recorded in data/scenario-regression.json.
"""
import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _credentials import activate_test_key
from live_regression import CASES
from src.common.scrapper import Scrapper, ScrapperConfig, JevHtmlToWebPageEntities
from tests.legacy import jev_connector


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', action='append', choices=[case[0] for case in CASES])
    parser.add_argument('--report', default='data/scenario-regression.json')
    args = parser.parse_args()
    activate_test_key(required=True)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'runs': []}
    report_path.write_text(json.dumps(report))
    for name, url, expected in CASES:
        if args.case and name not in args.case:
            continue
        print('Analizando:', name, flush=True)
        traces = []
        def traced(state, questions):
            response = jev_connector.predict(state, questions)
            phase = ('summary' if 'summary' in state else 'fields' if 'fields' in state else
                     'controls' if 'controls' in state else 'reviews' if 'state' in questions else
                     'records' if 'records' in questions else 'blocks')
            trace = {'phase': phase, 'question_ids': list(questions), 'answers': response['answers']}
            if phase == 'controls':
                trace['controls'] = [{k: c.get(k) for k in ('id', 'text', 'group_role')} for c in state['controls']]
            traces.append(trace)
            return response
        scraper = Scrapper(LegacyConfig(max_sites=5, max_hops=1, max_retries=0, follow_navigation=False,
            html_to_entities=JevHtmlToWebPageEntities(predict=traced)))
        run = scraper.run([url])
        visit = scraper.audit.get_visit(scraper.audit.list_visits(run.run_id)[0]['id'])
        result = visit.get('result') or {}
        classification = result.get('classification', {})
        reviews = result.get('reviews', {})
        actions = result.get('actions', [])
        checks = {'completed': visit['status'] == 'done', 'page_type': result.get('page_type') == expected}
        if expected == 'listing':
            checks['primary_collection'] = classification.get('primary_decision', {}).get('value') not in (None, 'other')
        if name == 'Vichy ficha':
            checks['empty_reviews'] = reviews.get('state', {}).get('value') == 'empty_widget'
        if name in ('NIVEA ficha', 'Sibari ficha'):
            checks['published_reviews'] = reviews.get('state', {}).get('value') == 'published'
            checks['verified_more_reviews'] = any(a['effect'] == 'loaded_reviews' and
                a.get('after_reviews', 0) > a.get('before_reviews', 0) for a in actions)
        if name == 'Vichy listado':
            checks['verified_more_products'] = any(a['effect'] == 'added_products' for a in actions)
        usage = scraper.audit.get_run(run.run_id)['usage'] or {}
        output = {'name': name, 'url': url, 'run_id': run.run_id, 'status': visit['status'],
            'visited_states': run.scheduled, 'visit_limit': 5,
            'expansion_visits': sum(v['kind'] == 'expansion' for v in scraper.audit.list_visits(run.run_id)),
            'error': visit.get('error'), 'page_type': result.get('page_type'),
            'confidence': result.get('confidence'), 'checks': checks,
            'summary_counts': classification.get('summary', {}).get('counts'),
            'primary': classification.get('primary_decision'), 'reviews': reviews,
            'actions': [{key: a.get(key) for key in ('effect', 'before_products', 'after_products',
                'before_product_nodes', 'after_product_nodes', 'before_reviews', 'after_reviews',
                'product_identities')} | {'text': a.get('control', {}).get('text')} for a in actions],
            'usage': usage, 'input_cost_usd': usage.get('input_tokens', 0) / 1_000_000 * .042}
        report['runs'].append(output)
        report['input_tokens'] = sum(r['usage'].get('input_tokens', 0) for r in report['runs'])
        report['output_tokens'] = sum(r['usage'].get('output_tokens', 0) for r in report['runs'])
        report['input_cost_usd'] = report['input_tokens'] / 1_000_000 * .042
        report['usage_complete'] = all(r['usage'].get('exact', False) for r in report['runs'])
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        (report_path.parent / f"scenario-trace-{run.run_id}.json").write_text(json.dumps(traces, ensure_ascii=False, indent=2))
        print(json.dumps(output, ensure_ascii=False), flush=True)
    return 0 if all(all(r['checks'].values()) for r in report['runs']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
