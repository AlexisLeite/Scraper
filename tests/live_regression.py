"""Manual live smoke tests; require network, Playwright and a server-side Jev key.

The original strategy copy omitted exact URLs, so these are public comparable pages.
Run with: .venv/bin/python tests/live_regression.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _credentials import activate_test_key
from tests.legacy.jev_extractor import inspect_url

CASES = [
    ('Vichy listado', 'https://www.vichy.es/todos-los-productos/cuidado-de-la-piel', 'listing'),
    ('Vichy ficha', 'https://www.vichy.es/todos-los-productos/cuidado-de-la-piel/aguas-micelares-y-desmaquillantes/sequedad/purete-thermale-agua-micelar-mineral-piel-sensible', 'product'),
    ('NIVEA listado', 'https://www.nivea.es/productos/rostro/cuidado-facial', 'listing'),
    ('NIVEA ficha', 'https://www.nivea.es/productos/nivea-creme-250-ml-40058088010530244.html', 'product'),
    ('Sibari ficha', 'https://sibarirepublic.com/producto/long-lasting-hydration-calming-cream/', 'product'),
]

if __name__ == '__main__':
    activate_test_key(required=True)
    for name, url, expected_kind in CASES:
        result = inspect_url(url)
        kind = result['classification']['value']
        actions = [(x['effect'], x.get('before_products'), x.get('after_products'),
                    x.get('before_review_nodes'), x.get('after_review_nodes'))
                   for x in result['analysis']['actions']]
        print(name, kind, 'productos:', len(result['analysis']['product_links']),
              'reseñas:', result['analysis']['reviews']['state']['value'], 'acciones:', actions, flush=True)
        assert kind == expected_kind
