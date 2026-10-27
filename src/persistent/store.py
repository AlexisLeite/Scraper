from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator, ConfigDict
from .rules import DomainRule, RegexRule
from .resources import worker_resources
from ..common.scrapper.page_urls import is_page_url
from typing import Literal
from ..storage.postgres import configured_dsn, connect as pg_connect
from ..common.scrapper.orchestrator import canonical_uri
from ..common.scrapper.url_policy import URLPolicy

ROOT = Path(__file__).resolve().parents[2] / 'data' / 'persistent'
RULES = ('allowed_domains', 'blocked_domains', 'allowed_url_patterns', 'blocked_url_patterns')
ACTIVE = ('queued', 'running', 'retry_wait', 'paused')

class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    threads: int = Field(3, ge=1, le=50)
    attempts: int = Field(3, ge=1, le=10)
    freshness_hours: int = Field(24, ge=1, le=8760)
    retry_delay: int = Field(30, ge=1, le=86400)
    memory_mb: int = Field(2048, ge=256, le=262144)
    activity_memory_mb: int = Field(1024, ge=128, le=262144)
    cpu_percent: int = Field(100, ge=1, le=10000)
    suspicious_pages: int = Field(1000, ge=2, le=1000000)
    strategy: Literal['semantic'] = 'semantic'

class Seed(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str = Field(min_length=8, max_length=2048)
    enabled: bool = True
    rule_mode: Literal['white', 'black'] = 'black'
    max_urls_per_minute: int | None = Field(None, ge=1, le=60000, strict=True)
    max_pages: int | None = Field(None, ge=1, strict=True)
    allowed_domains: list[DomainRule] = Field(default_factory=list, max_length=100)
    blocked_domains: list[DomainRule] = Field(default_factory=list, max_length=100)
    allowed_url_patterns: list[RegexRule] = Field(default_factory=list, max_length=100)
    blocked_url_patterns: list[RegexRule] = Field(default_factory=list, max_length=100)

    @model_validator(mode='before')
    @classmethod
    def infer_legacy_mode(cls, values):
        if isinstance(values, dict) and 'rule_mode' not in values:
            values = {**values, 'rule_mode': 'white' if values.get('allowed_domains') or values.get('allowed_url_patterns') else 'black'}
        return values

    @model_validator(mode='after')
    def validate_rules(self):
        white = bool(self.allowed_url_patterns)
        black = bool(self.blocked_url_patterns)
        if white and black:
            raise ValueError('La lista blanca y la lista negra son excluyentes. Elige una sola.')
        if (self.rule_mode == 'white' and black) or (self.rule_mode == 'black' and white):
            raise ValueError('Las reglas deben pertenecer al modo de lista seleccionado.')
        if self.rule_mode == 'white' and not white and not self.allowed_domains:
            raise ValueError('Agrega al menos una regla a la lista blanca.')
        self.url = canonical_uri(self.url)
        if not is_page_url(self.url):
            raise ValueError('La URL inicial debe apuntar a una página, no a un archivo de recursos.')
        for key in RULES:
            if any(len(rule) > 2048 for rule in getattr(self, key)):
                raise ValueError('Las reglas admiten hasta 2048 caracteres.')
        policy = URLPolicy(*(getattr(self, key) for key in RULES))
        if policy.disposition(self.url) == 'blocked_url':
            raise ValueError('La URL inicial debe cumplir sus propias reglas.')
        return self

class Store:
    def __init__(self, root=ROOT, *, dsn=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'service.sqlite3'
        self.dsn = dsn or (configured_dsn() if self.root.resolve()==ROOT.resolve() else None)
        if self.dsn:
            return
        with self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS config(id INTEGER PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS seeds(id TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS domains(name TEXT PRIMARY KEY, suspicious INTEGER DEFAULT 0,
                reason TEXT, approved_limit INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS pages(id TEXT PRIMARY KEY, url TEXT UNIQUE NOT NULL,
                domain TEXT NOT NULL REFERENCES domains(name), created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS pages_domain ON pages(domain);
            CREATE TABLE IF NOT EXISTS discoveries(seed_id TEXT NOT NULL, page_id TEXT NOT NULL,
                depth INTEGER NOT NULL, PRIMARY KEY(seed_id,page_id));
            CREATE TABLE IF NOT EXISTS activities(id TEXT PRIMARY KEY, seed_id TEXT NOT NULL,
                page_id TEXT NOT NULL, depth INTEGER NOT NULL, status TEXT NOT NULL,
                stage TEXT NOT NULL, created REAL NOT NULL, started REAL, finished REAL,
                due REAL NOT NULL, attempt INTEGER DEFAULT 0, slot INTEGER, strategy TEXT,
                error TEXT, run_id TEXT, has_effects INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS activities_queue ON activities(status,due,created);
            CREATE INDEX IF NOT EXISTS activities_page ON activities(page_id,created DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS activities_active ON activities(seed_id,page_id)
                WHERE status IN ('queued','running','retry_wait','paused');
            CREATE TABLE IF NOT EXISTS stages(id INTEGER PRIMARY KEY, activity_id TEXT NOT NULL,
                stage TEXT NOT NULL, at REAL NOT NULL, detail TEXT);
            CREATE INDEX IF NOT EXISTS stages_starting_time ON stages(at,activity_id) WHERE stage='starting';
            CREATE TABLE IF NOT EXISTS activity_runs(activity_id TEXT NOT NULL, run_id TEXT NOT NULL,
                attempt INTEGER NOT NULL, PRIMARY KEY(activity_id,run_id));
            CREATE TABLE IF NOT EXISTS failure_events(id INTEGER PRIMARY KEY,
                stage_id INTEGER UNIQUE,activity_id TEXT NOT NULL,page_id TEXT NOT NULL,
                at REAL NOT NULL,error TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS failures_page_time ON failure_events(page_id,at);
            CREATE TABLE IF NOT EXISTS extraction_checks(seed_id TEXT NOT NULL, at REAL NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS extraction_checks_seed_time ON extraction_checks(seed_id,at DESC);
            CREATE TABLE IF NOT EXISTS heartbeat(id INTEGER PRIMARY KEY, at REAL NOT NULL, body TEXT NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO config VALUES(1,?)', (Settings().model_dump_json(),))
            db.execute("""INSERT OR IGNORE INTO failure_events(stage_id,activity_id,page_id,at,error)
                SELECT s.id,s.activity_id,a.page_id,s.at,s.detail FROM stages s
                JOIN activities a ON a.id=s.activity_id WHERE s.stage IN ('error','retry_wait') AND s.detail IS NOT NULL""")

    @contextmanager
    def db(self):
        if self.dsn:
            with pg_connect(self.dsn,'service') as db:
                yield db
            return
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=30000')
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self):
        with self.db() as db:
            body = json.loads(db.execute('SELECT body FROM config WHERE id=1').fetchone()[0])
            # Existing installations can contain a retired choice; normalize only
            # while reading persisted configuration, never accept it in new API requests.
            body['strategy'] = 'semantic'
            return Settings(**body).model_dump()

    def save_settings(self, settings):
        with self.db() as db:
            db.execute('UPDATE config SET body=? WHERE id=1', (Settings(**settings).model_dump_json(),))
            for row in db.execute('SELECT d.name,d.approved_limit,count(p.id) n FROM domains d JOIN pages p ON p.domain=d.name GROUP BY d.name').fetchall():
                limit = max(settings['suspicious_pages'],row['approved_limit'])
                if row['n'] >= limit:
                    self._suspect(db,row['name'],f'Alcanzó el límite de {limit} URL diferentes.')
        return self.settings()

    def seeds(self):
        with self.db() as db:
            return [dict(id=r['id'], **json.loads(r['body'])) for r in db.execute('SELECT * FROM seeds ORDER BY created')]

    def seed_page(self, page=1):
        with self.db() as db:
            total = db.execute('SELECT count(*) FROM seeds').fetchone()[0]
            rows = db.execute('''SELECT s.*,p.id page_id,
                (SELECT a.id FROM activities a WHERE a.page_id=p.id ORDER BY a.created DESC LIMIT 1) activity_id
                FROM seeds s LEFT JOIN pages p ON p.url=json_extract(s.body,'$.url')
                ORDER BY s.created,s.id LIMIT 20 OFFSET ?''',((page-1)*20,))
            items = [dict(id=r['id'],page_id=r['page_id'],activity_id=r['activity_id'],**json.loads(r['body'])) for r in rows]
            for item in items:
                pages = db.execute('''SELECT p.url,
                    (SELECT status FROM activities WHERE page_id=p.id ORDER BY created DESC LIMIT 1) status
                    FROM pages p JOIN discoveries d ON d.page_id=p.id WHERE d.seed_id=?''',(item['id'],))
                counts = {}
                for row in pages:
                    if is_page_url(row['url']):
                        status = row['status'] or 'idle'
                        counts[status] = counts.get(status, 0) + 1
                item['page_count'] = sum(counts.values())
                item['activity_counts'] = counts
                if counts.get('running'):
                    item['status'] = 'running'
                elif not item['enabled'] or not self.settings()['enabled'] or counts.get('paused'):
                    item['status'] = 'paused'
                else:
                    item['status'] = next((status for status in ('queued','retry_wait') if counts.get(status)),
                                          'completed_with_errors' if counts.get('error') else 'idle')
            return dict(items=items,total=total,page=page,page_size=20)

    def pause_seed(self, seed_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM seeds WHERE id=?',(seed_id,)).fetchone()
            if row is None:
                raise LookupError('URL inicial no encontrada.')
            body = json.loads(row['body'])
            body['enabled'] = False
            db.execute('UPDATE seeds SET body=? WHERE id=?',(json.dumps(body),seed_id))
        return dict(enabled=False)

    def start_seed(self, seed_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM seeds WHERE id=?',(seed_id,)).fetchone()
            if row is None:
                raise LookupError('URL inicial no encontrada.')
            body = json.loads(row['body'])
            body['enabled'] = True
            db.execute('UPDATE seeds SET body=? WHERE id=?',(json.dumps(body),seed_id))
        activity = self.discover(seed_id,body['url'],0,manual=True)
        return dict(activity_id=activity,engine_enabled=self.settings()['enabled'])

    def delete_seed(self, seed_id, audit):
        return self.clear_seed_history(seed_id, audit, remove_seed=True)

    def clear_seed_history(self, seed_id, audit, *, remove_seed=False):
        # Lock admission before checking active work or removing associations.
        with self.db() as db:
            db.execute('ATTACH DATABASE ? AS audit_history', (str(audit.database),))
            db.execute('BEGIN IMMEDIATE')
            seed = db.execute('SELECT body FROM seeds WHERE id=?',(seed_id,)).fetchone()
            if not seed:
                raise LookupError('URL inicial no encontrada.')
            active = [r['id'] for r in db.execute("""SELECT a.id FROM activities a JOIN discoveries d ON d.page_id=a.page_id
                WHERE d.seed_id=? AND a.slot IS NOT NULL AND NOT EXISTS
                (SELECT 1 FROM discoveries other WHERE other.page_id=a.page_id AND other.seed_id!=?)""",(seed_id,seed_id))]
            from .cancellation import cancel_workers
            cancel_workers(active,self.root)
            body = json.loads(seed['body'])
            body['enabled'] = False
            db.execute('UPDATE seeds SET body=? WHERE id=?',(json.dumps(body),seed_id))
            db.execute('CREATE TEMP TABLE removed_pages AS SELECT page_id FROM discoveries WHERE seed_id=? AND page_id NOT IN (SELECT page_id FROM discoveries WHERE seed_id!=?)',(seed_id,seed_id))
            db.execute('CREATE TEMP TABLE removed_activities AS SELECT id FROM activities WHERE page_id IN (SELECT page_id FROM removed_pages)')
            db.execute("""CREATE TEMP TABLE removed_runs AS
                SELECT run_id FROM activity_runs WHERE activity_id IN (SELECT id FROM removed_activities)
                UNION SELECT run_id FROM activities WHERE id IN (SELECT id FROM removed_activities) AND run_id IS NOT NULL""")
            db.execute('DELETE FROM removed_runs WHERE run_id IN (SELECT run_id FROM activity_runs WHERE activity_id NOT IN (SELECT id FROM removed_activities))')
            db.execute('CREATE TEMP TABLE removed_visits AS SELECT id FROM audit_history.visits WHERE run_id IN (SELECT run_id FROM removed_runs)')
            visit_ids = [r[0] for r in db.execute('SELECT id FROM removed_visits')]
            db.execute('CREATE TABLE IF NOT EXISTS pending_capture_deletions(path TEXT PRIMARY KEY)')
            for visit_id in visit_ids:
                if len(visit_id)!=32 or any(c not in '0123456789abcdef' for c in visit_id):
                    raise ValueError('Identificador de captura inválido.')
                for path in ([] if self.dsn else audit.captures.glob(visit_id+'-*')):
                    db.execute('INSERT OR IGNORE INTO pending_capture_deletions VALUES(?)',(str(path),))
            for table in ('attempts','model_calls','strategy_expenses'):
                db.execute(f'DELETE FROM audit_history.{table} WHERE visit_id IN (SELECT id FROM removed_visits)')
            for table in ('events','edges','visits'):
                db.execute(f'DELETE FROM audit_history.{table} WHERE run_id IN (SELECT run_id FROM removed_runs)')
            db.execute('DELETE FROM audit_history.runs WHERE id IN (SELECT run_id FROM removed_runs)')
            for table in ('failure_events','stages','activity_runs'):
                db.execute(f'DELETE FROM {table} WHERE activity_id IN (SELECT id FROM removed_activities)')
            deleted = db.execute('SELECT count(*) FROM removed_activities').fetchone()[0]
            db.execute('DELETE FROM activities WHERE id IN (SELECT id FROM removed_activities)')
            db.execute('DELETE FROM discoveries WHERE seed_id=?',(seed_id,))
            # Shared activities still belong to their remaining origin.
            db.execute("""UPDATE activities SET seed_id=(SELECT seed_id FROM discoveries WHERE page_id=activities.page_id LIMIT 1)
                WHERE seed_id=?""",(seed_id,))
            db.execute('DELETE FROM pages WHERE id IN (SELECT page_id FROM removed_pages)')
            db.execute('DELETE FROM domains WHERE name NOT IN (SELECT domain FROM pages)')
            if remove_seed:
                db.execute('DELETE FROM extraction_checks WHERE seed_id=?',(seed_id,))
                db.execute('DELETE FROM seeds WHERE id=?',(seed_id,))
        errors = []
        with self.db() as db:
            for row in db.execute('SELECT path FROM pending_capture_deletions').fetchall():
                path = Path(row['path'])
                try:
                    if path.parent.resolve() != audit.captures.resolve():
                        raise OSError('Ruta de captura fuera del archivo de auditoría.')
                    path.unlink(missing_ok=True)
                    db.execute('DELETE FROM pending_capture_deletions WHERE path=?',(str(path),))
                except OSError as exc:
                    errors.append(str(exc))
        return dict(deleted_activities=deleted,file_errors=errors)

    @staticmethod
    def _matching_seeds(db, url):
        matches = []
        for row in db.execute('SELECT id,body FROM seeds ORDER BY created,id'):
            seed = json.loads(row['body'])
            disposition = URLPolicy(*(seed[k] for k in RULES)).disposition(url)
            if seed['enabled'] and (not disposition or (url == seed['url'] and disposition == 'outside_allowlist')):
                matches.append(row['id'])
        return matches

    def matching_seeds(self, url):
        with self.db() as db:
            return self._matching_seeds(db,canonical_uri(url))

    def known_urls(self, seed_id):
        with self.db() as db:
            rows = db.execute('SELECT p.url FROM pages p JOIN discoveries d ON d.page_id=p.id WHERE d.seed_id=? ORDER BY p.created DESC LIMIT 100',(seed_id,))
            return [r[0] for r in rows]

    def save_seed(self, seed, seed_id=None):
        body = Seed(**seed).model_dump()
        seed_id = seed_id or uuid.uuid4().hex
        with self.db() as db:
            db.execute('INSERT INTO seeds VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                       (seed_id, json.dumps(body), time.time()))
        self.discover(seed_id, body['url'], 0)
        return dict(id=seed_id, **body)

    @staticmethod
    def _stage(db, activity_id, stage, detail=None):
        db.execute('UPDATE activities SET stage=? WHERE id=?', (stage, activity_id))
        cursor = db.execute('INSERT INTO stages(activity_id,stage,at,detail) VALUES(?,?,?,?)',
                   (activity_id, stage, time.time(), detail))
        return cursor.lastrowid

    def stage(self, activity_id, stage, detail=None):
        with self.db() as db:
            self._stage(db, activity_id, stage, detail)

    def effects(self, activity_id):
        with self.db() as db:
            db.execute('UPDATE activities SET has_effects=1 WHERE id=?', (activity_id,))

    def discover(self, seed_id, url, depth, manual=False):
        url = canonical_uri(url)
        if len(url) > 2048 or not is_page_url(url):
            return None
        domain = urlsplit(url).hostname
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM seeds WHERE id=?', (seed_id,)).fetchone():
                raise ValueError('URL inicial no encontrada.')
            matches = self._matching_seeds(db, url)
            # Disabled origins may be configured ahead of time, but cannot admit
            # discovered URLs until enabled. Keep only their own seed queued.
            if not matches:
                row = db.execute('SELECT body FROM seeds WHERE id=?', (seed_id,)).fetchone()
                body = json.loads(row[0])
                if depth == 0 and not manual and body['url'] == url:
                    matches = [seed_id]
                else:
                    return None
            db.execute('INSERT OR IGNORE INTO domains(name) VALUES(?)', (domain,))
            d = db.execute('SELECT * FROM domains WHERE name=?', (domain,)).fetchone()
            settings = json.loads(db.execute('SELECT body FROM config WHERE id=1').fetchone()[0])
            limit = max(settings['suspicious_pages'], d['approved_limit'])
            existing = db.execute('SELECT id FROM pages WHERE url=?', (url,)).fetchone()
            count = db.execute('SELECT count(*) FROM pages WHERE domain=?', (domain,)).fetchone()[0]
            # Stop growing the database after the threshold, including links emitted
            # by already running pages. Existing evidence and URLs remain accessible.
            if not existing and (d['suspicious'] or count >= limit):
                self._suspect(db, domain, f'Alcanzó el límite de {limit} URL diferentes.')
                return None
            page_id = existing[0] if existing else uuid.uuid4().hex
            db.execute('INSERT OR IGNORE INTO pages VALUES(?,?,?,?)', (page_id, url, domain, time.time()))
            for origin in matches:
                db.execute('INSERT OR IGNORE INTO discoveries VALUES(?,?,?)', (origin, page_id, depth))
            seed_id = seed_id if seed_id in matches else matches[0]
            if not existing and count + 1 >= limit:
                self._suspect(db, domain, f'Alcanzó el límite de {limit} URL diferentes.')
            active = db.execute("SELECT id FROM activities WHERE page_id=? AND status IN ('queued','running','retry_wait','paused') ORDER BY created LIMIT 1", (page_id,)).fetchone()
            if active:
                return active[0]
            previous = db.execute("SELECT id,finished,status FROM activities WHERE page_id=? AND status NOT IN ('excluded','duplicate') ORDER BY created DESC LIMIT 1", (page_id,)).fetchone()
            if previous and previous['status'] == 'error':
                failures = db.execute('SELECT count(*) FROM failure_events WHERE page_id=? AND at>?',(page_id,time.time()-86400)).fetchone()[0]
                if failures >= 3 and not manual:
                    return previous['id']
                db.execute("UPDATE activities SET status='queued',finished=NULL,due=?,slot=NULL,error=NULL WHERE id=?",(time.time(),previous['id']))
                self._stage(db,previous['id'],'queued','Reintento de la misma actividad; errores anteriores conservados.')
                return previous['id']
            if previous and not manual and (previous['finished'] is None or previous['finished'] > time.time()-settings.get('freshness_hours',24)*3600):
                return previous['id']
            paused = db.execute('SELECT suspicious FROM domains WHERE name=?', (domain,)).fetchone()[0]
            activity_id = uuid.uuid4().hex
            status = 'paused' if paused else 'queued'
            db.execute('INSERT INTO activities(id,seed_id,page_id,depth,status,stage,created,due) VALUES(?,?,?,?,?,?,?,?)',
                       (activity_id, seed_id, page_id, depth, status, status, time.time(), time.time()))
            self._stage(db, activity_id, status)
            return activity_id

    @staticmethod
    def _suspect(db, domain, reason):
        db.execute('UPDATE domains SET suspicious=1,reason=? WHERE name=?', (reason, domain))
        # Running processes are suspended by the daemon; never rescheduled while alive.
        db.execute("UPDATE activities SET status='paused' WHERE status IN ('queued','retry_wait') AND page_id IN (SELECT id FROM pages WHERE domain=?)", (domain,))

    def domain_action(self, domain, limit=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM domains WHERE name=?', (domain,)).fetchone():
                raise ValueError('Dominio no encontrado.')
            if limit is None:
                self._suspect(db, domain, 'Pausado manualmente.')
            else:
                count = db.execute('SELECT count(*) FROM pages WHERE domain=?', (domain,)).fetchone()[0]
                if limit <= count:
                    raise ValueError(f'El nuevo límite debe superar las {count} URL actuales.')
                db.execute('UPDATE domains SET suspicious=0,reason=NULL,approved_limit=? WHERE name=?', (limit, domain))
                db.execute("UPDATE activities SET status='queued' WHERE status='paused' AND slot IS NULL AND page_id IN (SELECT id FROM pages WHERE domain=?)", (domain,))

    @staticmethod
    def _rate_limited_seeds(db, now):
        # The persisted starting stage records every attempt, including retries.
        # Count shared pages against every associated origin so another matching
        # origin cannot bypass a configured limit. claim holds the queue lock.
        limits = {r['id']: json.loads(r['body']).get('max_urls_per_minute')
                  for r in db.execute('SELECT id,body FROM seeds')}
        limits = {key: value for key, value in limits.items() if value is not None}
        if not limits:
            return set()
        counts = db.execute("""SELECT d.seed_id,count(*) n,max(st.at) last_start FROM stages st
            JOIN activities a ON a.id=st.activity_id
            JOIN discoveries d ON d.page_id=a.page_id
            WHERE st.stage='starting' AND st.at>? GROUP BY d.seed_id""", (now-60,))
        return {r['seed_id'] for r in counts
                if r['seed_id'] in limits and (r['n'] >= limits[r['seed_id']]
                    or now-r['last_start'] < 60/limits[r['seed_id']])}

    @staticmethod
    def _page_limited_seeds(db):
        limits = {r['id']: json.loads(r['body']).get('max_pages')
                  for r in db.execute('SELECT id,body FROM seeds')}
        if not any(value is not None for value in limits.values()):
            return set()
        counts = db.execute("""SELECT d.seed_id,count(DISTINCT a.page_id) n
            FROM activities a JOIN discoveries d ON d.page_id=a.page_id
            WHERE a.started IS NOT NULL GROUP BY d.seed_id""")
        return {r['seed_id'] for r in counts
                if limits.get(r['seed_id']) is not None and r['n'] >= limits[r['seed_id']]}

    @staticmethod
    def _page_budget_allows(db, matches, page_id, limited):
        # Discovery is free. Reserve a distinct page only when claim starts it,
        # under the queue lock; retries and refreshes reuse that page's place.
        return not limited.intersection(matches) or bool(db.execute(
            'SELECT 1 FROM activities WHERE page_id=? AND started IS NOT NULL LIMIT 1',
            (page_id,)).fetchone())

    def has_pending_work(self):
        with self.db() as db:
            now = time.time()
            limited = self._rate_limited_seeds(db, now)
            page_limited = self._page_limited_seeds(db)
            rows=db.execute("""SELECT p.url,p.id page_id FROM activities a JOIN pages p ON p.id=a.page_id
                JOIN domains d ON d.name=p.domain WHERE a.status IN ('queued','retry_wait')
                AND a.due<=? AND d.suspicious=0""",(now,))
            for row in rows:
                matches = self._matching_seeds(db, row['url']) if is_page_url(row['url']) else []
                if matches and not limited.intersection(matches) and self._page_budget_allows(db,matches,row['page_id'],page_limited):
                    return True
            return False

    def claim(self, slot):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            settings = json.loads(db.execute('SELECT body FROM config WHERE id=1').fetchone()[0])
            if not settings['enabled'] or slot > settings['threads']:
                return None
            if db.execute("SELECT 1 FROM activities WHERE slot=? AND status IN ('running','paused')",(slot,)).fetchone():
                return None
            limited = self._rate_limited_seeds(db, time.time())
            page_limited = self._page_limited_seeds(db)
            # Alternate shallow and deep claims without assuming either is more useful.
            deep = db.execute('SELECT sum(attempt) FROM activities').fetchone()[0] or 0
            rows = db.execute("""SELECT a.*,p.url,p.domain,s.body FROM activities a
                JOIN pages p ON p.id=a.page_id JOIN domains d ON d.name=p.domain
                JOIN seeds s ON s.id=a.seed_id WHERE a.status IN ('queued','retry_wait')
                AND a.due<=? AND d.suspicious=0 ORDER BY a.depth """ + ('DESC' if deep % 2 else 'ASC') + ',a.created', (time.time(),)).fetchall()
            for row in rows:
                if not is_page_url(row['url']):
                    db.execute("UPDATE activities SET status='excluded',finished=?,slot=NULL WHERE id=?",(time.time(),row['id']))
                    self._stage(db,row['id'],'excluded','Recurso estático; no es una página.')
                    continue
                matches = self._matching_seeds(db,row['url'])
                if not matches or limited.intersection(matches):
                    continue
                if not self._page_budget_allows(db,matches,row['page_id'],page_limited):
                    continue
                duplicate = db.execute("SELECT id FROM activities WHERE page_id=? AND id!=? AND status='running' LIMIT 1",(row['page_id'],row['id'])).fetchone()
                if duplicate:
                    db.execute("UPDATE activities SET status='duplicate',finished=? WHERE id=?",(time.time(),row['id']))
                    self._stage(db,row['id'],'duplicate','Comparte extracción con '+duplicate[0])
                    continue
                for origin in matches:
                    db.execute('INSERT OR IGNORE INTO discoveries VALUES(?,?,?)',(origin,row['page_id'],row['depth']))
                db.execute('UPDATE activities SET seed_id=? WHERE id=?',(row['seed_id'] if row['seed_id'] in matches else matches[0],row['id']))
                db.execute("UPDATE activities SET status='running',slot=?,started=coalesce(started,?),attempt=attempt+1,strategy=?,error=NULL,has_effects=0 WHERE id=?",
                           (slot,time.time(),'semantic',row['id']))
                self._stage(db,row['id'],'starting')
                return row['id']
        return None

    def attach_run(self, activity_id, run_id):
        with self.db() as db:
            db.execute('UPDATE activities SET run_id=? WHERE id=?',(run_id,activity_id))
            db.execute('INSERT INTO activity_runs SELECT id,?,attempt FROM activities WHERE id=?',(run_id,activity_id))

    def finish(self, activity_id, error=None, retryable=True):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            a = db.execute('SELECT * FROM activities WHERE id=?',(activity_id,)).fetchone()
            if not a or a['status'] not in ('running','paused'):
                return
            settings = json.loads(db.execute('SELECT body FROM config WHERE id=1').fetchone()[0])
            if error:
                stage_id = self._stage(db,activity_id,'error',error)
                db.execute('INSERT INTO failure_events(stage_id,activity_id,page_id,at,error) VALUES(?,?,?,?,?)',
                           (stage_id,activity_id,a['page_id'],time.time(),error))
            failures = db.execute('SELECT count(*) FROM failure_events WHERE page_id=? AND at>?',(a['page_id'],time.time()-86400)).fetchone()[0]
            retry = error and retryable and not a['has_effects'] and failures < min(settings['attempts'],3)
            status = 'retry_wait' if retry else ('error' if error else 'done')
            db.execute('UPDATE activities SET status=?,slot=NULL,error=?,due=?,finished=? WHERE id=?',
                       (status,error,time.time()+settings['retry_delay'],None if retry else time.time(),activity_id))
            if status != 'error':
                self._stage(db,activity_id,status)


    def exclude(self, activity_id, reason):
        with self.db() as db:
            db.execute("UPDATE activities SET status='excluded',finished=?,slot=NULL,error=NULL WHERE id=?",(time.time(),activity_id))
            self._stage(db,activity_id,'excluded',reason)

    def recover(self):
        # The service manager has killed the previous process group before restart.
        with self.db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM activities WHERE slot IS NOT NULL")]
        for activity_id in ids:
            self.finish(activity_id,'Proceso interrumpido; se conservaron los resultados parciales.')

    @staticmethod
    def _progress(db, activity):
        if activity['status']=='done':
            return 100
        if activity['status']=='queued':
            return 0
        weights={'starting':10,'loading':25,'capture':45,'extracting':65,'interaction':80,'saving':90}
        stages=db.execute("""SELECT stage FROM stages WHERE activity_id=? AND id>=coalesce(
            (SELECT max(id) FROM stages WHERE activity_id=? AND stage='starting'),0)""",(activity['id'],activity['id']))
        return max((weights.get(row['stage'],0) for row in stages),default=0)

    def activity(self, activity_id):
        with self.db() as db:
            a = db.execute('SELECT a.*,p.url,p.domain FROM activities a JOIN pages p ON p.id=a.page_id WHERE a.id=?',(activity_id,)).fetchone()
            if not a:
                return None
            return dict(a, progress=self._progress(db,a), origins=[dict(id=r['id'],url=json.loads(r['body'])['url']) for r in db.execute('SELECT s.id,s.body FROM seeds s JOIN discoveries d ON d.seed_id=s.id WHERE d.page_id=? ORDER BY s.created,s.id',(a['page_id'],))], errors=[dict(r) for r in db.execute('SELECT at,error FROM failure_events WHERE activity_id=? ORDER BY at',(activity_id,))],
                        errors_last_24h=db.execute('SELECT count(*) FROM failure_events WHERE page_id=? AND at>?',(a['page_id'],time.time()-86400)).fetchone()[0], stages=[dict(r) for r in db.execute('SELECT * FROM stages WHERE activity_id=? ORDER BY id',(activity_id,))],
                        runs=[dict(r) for r in db.execute('SELECT * FROM activity_runs WHERE activity_id=? ORDER BY attempt',(activity_id,))])

    def activities(self, page=1, status='', domain=''):
        where = ' WHERE (?=\'\' OR a.status=?) AND (?=\'\' OR p.domain=?)'
        args = (status,status,domain,domain)
        with self.db() as db:
            total = db.execute('SELECT count(*) FROM activities a JOIN pages p ON p.id=a.page_id'+where,args).fetchone()[0]
            rows = db.execute('SELECT a.*,p.url,p.domain FROM activities a JOIN pages p ON p.id=a.page_id'+where+' ORDER BY a.created DESC LIMIT 20 OFFSET ?',(*args,(page-1)*20))
            return dict(items=[dict(r,progress=self._progress(db,r)) for r in rows],total=total,page=page,page_size=20)

    def domains(self):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT d.*,count(p.id) pages FROM domains d LEFT JOIN pages p ON p.domain=d.name GROUP BY d.name ORDER BY d.suspicious DESC,d.name')]

    def pages(self, domain='', query='', page=1, seed_id=None, status=''):
        with self.db() as db:
            rows = [dict(r) for r in db.execute('''SELECT p.*,
                (SELECT id FROM activities WHERE page_id=p.id ORDER BY created DESC LIMIT 1) activity_id,
                (SELECT status FROM activities WHERE page_id=p.id ORDER BY created DESC LIMIT 1) status
                FROM pages p WHERE (?='' OR domain=?) AND
                (? IS NULL OR EXISTS (SELECT 1 FROM discoveries d WHERE d.page_id=p.id AND d.seed_id=?))
                ORDER BY url''',(domain,domain,seed_id,seed_id))]
        rows = [r for r in rows if is_page_url(r['url']) and (not status or r['status']==status)]
        if query.strip():
            q = query.lower().strip()
            def score(row):
                value = row['url'].lower()
                tokens = value.replace('/',' ').replace('-',' ').replace('_',' ').split()
                return 1 if q in value else max([SequenceMatcher(None,q,t).ratio() for t in tokens]+[SequenceMatcher(None,q,value).ratio()])
            ranked = [(score(r),r) for r in rows]
            rows = [r for s,r in sorted(ranked,key=lambda x:(-x[0],x[1]['url'])) if s >= .55]
        return dict(items=rows[(page-1)*20:page*20],total=len(rows),page=page,page_size=20)

    def rescrape(self, page_id):
        with self.db() as db:
            row = db.execute('SELECT p.url,d.seed_id,d.depth FROM pages p JOIN discoveries d ON d.page_id=p.id WHERE p.id=? ORDER BY d.depth LIMIT 1',(page_id,)).fetchone()
            if not row:
                raise ValueError('Página no encontrada.')
        result = self.discover(row['seed_id'],row['url'],row['depth'],manual=True)
        if not result:
            raise ValueError('La página ya no cumple las reglas de exploración.')
        return result

    def heartbeat(self, body):
        with self.db() as db:
            db.execute('INSERT INTO heartbeat VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET at=excluded.at,body=excluded.body',(time.time(),json.dumps(body)))

    def dashboard(self):
        with self.db() as db:
            hb = db.execute('SELECT * FROM heartbeat WHERE id=1').fetchone()
            counts = {r[0]:r[1] for r in db.execute('SELECT status,count(*) FROM activities GROUP BY status')}
            workers = [dict(r) for r in db.execute('SELECT a.*,p.url,p.domain FROM activities a JOIN pages p ON p.id=a.page_id WHERE slot IS NOT NULL ORDER BY slot')]
            for worker in workers:
                worker['progress'] = self._progress(db,worker)
            researched = db.execute('SELECT count(DISTINCT p.domain) FROM activities a JOIN pages p ON p.id=a.page_id WHERE a.started IS NOT NULL').fetchone()[0]
            tz = ZoneInfo('America/Montevideo')
            now = datetime.now(tz)
            hour = now.replace(minute=0,second=0,microsecond=0)
            monday = now.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=now.weekday())
            def bucket(start, width):
                end = start + width
                count = db.execute("SELECT count(*) FROM activities WHERE status='done' AND finished>=? AND finished<?",(start.timestamp(),end.timestamp())).fetchone()[0]
                return dict(label=start.isoformat(),count=count)
            hourly = [bucket(hour-timedelta(hours=23-i),timedelta(hours=1)) for i in range(24)]
            for point in hourly:
                start = datetime.fromisoformat(point['label']).timestamp()
                end = start + 3600
                point['explored'] = db.execute("SELECT count(*) FROM stages WHERE stage='starting' AND at>=? AND at<?",(start,end)).fetchone()[0]
                point['extracted'] = db.execute("SELECT count(*) FROM stages WHERE stage='done' AND at>=? AND at<?",(start,end)).fetchone()[0]
                point['errors'] = db.execute('SELECT count(*) FROM failure_events WHERE at>=? AND at<?',(start,end)).fetchone()[0]
            since = now.timestamp()-3600
            activity_counters = dict(
                pages_per_hour=db.execute("SELECT count(*) FROM stages WHERE stage='done' AND at>=? AND at<=?",(since,now.timestamp())).fetchone()[0],
                queued=counts.get('queued',0)+counts.get('retry_wait',0),
                running=counts.get('running',0),
                errors_per_hour=db.execute('SELECT count(*) FROM failure_events WHERE at>=? AND at<=?',(since,now.timestamp())).fetchone()[0],
            )
            daily = [bucket(monday+timedelta(days=i),timedelta(days=1)) for i in range(7)]
        settings = self.settings()
        by_slot = {w['slot']:w for w in workers}
        slots = [dict(slot=i,status=by_slot[i]['status'] if i in by_slot else 'idle',activity=by_slot.get(i))
                 for i in range(1,max(settings['threads'],max(by_slot,default=0))+1)]
        daemon=json.loads(hb['body']) if hb else {}
        current=worker_resources.read(daemon.get('pid'))
        if current:
            daemon['resources']={**daemon.get('resources',{}),**current}
        return dict(settings=settings,counts=counts,workers=workers,slots=slots,domains=researched,
                    hourly=hourly,daily=daily,activity_counters=activity_counters,timezone='America/Montevideo',
                    daemon=dict(online=bool(hb and time.time()-hb['at']<10),at=hb['at'] if hb else None,
                                **daemon))
