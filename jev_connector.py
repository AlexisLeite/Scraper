"""Server-side TypeSafe Jev integration."""

import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_URL = "https://api.typesafe.ai/v1/systemone"
ENV_FILE = Path(__file__).resolve().parent / ".env"


def api_key():
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
    try:
        with opener(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Jev devolvió HTTP {exc.code}; revisa la clave o el esquema.") from exc
    except URLError as exc:
        raise RuntimeError("No se pudo conectar con Jev.") from exc
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise RuntimeError("Jev devolvió una respuesta inesperada.")
    return result
