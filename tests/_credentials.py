"""Use a local test credential for command-line test processes only."""
import os
from pathlib import Path

TEST_ENV = Path(__file__).resolve().parents[1] / '.env.test'


def activate_test_key(required=False):
    key = os.environ.get('TYPESAFE_TEST_API_KEY', '').strip()
    if not key and TEST_ENV.exists():
        for line in TEST_ENV.read_text(encoding='utf-8').splitlines():
            if line.startswith('TYPESAFE_API_KEY='):
                key = line.partition('=')[2].strip().strip('"\'')
                break
    if not key and required:
        raise RuntimeError('Configura TYPESAFE_TEST_API_KEY o .env.test para las pruebas con Jev.')
    if key:
        os.environ['TYPESAFE_API_KEY'] = key
    return bool(key)
