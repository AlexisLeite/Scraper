from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from .store import Store, Settings, Seed
from .rules import validate_rule
from typing import Literal
import json
import subprocess
import sys
from ..common.scrapper.orchestrator import canonical_uri
from ..common.scrapper import AuditStore


def router(store=None,audit=None):
    store = store or Store()
    audit = audit or AuditStore()
    api = APIRouter(prefix='/api/service')

    class RuleRequest(BaseModel):
        kind: Literal['domain', 'regex']
        value: str = Field(min_length=1, max_length=2048)

    @api.post('/rules/validate')
    def validate_rule_endpoint(body: RuleRequest):
        try:
            return {'value': validate_rule(body.kind, body.value)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    class RegexTest(BaseModel):
        pattern: str = Field(min_length=1, max_length=2048)
        urls: list[str] = Field(min_length=1, max_length=100)

    @api.post('/rules/test')
    def test_regex(body: RegexTest):
        try:
            pattern = validate_rule('regex',body.pattern)
            rows = []
            for url in body.urls:
                if len(url) > 2048:
                    raise ValueError('Cada URL admite hasta 2048 caracteres.')
                try:
                    rows.append(dict(url=url,normalized=canonical_uri(url)))
                except ValueError as exc:
                    rows.append(dict(url=url,error=str(exc),matched=False))
            # An erroneous expression must not hang the API while experimenting.
            code = "import json,re,sys; d=json.load(sys.stdin); p=re.compile(d['pattern']); print(json.dumps([bool(p.search(u)) for u in d['urls']]))"
            result = subprocess.run([sys.executable,'-c',code],input=json.dumps(dict(pattern=pattern,urls=[r.get('normalized','') for r in rows])),text=True,capture_output=True,timeout=2,check=True)
            for row,matched in zip(rows,json.loads(result.stdout)):
                row['matched'] = matched if 'error' not in row else False
            return dict(results=rows)
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(422,'La regex excedió el tiempo máximo de evaluación. Simplifica el patrón.') from exc

    class SubscriberRequest(BaseModel):
        name: str = Field(min_length=1,max_length=200)

    def subscriptions():
        from ..storage.subscriptions import Subscriptions
        if not store.dsn:
            raise HTTPException(503,'Las suscripciones requieren PostgreSQL.')
        return Subscriptions(store.dsn)

    @api.post('/subscribers',status_code=201)
    def create_subscriber(body: SubscriberRequest):
        import psycopg
        name=body.name.strip()
        if not name:
            raise HTTPException(422,'El nombre no puede estar vacío.')
        try:
            return subscriptions().create_subscriber(name)
        except psycopg.errors.UniqueViolation as exc:
            raise HTTPException(409,'Ya existe un suscriptor con ese nombre.') from exc

    @api.get('/subscribers')
    def list_subscribers():
        return subscriptions().subscribers()

    @api.get('/contents')
    def list_contents(page: int=Query(1,ge=1)):
        return subscriptions().contents(page)

    @api.get('/subscribers/{subscriber_id}/contents')
    def subscriber_contents(subscriber_id: str,page: int=Query(1,ge=1)):
        registry=subscriptions()
        if not any(s['id']==subscriber_id for s in registry.subscribers()):
            raise HTTPException(404,'Suscriptor no encontrado.')
        return registry.contents(page,subscriber_id)

    @api.get('/dashboard')
    def dashboard():
        return store.dashboard()

    @api.get('/settings')
    def settings():
        return store.settings()

    @api.put('/settings')
    def save_settings(body: Settings):
        return store.save_settings(body.model_dump())

    @api.get('/seeds')
    def seeds(page: int=Query(1,ge=1)):
        return store.seed_page(page)

    @api.get('/seeds/{seed_id}/urls')
    def known_urls(seed_id: str):
        return store.known_urls(seed_id)

    @api.get('/seeds/{seed_id}/pages')
    def seed_pages(seed_id: str,query: str=Query('',max_length=2048),page: int=Query(1,ge=1),status: str=''):
        if seed_id not in {s['id'] for s in store.seeds()}:
            raise HTTPException(404,'URL inicial no encontrada.')
        return store.pages(query=query,page=page,seed_id=seed_id,status=status)

    @api.post('/seeds/{seed_id}/pause')
    def pause_seed(seed_id: str):
        try:
            return store.pause_seed(seed_id)
        except LookupError as exc:
            raise HTTPException(404,str(exc)) from exc

    @api.post('/seeds/{seed_id}/start',status_code=202)
    def start_seed(seed_id: str):
        try:
            return store.start_seed(seed_id)
        except LookupError as exc:
            raise HTTPException(404,str(exc)) from exc

    @api.delete('/seeds/{seed_id}/history')
    def clear_seed_history(seed_id: str):
        try:
            return store.clear_seed_history(seed_id,audit)
        except LookupError as exc:
            raise HTTPException(404,str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc

    @api.get('/seeds/{seed_id}/checks')
    def extraction_checks(seed_id: str):
        if not any(s['id'] == seed_id for s in store.seeds()):
            raise HTTPException(404, 'URL inicial no encontrada.')
        with store.db() as db:
            return [json.loads(r['body']) for r in db.execute(
                'SELECT body FROM extraction_checks WHERE seed_id=? ORDER BY at DESC LIMIT 20', (seed_id,))]

    @api.get('/seeds/{seed_id}')
    def seed_detail(seed_id: str):
        seed = next((s for s in store.seeds() if s['id']==seed_id),None)
        if seed is None:
            raise HTTPException(404,'URL inicial no encontrada.')
        return seed

    @api.post('/seeds',status_code=201)
    def add_seed(body: Seed):
        return store.save_seed(body.model_dump())

    @api.put('/seeds/{seed_id}')
    def edit_seed(seed_id: str,body: Seed):
        if seed_id not in {s['id'] for s in store.seeds()}:
            raise HTTPException(404,'URL inicial no encontrada.')
        return store.save_seed(body.model_dump(),seed_id)

    @api.get('/activities')
    def activities(page: int=Query(1,ge=1),status: str='',domain: str=''):
        return store.activities(page,status,domain)

    @api.get('/activities/{activity_id}')
    def activity(activity_id: str):
        result = store.activity(activity_id)
        if not result:
            raise HTTPException(404,'Actividad no encontrada.')
        result['visits'] = [audit.get_visit(v['id']) for run in result['runs'] for v in audit.list_visits(run['run_id'])]
        result['costs'] = [audit.get_run(run['run_id']) for run in result['runs']]
        result['referrers'] = audit.referring_pages(result['url'])
        with store.db() as db:
            for source in result['referrers']:
                row = db.execute('SELECT activity_id FROM activity_runs WHERE run_id=? LIMIT 1',(source['run_id'],)).fetchone()
                source['activity_id'] = row['activity_id'] if row else None

        return result

    @api.post('/activities/{activity_id}/retry',status_code=202)
    def retry_activity(activity_id: str):
        activity = store.activity(activity_id)
        if not activity:
            raise HTTPException(404,'Actividad no encontrada.')
        if activity['status'] in ('queued','running','retry_wait','paused') and activity['errors']:
            return dict(activity_id=activity_id)
        if activity['status'] != 'error':
            raise HTTPException(409,'Solo se pueden reintentar actividades marcadas como error.')
        try:
            return dict(activity_id=store.rescrape(activity['page_id']))
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc

    @api.get('/domains')
    def domains():
        return store.domains()

    @api.get('/pages')
    def pages(domain: str,query: str=Query('',max_length=2048),page: int=Query(1,ge=1),status: str=''):
        return store.pages(domain,query,page,status=status)

    @api.post('/pages/{page_id}/rescrape',status_code=202)
    def rescrape(page_id: str):
        try:
            return dict(activity_id=store.rescrape(page_id))
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc

    class DomainAction(BaseModel):
        domain: str
        approved_limit: int | None = Field(None,ge=2,le=1000000)

    @api.post('/domains/action')
    def domain_action(body: DomainAction):
        try:
            store.domain_action(body.domain,body.approved_limit)
            return dict(ok=True)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc

    return api
