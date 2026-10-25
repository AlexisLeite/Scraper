"""PostgreSQL connections for the existing repositories.

The statement adapter keeps parameterized repository SQL shared with isolated
SQLite regression fixtures. Production always uses the configured PostgreSQL DSN.
"""
import os
import re
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def configured_dsn():
    value = os.environ.get('SCRAPER_DATABASE_URL')
    if value:
        return value
    config = Path('/etc/scraper/database.env')
    if config.exists():
        for line in config.read_text().splitlines():
            if line.startswith('SCRAPER_DATABASE_URL='):
                return line.split('=',1)[1].strip()
    return None


class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Cursor:
    def __init__(self, cursor, lastrowid=None):
        self.cursor = cursor
        self.lastrowid = lastrowid

    def fetchone(self):
        row = self.cursor.fetchone()
        return Row(row) if row is not None else None

    def fetchall(self):
        return [Row(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, statement, params=()):
        statement = statement.strip().rstrip(';')
        if statement == 'BEGIN IMMEDIATE':
            return Cursor(self.raw.execute('SELECT pg_advisory_xact_lock(73492001)'))
        if statement.startswith('ATTACH DATABASE'):
            return Cursor(self.raw.execute('SELECT 1'))
        statement = statement.replace('audit_history.', 'audit.')
        statement = statement.replace("json_extract(s.body,'$.url')", "(s.body::jsonb ->> 'url')")
        statement = re.sub(r'\browid\b', 'edge_order', statement)
        ignore = statement.startswith('INSERT OR IGNORE ')
        if ignore:
            statement = statement.replace('INSERT OR IGNORE ', 'INSERT ', 1) + ' ON CONFLICT DO NOTHING'
        statement = statement.replace('? IS NULL', 'CAST(? AS TEXT) IS NULL')
        # Literal percentage signs and placeholders are escaped separately.
        statement = statement.replace('%', '%%').replace('?', '%s')
        returning = statement.startswith('INSERT INTO stages(')
        if returning:
            statement += ' RETURNING id'
        cursor = self.raw.execute(statement, tuple(int(v) if isinstance(v,bool) else v for v in params))
        return Cursor(cursor, cursor.fetchone()['id'] if returning else None)


@contextmanager
def connect(dsn, schema):
    if schema not in ('service','audit','public'):
        raise ValueError('Unknown repository schema')
    with psycopg.connect(dsn, row_factory=dict_row) as raw:
        raw.execute(f'SET search_path TO {schema}, public')
        yield Connection(raw)
