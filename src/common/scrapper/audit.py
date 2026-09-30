"""Durable crawl history with complete compressed HTML and captured screenshots."""
from __future__ import annotations

import base64
import gzip
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import ContentEvent, PageCapture, RunSummary, WebPageEntities

DEFAULT_AUDIT_DIR = Path(__file__).resolve().parents[3] / 'data' / 'scrapper'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class AuditStore:
    """SQLite index plus full capture files; safe for concurrent worker writes."""

    def __init__(self, root: str | Path = DEFAULT_AUDIT_DIR):
        self.root = Path(root).resolve()
        self.captures = self.root / 'captures'
        self.captures.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'audit.sqlite3'
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    config_json TEXT NOT NULL, summary_json TEXT
                );
                CREATE TABLE IF NOT EXISTS visits (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    requested_uri TEXT NOT NULL, canonical_uri TEXT NOT NULL,
                    final_uri TEXT, title TEXT, depth INTEGER NOT NULL,
                    parent_visit_id TEXT, via_entity_id TEXT,
                    status TEXT NOT NULL, doubtful INTEGER NOT NULL DEFAULT 0,
                    page_type TEXT, page_confidence REAL, error TEXT,
                    result_json TEXT, html_path TEXT, screenshot_path TEXT,
                    started_at TEXT NOT NULL, finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS visits_by_run ON visits(run_id, started_at);
                CREATE TABLE IF NOT EXISTS attempts (
                    visit_id TEXT NOT NULL REFERENCES visits(id), number INTEGER NOT NULL,
                    phase TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
                    html_path TEXT, screenshot_path TEXT, started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL, PRIMARY KEY (visit_id, number)
                );
                CREATE TABLE IF NOT EXISTS edges (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    from_visit_id TEXT, via_entity_id TEXT, target_uri TEXT NOT NULL,
                    canonical_uri TEXT, depth INTEGER NOT NULL, confidence REAL,
                    disposition TEXT NOT NULL, to_visit_id TEXT, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS edges_by_run ON edges(run_id, from_visit_id);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    visit_id TEXT NOT NULL REFERENCES visits(id), entity_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, callback_errors_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id, created_at);
                CREATE TABLE IF NOT EXISTS model_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    visit_id TEXT NOT NULL REFERENCES visits(id), attempt INTEGER NOT NULL,
                    model TEXT NOT NULL, status TEXT NOT NULL,
                    input_tokens INTEGER, output_tokens INTEGER,
                    request_bytes INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS model_calls_by_visit ON model_calls(visit_id, id);
            ''')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=30000')
        try:
            with db:
                yield db
        finally:
            db.close()

    def start_run(self, config: dict[str, Any]) -> str:
        run_id = uuid.uuid4().hex
        with self._lock, self._connect() as db:
            db.execute('INSERT INTO runs(id,started_at,config_json) VALUES(?,?,?)',
                       (run_id, _now(), _json(config)))
        return run_id

    def finish_run(self, summary: RunSummary) -> None:
        with self._lock, self._connect() as db:
            db.execute('UPDATE runs SET finished_at=?, summary_json=? WHERE id=?',
                       (_now(), _json(asdict(summary)), summary.run_id))

    def record_edge(self, run_id: str, from_visit_id: str | None, via_entity_id: str | None,
                    target_uri: str, canonical_uri: str | None, depth: int,
                    confidence: float | None, disposition: str) -> str:
        edge_id = uuid.uuid4().hex
        with self._lock, self._connect() as db:
            db.execute('''INSERT INTO edges
                (id,run_id,from_visit_id,via_entity_id,target_uri,canonical_uri,depth,
                 confidence,disposition,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (edge_id, run_id, from_visit_id, via_entity_id, target_uri, canonical_uri,
                 depth, confidence, disposition, _now()))
        return edge_id

    def set_edge_disposition(self, edge_id: str, disposition: str) -> None:
        with self._lock, self._connect() as db:
            db.execute('UPDATE edges SET disposition=? WHERE id=?', (disposition, edge_id))

    def start_visit(self, run_id: str, uri: str, canonical_uri: str, depth: int,
                    parent_visit_id: str | None, via_entity_id: str | None,
                    edge_id: str) -> str:
        visit_id = uuid.uuid4().hex
        with self._lock, self._connect() as db:
            db.execute('''INSERT INTO visits
                (id,run_id,requested_uri,canonical_uri,depth,parent_visit_id,via_entity_id,
                 status,started_at) VALUES(?,?,?,?,?,?,?,?,?)''',
                (visit_id, run_id, uri, canonical_uri, depth, parent_visit_id,
                 via_entity_id, 'running', _now()))
            db.execute('UPDATE edges SET to_visit_id=?, disposition=? WHERE id=?',
                       (visit_id, 'scheduled', edge_id))
        return visit_id

    def save_capture(self, visit_id: str, attempt: int, capture: PageCapture) -> tuple[str, str | None]:
        stem = f'{visit_id}-{attempt}'
        html_path = self.captures / f'{stem}.html.gz'
        temporary = self.captures / f'{stem}.{uuid.uuid4().hex}.tmp'
        try:
            with gzip.open(temporary, 'wb') as handle:
                handle.write(capture.html.encode('utf-8'))
            os.replace(temporary, html_path)
        finally:
            temporary.unlink(missing_ok=True)
        screenshot_path = None
        if capture.screenshot_base64:
            raw = base64.b64decode(capture.screenshot_base64, validate=True)
            image_path = self.captures / f'{stem}.jpg'
            temporary = self.captures / f'{stem}.{uuid.uuid4().hex}.tmp'
            try:
                temporary.write_bytes(raw)
                os.replace(temporary, image_path)
            finally:
                temporary.unlink(missing_ok=True)
            screenshot_path = str(image_path.relative_to(self.root))
        html_relative = str(html_path.relative_to(self.root))
        with self._lock, self._connect() as db:
            db.execute('''UPDATE visits SET final_uri=?, title=?, html_path=?, screenshot_path=?
                          WHERE id=?''',
                       (capture.final_uri, capture.title, html_relative, screenshot_path, visit_id))
        return html_relative, screenshot_path

    def record_attempt(self, visit_id: str, number: int, phase: str, status: str,
                       started_at: str, error: str | None = None,
                       html_path: str | None = None, screenshot_path: str | None = None) -> None:
        with self._lock, self._connect() as db:
            db.execute('''INSERT INTO attempts
                (visit_id,number,phase,status,error,html_path,screenshot_path,started_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                (visit_id, number, phase, status, error, html_path, screenshot_path,
                 started_at, _now()))

    def record_model_call(self, visit_id: str, attempt: int, call: dict[str, Any]) -> None:
        """Save provider-reported token counts; never store prompts or credentials."""
        with self._lock, self._connect() as db:
            db.execute('''INSERT INTO model_calls
                (visit_id,attempt,model,status,input_tokens,output_tokens,request_bytes,created_at)
                VALUES(?,?,?,?,?,?,?,?)''',
                (visit_id, attempt, call['model'], call['status'], call['input_tokens'],
                 call['output_tokens'], call['request_bytes'], _now()))

    def _usage(self, db: sqlite3.Connection, run_id: str | None = None,
               visit_id: str | None = None) -> dict[str, Any]:
        if visit_id:
            where, key = 'WHERE visit_id=?', visit_id
        else:
            where, key = 'WHERE visit_id IN (SELECT id FROM visits WHERE run_id=?)', run_id
        row = db.execute(f'''SELECT COUNT(*) AS calls,
            COALESCE(SUM(input_tokens),0) AS input_tokens,
            COALESCE(SUM(output_tokens),0) AS output_tokens,
            COALESCE(SUM(request_bytes),0) AS request_bytes,
            COALESCE(SUM(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL THEN 1 ELSE 0 END),0) AS unreported_calls
            FROM model_calls {where}''', (key,)).fetchone()
        return {**dict(row), 'exact': row['unreported_calls'] == 0}

    def complete_visit(self, visit_id: str, result: WebPageEntities, doubtful: bool) -> None:
        with self._lock, self._connect() as db:
            db.execute('''UPDATE visits SET status='done', doubtful=?, page_type=?,
                          page_confidence=?, result_json=?, finished_at=? WHERE id=?''',
                       (int(doubtful), result.page_type, result.confidence,
                        _json(asdict(result)), _now(), visit_id))

    def fail_visit(self, visit_id: str, error: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE visits SET status='error', error=?, finished_at=? WHERE id=?",
                       (error, _now(), visit_id))

    def record_event(self, event: ContentEvent, callback_errors: list[str] | None = None) -> str:
        event_id = uuid.uuid4().hex
        with self._lock, self._connect() as db:
            db.execute('''INSERT INTO events
                (id,run_id,visit_id,entity_id,payload_json,callback_errors_json,created_at)
                VALUES(?,?,?,?,?,?,?)''',
                (event_id, event.run_id, event.visit_id, event.entity.id, _json(asdict(event)),
                 _json(callback_errors) if callback_errors else None, _now()))
        return event_id

    def set_event_callback_errors(self, event_id: str, errors: list[str]) -> None:
        with self._lock, self._connect() as db:
            db.execute('UPDATE events SET callback_errors_json=? WHERE id=?',
                       (_json(errors), event_id))

    def list_visits(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute('''SELECT v.id,v.requested_uri,v.canonical_uri,v.final_uri,
                v.title,v.depth,v.parent_visit_id,v.via_entity_id,v.status,v.doubtful,
                v.page_type,v.page_confidence,v.error,v.html_path,v.screenshot_path,
                v.started_at,v.finished_at,e.rowid AS discovery_order
                FROM visits v LEFT JOIN edges e ON e.to_visit_id=v.id
                WHERE v.run_id=? ORDER BY e.rowid''', (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute('''SELECT id,started_at,finished_at,config_json,summary_json
                                 FROM runs ORDER BY started_at DESC LIMIT ?''',
                              (max(1, min(limit, 100)),)).fetchall()
            return [{**dict(row), 'config': (config := json.loads(row['config_json'])),
                     'summary': json.loads(row['summary_json']) if row['summary_json'] else None,
                     'usage': self._usage(db, run_id=row['id']) if config.get('usage_tracking') else None}
                    for row in rows]

    def list_doubtful(self, run_id: str) -> list[dict[str, Any]]:
        return [row for row in self.list_visits(run_id) if row['doubtful']]

    def list_edges(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute('SELECT rowid AS discovery_order,* FROM edges WHERE run_id=? ORDER BY rowid',
                              (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute('SELECT * FROM events WHERE run_id=? ORDER BY created_at,id',
                              (run_id,)).fetchall()
        return [{**dict(row), 'payload': json.loads(row['payload_json'])} for row in rows]

    def get_visit(self, visit_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute('SELECT * FROM visits WHERE id=?', (visit_id,)).fetchone()
            if row is None:
                return None
            attempts = db.execute('SELECT * FROM attempts WHERE visit_id=? ORDER BY number',
                                  (visit_id,)).fetchall()
            edges = db.execute('SELECT rowid AS discovery_order,* FROM edges WHERE from_visit_id=? ORDER BY rowid',
                               (visit_id,)).fetchall()
            events = db.execute('SELECT * FROM events WHERE visit_id=? ORDER BY created_at,id',
                                (visit_id,)).fetchall()
            model_calls = db.execute('SELECT * FROM model_calls WHERE visit_id=? ORDER BY id',
                                     (visit_id,)).fetchall()
            run = db.execute('SELECT config_json FROM runs WHERE id=?', (row['run_id'],)).fetchone()
        return {**dict(row), 'result': json.loads(row['result_json']) if row['result_json'] else None,
                'attempts': [dict(item) for item in attempts],
                'outgoing_edges': [dict(item) for item in edges],
                'events': [dict(item) for item in events],
                'model_calls': [dict(item) for item in model_calls],
                'usage': self._usage_for_visit(visit_id) if run and json.loads(run['config_json']).get('usage_tracking') else None}

    def _usage_for_visit(self, visit_id: str) -> dict[str, Any]:
        with self._connect() as db:
            return self._usage(db, visit_id=visit_id)

    def read_html(self, visit_id: str, attempt: int | None = None) -> str | None:
        visit = self.get_visit(visit_id)
        if visit is None:
            return None
        path = visit['html_path'] if attempt is None else next(
            (row['html_path'] for row in visit['attempts'] if row['number'] == attempt), None)
        if not path:
            return None
        with gzip.open(self.root / path, 'rt', encoding='utf-8') as handle:
            return handle.read()

    def read_screenshot(self, visit_id: str, attempt: int | None = None) -> bytes | None:
        visit = self.get_visit(visit_id)
        if visit is None:
            return None
        path = visit['screenshot_path'] if attempt is None else next(
            (row['screenshot_path'] for row in visit['attempts'] if row['number'] == attempt), None)
        return (self.root / path).read_bytes() if path else None

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                return None
            config = json.loads(row['config_json'])
            usage = self._usage(db, run_id=run_id) if config.get('usage_tracking') else None
        return {**dict(row), 'config': config,
                'summary': json.loads(row['summary_json']) if row['summary_json'] else None,
                'usage': usage}
