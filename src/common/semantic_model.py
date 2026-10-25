"""Server-side TypeSafe Jev integration."""

import json
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_URL = "https://api.typesafe.ai/v1/systemone"
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
_usage_recorder: ContextVar[Callable[[dict], None] | None] = ContextVar('jev_usage_recorder', default=None)
_log = logging.getLogger(__name__)


class UsageRecordingError(RuntimeError):
    """The provider answered but its usage could not be saved; do not rebill by retrying."""

    retryable = False
    phase = 'usage'


@contextmanager
def record_usage(callback: Callable[[dict], None]):
    """Attach a recorder to Jev calls in this worker, without sharing it across pages."""
    token = _usage_recorder.set(callback)
    try:
        yield
    finally:
        _usage_recorder.reset(token)


def api_key():
    if os.environ.get('TYPESAFE_TEST_MODE') == '1':
        return os.environ.get('TYPESAFE_TEST_API_KEY', '').strip()
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key.strip()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("TYPESAFE_API_KEY="):
                return line.partition("=")[2].strip().strip('"\'')
    return ""


def configured():
    return bool(api_key())


def predict(state, questions, model="jev-latest", *, opener=urlopen):
    key = api_key()
    if not key:
        raise RuntimeError("Jev no está configurado: falta TYPESAFE_API_KEY en .env o en el entorno.")
    body = json.dumps({"model": model, "state": state, "questions": questions},
                      ensure_ascii=False).encode("utf-8")
    request = Request(API_URL, data=body, headers={"Authorization": "Bearer " + key,
                                                   "Content-Type": "application/json"})
    result = None
    status = 'error'
    try:
        with opener(request, timeout=30) as response:
            result = json.load(response)
        if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
            raise RuntimeError("Jev devolvió una respuesta inesperada.")
        status = 'done'
        return result
    except HTTPError as exc:
        raise RuntimeError(f"Jev devolvió HTTP {exc.code}; revisa la clave o el esquema.") from exc
    except URLError as exc:
        raise RuntimeError("No se pudo conectar con Jev.") from exc
    finally:
        recorder = _usage_recorder.get()
        if recorder:
            usage = result.get('usage') if isinstance(result, dict) else None
            def token_count(name):
                value = usage.get(name) if isinstance(usage, dict) else None
                return value if type(value) is int and value >= 0 else None
            try:
                recorder({'model': result.get('model', model) if isinstance(result, dict) else model,
                          'input_tokens': token_count('input_tokens'),
                          'output_tokens': token_count('output_tokens'),
                          'request_bytes': len(body), 'status': status})
            except Exception as exc:
                raise UsageRecordingError('No se pudo guardar el consumo de Jev; medición incompleta.') from exc
