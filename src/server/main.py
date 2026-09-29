"""HTTP API and frontend mount for the local studio."""
import ipaddress
import logging
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from threading import Lock, Thread

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..common import jev_connector
from ..common.jev_extractor import MAX_CAPTURE_CHARS, inspect_url
from ..common.schema import generate_schema, validate_questions
from ..common.uri_to_html import _valid_url

ROOT = Path(__file__).resolve().parents[1] / 'app'
_jobs = {}
_jobs_lock = Lock()
_log = logging.getLogger('markup_studio')
app = FastAPI(title='Markup Studio', docs_url='/api/docs')

class CaptureRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    objective: str = Field(default='', max_length=1000)

class HtmlRequest(CaptureRequest):
    html: str = Field(min_length=1, max_length=MAX_CAPTURE_CHARS)

class ClassifyRequest(BaseModel):
    content: str = Field(min_length=1, max_length=50_000)
    classification_schema: dict = Field(alias='schema')

class GenerateRequest(BaseModel):
    instructions: str = Field(min_length=8, max_length=4000)


def classify(content, schema):
    return jev_connector.predict(content, validate_questions(schema))


def _update_job(job_id, **changes):
    with _jobs_lock:
        _jobs[job_id].update(changes)


def _run_job(job_id, url, objective, captured=None):
    try:
        result = inspect_url(url, objective, lambda phase, percent, message:
                             _update_job(job_id, phase=phase, percent=percent, message=message), captured)
        _update_job(job_id, status='done', phase='done', percent=100, message='Análisis completo', result=result)
    except (ValueError, RuntimeError) as exc:
        _update_job(job_id, status='error', message=str(exc))
    except Exception:
        _log.exception('Page analysis failed')
        _update_job(job_id, status='error', message='No se pudo analizar la página; revisa el servidor.')

@app.get('/')
def index():
    return FileResponse(ROOT / 'index.html')

@app.get('/app.css')
def styles():
    return FileResponse(ROOT / 'app.css', media_type='text/css')

@app.get('/app.js')
def script():
    return FileResponse(ROOT / 'app.js', media_type='text/javascript')

@app.get('/api/health')
def health():
    return {'codex_cli': bool(shutil.which('codex.cmd' if os.name == 'nt' else 'codex')),
            'jev': {'configured': jev_connector.configured()}}

@app.post('/api/capture')
async def capture(request: CaptureRequest):
    try:
        return await run_in_threadpool(inspect_url, request.url, request.objective)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


def _start_job(url, objective, captured=None):
    with _jobs_lock:
        now = time.time()
        for key in list(_jobs):
            if _jobs[key]['status'] != 'running' and now - _jobs[key]['created'] > 900:
                del _jobs[key]
        if sum(job['status'] == 'running' for job in _jobs.values()) >= 3:
            raise HTTPException(503, 'Hay demasiados análisis activos; espera a que termine uno.')
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {'status': 'running', 'phase': 'queued', 'percent': 0,
                         'message': 'En cola', 'created': now, 'result': None}
    Thread(target=_run_job, args=(job_id, url, objective, captured), daemon=True).start()
    return {'job_id': job_id}

@app.post('/api/analyze', status_code=202)
def start_analysis(request: CaptureRequest):
    _valid_url(request.url)
    return _start_job(request.url, request.objective)

@app.post('/api/analyze-html', status_code=202)
def start_html_analysis(request: HtmlRequest):
    _valid_url(request.url)
    return _start_job(request.url, request.objective, (request.html, '', request.url, ''))

@app.get('/api/jobs/{job_id}')
def job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(404, 'Análisis no encontrado o vencido.')
        return {key: value for key, value in job.items() if key != 'created'}

@app.post('/api/classify')
async def classify_endpoint(request: ClassifyRequest):
    try:
        return await run_in_threadpool(classify, request.content, request.classification_schema)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

@app.post('/api/generate-schema')
async def generate_endpoint(request: GenerateRequest):
    try:
        return await run_in_threadpool(generate_schema, request.instructions)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(503, str(exc)) from exc

def run():
    """Bind only to the current Tailscale IPv4 address."""
    import uvicorn
    try:
        result = subprocess.run(['tailscale', 'ip', '-4'], capture_output=True, text=True,
                                timeout=5, check=True)
        host = str(ipaddress.IPv4Address(result.stdout.strip()))
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired,
            ipaddress.AddressValueError) as exc:
        raise SystemExit('Tailscale no tiene una dirección IPv4 disponible.') from exc
    uvicorn.run(app, host=host, port=8787)
