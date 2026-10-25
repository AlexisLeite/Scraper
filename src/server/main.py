"""HTTP API and frontend mount for the local studio."""
import ipaddress
import logging
import subprocess
import uuid
from pathlib import Path
from threading import Lock, Thread
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..common.dom import clean_document
from ..common.scrapper import (AuditStore, SemanticMetadataStrategy,
                              Scrapper, ScrapperConfig, canonical_uri)
from ..common.uri_to_html import _valid_url
from ..common.scrapper.url_policy import URLPolicy

FRONTEND_ROOT = Path(__file__).resolve().parents[1] / 'web' / 'out'
_scrape_jobs = {}
_scrape_jobs_lock = Lock()
_audit_store = AuditStore()
_log = logging.getLogger('markup_studio')
app = FastAPI(title='Scrapper', docs_url='/api/docs')
from ..persistent.api import router as persistent_router
app.include_router(persistent_router(audit=_audit_store))
app.mount('/_next', StaticFiles(directory=FRONTEND_ROOT / '_next', check_dir=False), name='next-assets')

class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str = Field(min_length=8, max_length=2048)

class ScrapeRequest(BaseModel):
    url: str | None = Field(default=None, min_length=8, max_length=2048)
    urls: list[str] = Field(default_factory=list, max_length=100)
    strategy: Literal['semantic'] = 'semantic'
    max_sites: int = Field(default=10, ge=1, le=100)
    max_hops: int = Field(default=2, ge=0, le=8)
    max_threads: int = Field(default=3, ge=1, le=50)
    allowed_domains: list[str] = Field(default_factory=list, max_length=100)
    blocked_domains: list[str] = Field(default_factory=list, max_length=100)
    allowed_url_patterns: list[str] = Field(default_factory=list, max_length=100)
    blocked_url_patterns: list[str] = Field(default_factory=list, max_length=100)

    @property
    def initial_urls(self):
        return ([self.url] if self.url else []) + self.urls

    @model_validator(mode='after')
    def validate_seeds(self):
        if not self.initial_urls or any(not 8 <= len(url) <= 2048 for url in self.initial_urls):
            raise ValueError('Indica una o más URL iniciales válidas.')
        if any(len(value) > 2048 for values in (self.allowed_domains, self.blocked_domains,
               self.allowed_url_patterns, self.blocked_url_patterns) for value in values):
            raise ValueError('Cada regla de URL admite un máximo de 2048 caracteres.')
        if (self.allowed_domains or self.allowed_url_patterns) and (self.blocked_domains or self.blocked_url_patterns):
            raise ValueError('La lista blanca y la lista negra son excluyentes. Elige una sola.')
        URLPolicy(self.allowed_domains, self.blocked_domains,
                  self.allowed_url_patterns, self.blocked_url_patterns)
        return self


@app.get('/')
def index():
    index_path = FRONTEND_ROOT / 'index.html'
    if not index_path.is_file():
        raise HTTPException(503, 'Compila el portal con npm ci --prefix src/web y npm run build --prefix src/web.')
    return FileResponse(index_path, headers={'Cache-Control':'no-cache'})

@app.get('/api/health')
def health():
    return {'extractor': 'semantic', 'extractors': ['semantic']}


@app.post('/api/capture', deprecated=True)
@app.post('/api/analyze', deprecated=True)
@app.post('/api/analyze-html', deprecated=True)
@app.get('/api/jobs/{job_id}', deprecated=True)
@app.post('/api/classify', deprecated=True)
@app.post('/api/generate-schema', deprecated=True)
def retired_extractor(job_id: str | None = None):
    raise HTTPException(410, 'Los extractores anteriores fueron retirados. Usa Extraer (/api/extract).')


def _run_scrape(job_id: str, request: ScrapeRequest):
    def progress(update):
        with _scrape_jobs_lock:
            _scrape_jobs[job_id].update(update)
    try:
        scraper = Scrapper(ScrapperConfig(max_threads=request.max_threads, max_hops=request.max_hops,
                                         max_sites=request.max_sites,
                                         allowed_domains=tuple(request.allowed_domains),
                                         blocked_domains=tuple(request.blocked_domains),
                                         allowed_url_patterns=tuple(request.allowed_url_patterns),
                                         blocked_url_patterns=tuple(request.blocked_url_patterns),
                                         strategy=SemanticMetadataStrategy(follow_navigation=request.max_hops>0)), audit=_audit_store)
        summary = scraper.run(request.initial_urls, on_progress=progress)
        with _scrape_jobs_lock:
            _scrape_jobs[job_id].update(status='done', summary=vars(summary))
    except Exception:
        _log.exception('Scrape failed')
        with _scrape_jobs_lock:
            _scrape_jobs[job_id].update(status='error', message='El recorrido falló; consulta el historial conservado.')


@app.post('/api/scrapes', status_code=202)
def start_scrape(request: ScrapeRequest):
    try:
        for url in request.initial_urls:
            canonical_uri(url)
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


@app.post('/api/extract', status_code=202)
def extract_page(request: ExtractRequest):
    # Isolated from the persistent discovery queue. Both limits are enforced
    # server-side so neither links nor collection expansions can start a visit.
    return start_scrape(ScrapeRequest(url=request.url, strategy='semantic',
                                     max_sites=1, max_hops=0, max_threads=1))


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
def scrape_html(run_id: str, visit_id: str, clean: bool = False):
    _scrape_visit(run_id, visit_id)
    html = _audit_store.read_html(visit_id)
    if html is None:
        raise HTTPException(404, 'Esta página no tiene HTML disponible.')
    if clean:
        soup, _ = clean_document(html)
        html = str(soup)
    return Response(html, media_type='text/html; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="pagina-renderizada.html"'})


@app.get('/api/scrapes/{run_id}/visits/{visit_id}/actions/{index}/{phase}/{kind}')
def scrape_action_artifact(run_id: str, visit_id: str, index: int, phase: str, kind: str):
    _scrape_visit(run_id, visit_id)
    if kind not in ('html', 'screenshot'):
        raise HTTPException(404, 'Tipo de evidencia no encontrado.')
    content = _audit_store.read_action_artifact(visit_id, index, phase, kind)
    if content is None:
        raise HTTPException(404, 'Evidencia de acción no encontrada.')
    return Response(content, media_type='image/jpeg' if kind == 'screenshot' else 'text/html; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="accion.html"'} if kind == 'html' else {})


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
