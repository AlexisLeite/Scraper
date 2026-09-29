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
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..common import jev_connector
from ..common.jev_extractor import MAX_CAPTURE_CHARS, inspect_url
from ..common.schema import generate_schema, validate_questions
from ..common.scrapper import AuditStore, Scrapper, ScrapperConfig, canonical_uri
from ..common.uri_to_html import _valid_url

ROOT = Path(__file__).resolve().parents[1] / 'app'
_jobs = {}
_jobs_lock = Lock()
_scrape_jobs = {}
_scrape_jobs_lock = Lock()
_audit_store = AuditStore()
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

class ScrapeRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    max_sites: int = Field(default=10, ge=1, le=100)
    max_hops: int = Field(default=2, ge=0, le=8)


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

@app.get('/scrape.js')
def scrape_script():
    return FileResponse(ROOT / 'scrape.js', media_type='text/javascript')

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


def _run_scrape(job_id: str, request: ScrapeRequest):
    def progress(update):
        with _scrape_jobs_lock:
            _scrape_jobs[job_id].update(update)
    try:
        scraper = Scrapper(ScrapperConfig(max_threads=3, max_hops=request.max_hops,
                                         max_sites=request.max_sites), audit=_audit_store)
        summary = scraper.run([request.url], on_progress=progress)
        with _scrape_jobs_lock:
            _scrape_jobs[job_id].update(status='done', summary=vars(summary))
    except Exception:
        _log.exception('Scrape failed')
        with _scrape_jobs_lock:
            _scrape_jobs[job_id].update(status='error', message='El recorrido falló; consulta el historial conservado.')


@app.post('/api/scrapes', status_code=202)
def start_scrape(request: ScrapeRequest):
    try:
        canonical_uri(request.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with _scrape_jobs_lock:
        if sum(job['status'] == 'running' for job in _scrape_jobs.values()) >= 2:
            raise HTTPException(503, 'Ya hay dos recorridos activos; espera a que termine uno.')
        job_id = uuid.uuid4().hex
        _scrape_jobs[job_id] = {'status': 'running', 'message': 'Preparando recorrido',
                                'run_id': None, 'scheduled': 0, 'succeeded': 0,
                                'failed': 0, 'doubtful': 0, 'content_events': 0}
    Thread(target=_run_scrape, args=(job_id, request), daemon=True).start()
    return {'job_id': job_id}


@app.get('/api/scrapes')
def scrape_history():
    return _audit_store.list_runs()


@app.get('/api/scrapes/jobs/{job_id}')
def scrape_job(job_id: str):
    with _scrape_jobs_lock:
        job = _scrape_jobs.get(job_id)
        if job is None:
            raise HTTPException(404, 'Recorrido no encontrado en este proceso.')
        return dict(job)


@app.get('/api/scrapes/{run_id}')
def scrape_run(run_id: str):
    run = _audit_store.get_run(run_id)
    if run is None:
        raise HTTPException(404, 'Historial no encontrado.')
    return {'run': run, 'visits': _audit_store.list_visits(run_id),
            'edges': _audit_store.list_edges(run_id)}


def _scrape_visit(run_id: str, visit_id: str):
    visit = _audit_store.get_visit(visit_id)
    if visit is None or visit['run_id'] != run_id:
        raise HTTPException(404, 'Página no encontrada en este recorrido.')
    return visit


@app.get('/api/scrapes/{run_id}/visits/{visit_id}')
def scrape_visit(run_id: str, visit_id: str):
    return _scrape_visit(run_id, visit_id)


@app.get('/api/scrapes/{run_id}/visits/{visit_id}/screenshot')
def scrape_screenshot(run_id: str, visit_id: str):
    _scrape_visit(run_id, visit_id)
    screenshot = _audit_store.read_screenshot(visit_id)
    if screenshot is None:
        raise HTTPException(404, 'Esta página no tiene captura disponible.')
    return Response(screenshot, media_type='image/jpeg')


@app.get('/api/scrapes/{run_id}/visits/{visit_id}/html')
def scrape_html(run_id: str, visit_id: str):
    _scrape_visit(run_id, visit_id)
    html = _audit_store.read_html(visit_id)
    if html is None:
        raise HTTPException(404, 'Esta página no tiene HTML disponible.')
    return Response(html, media_type='text/html; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="pagina-renderizada.html"'})

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
