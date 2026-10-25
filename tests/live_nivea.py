from tests.legacy.config import ScrapperConfig as LegacyConfig
from tests.legacy.processors import JevHtmlToWebPageEntities
"""Live NIVEA regression with test credentials and persistent HTML/action/usage audit.

Run: .venv/bin/python tests/live_nivea.py [--case listing|product|all]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _credentials import activate_test_key
from live_regression import CASES
from src.common.scrapper import Scrapper, ScrapperConfig, JevHtmlToWebPageEntities
from tests.legacy import jev_connector


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=['listing', 'product', 'all'], default='all')
    args = parser.parse_args()
    activate_test_key(required=True)
    summaries = []
    for name, url, expected in CASES:
        if not name.startswith('NIVEA') or (args.case != 'all' and expected != args.case):
            continue
        print('Analizando:', name, flush=True)
        def traced_predict(state, questions):
            response = jev_connector.predict(state, questions)
            if 'controls' in state:
                print('Controles Jev:', [(control['text'], response['answers'].get(f'control_{i}'))
                      for i, control in enumerate(state['controls'])], flush=True)
            return response
        scraper = Scrapper(LegacyConfig(max_sites=5, max_hops=1, max_retries=0, follow_navigation=False,
            html_to_entities=JevHtmlToWebPageEntities(predict=traced_predict)))
        run = scraper.run([url])
        visit = scraper.audit.get_visit(scraper.audit.list_visits(run.run_id)[0]['id'])
        result = visit.get('result') or {}
        classification = result.get('classification', {})
        output = {'name': name, 'run_id': run.run_id, 'status': visit['status'],
            'error': visit.get('error'), 'page_type': result.get('page_type'),
            'confidence': result.get('confidence'),
            'summary_counts': classification.get('summary', {}).get('counts'),
            'discarded_blocks': classification.get('summary', {}).get('discarded_count'),
            'primary': classification.get('primary_decision'),
            'field_decisions': sum(len(d.get('parts', [])) for d in result.get('decisions', [])),
            'reviews': result.get('reviews'),
            'actions': [{key: action.get(key) for key in ('effect', 'before_products', 'after_products',
                       'before_reviews', 'after_reviews')} for action in result.get('actions', [])],
            'usage': scraper.audit.get_run(run.run_id)['usage']}
        summaries.append(output)
        Path('data/nivea-regression-result.json').write_text(json.dumps(summaries, ensure_ascii=False, indent=2))
        print(json.dumps(output, ensure_ascii=False), flush=True)
        assert visit['status'] == 'done', output.get('error')
        assert result['page_type'] == expected, f"Esperado {expected}, obtenido {result['page_type']}"
        if expected == 'listing':
            assert classification['primary_decision']['value'] != 'other', 'Colección principal no identificada'
        else:
            assert result['reviews']['state']['value'] == 'published'
            assert any(a['effect'] == 'loaded_reviews' and a['after_reviews'] > a['before_reviews']
                       for a in result['actions']), 'No se verificó la carga de reseñas'
    return summaries


if __name__ == '__main__':
    main()
