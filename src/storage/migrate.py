"""One-time migration. Stop API and worker before running; keep SQLite as backup.

Usage: python -m src.storage.migrate --dsn-file /path/to/private/dsn
"""
import argparse
import sqlite3
from pathlib import Path
import psycopg
from psycopg import sql

ROOT=Path(__file__).resolve().parents[2]


def migrate(dsn):
    with psycopg.connect(dsn) as pg:
        pg.execute((ROOT/'deploy/postgres.sql').read_text())
        if pg.execute('SELECT count(*) FROM service.seeds').fetchone()[0] or pg.execute('SELECT count(*) FROM audit.runs').fetchone()[0]:
            raise RuntimeError('Destination must be empty; migration will not overwrite data.')
        counts={}
        for schema,path,tables in [
            ('service',ROOT/'data/persistent/service.sqlite3',['config','seeds','domains','pages','discoveries','activities','activity_runs','stages','failure_events','heartbeat']),
            ('audit',ROOT/'data/scrapper/audit.sqlite3',['runs','visits','attempts','edges','events','model_calls','strategy_expenses']),
        ]:
            with sqlite3.connect(f'file:{path}?mode=ro',uri=True) as source:
                source.row_factory=sqlite3.Row
                for table in tables:
                    rows=source.execute('SELECT '+('rowid AS edge_order,*' if table=='edges' else '*')+' FROM '+table).fetchall()
                    if not rows:
                        counts[f'{schema}.{table}']=0;continue
                    cols=rows[0].keys()
                    statement=sql.SQL('INSERT INTO {}.{} ({}) VALUES ({})').format(sql.Identifier(schema),sql.Identifier(table),sql.SQL(',').join(map(sql.Identifier,cols)),sql.SQL(',').join(sql.Placeholder() for _ in cols))
                    with pg.cursor() as cursor:cursor.executemany(statement,[tuple(row) for row in rows])
                    counts[f'{schema}.{table}']=len(rows)
        # Preserve IDs and original order while allowing subsequent inserts.
        for schema,table,column in [('service','config','id'),('service','heartbeat','id'),('service','stages','id'),('service','failure_events','id'),('audit','model_calls','id'),('audit','strategy_expenses','id'),('audit','edges','edge_order')]:
            pg.execute(sql.SQL("SELECT setval(pg_get_serial_sequence(%s,%s),coalesce(max({}),0)+1,false) FROM {}.{}").format(sql.Identifier(column),sql.Identifier(schema),sql.Identifier(table)),(schema+'.'+table,column))
        visit_ids={row[0] for row in pg.execute('SELECT id FROM audit.visits')}
        files=0
        for path in sorted((ROOT/'data/scrapper/captures').iterdir()):
            visit=path.name.split('-')[0]
            if visit not in visit_ids or not (path.name.endswith('.html.gz') or path.name.endswith('.jpg')):
                continue
            pg.execute('INSERT INTO audit.artifacts(path,visit_id,mime_type,data) VALUES(%s,%s,%s,%s)',
                       ('captures/'+path.name,visit,'application/gzip' if path.name.endswith('.gz') else 'image/jpeg',path.read_bytes()))
            files+=1
        for name,count in counts.items():
            actual=pg.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(*name.split('.')))).fetchone()[0]
            if actual!=count:raise RuntimeError('Migration count mismatch: '+name)
        assert pg.execute('SELECT count(*) FROM public.contents').fetchone()[0]==counts['audit.events']
        return dict(tables=counts,artifacts=files)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--dsn-file',required=True)
    args=parser.parse_args();print(migrate(Path(args.dsn_file).read_text().strip()))
