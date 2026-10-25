"""Producer registry; delivery transport and acknowledgment are deliberately absent."""
import uuid
from .postgres import configured_dsn, connect


class Subscriptions:
    def __init__(self, dsn=None):
        self.dsn = dsn or configured_dsn()
        if not self.dsn:
            raise RuntimeError('PostgreSQL no está configurado.')

    def create_subscriber(self, name):
        with connect(self.dsn,'public') as db:
            row=db.execute('INSERT INTO subscribers(id,name) VALUES(?,?) RETURNING *',(uuid.uuid4().hex,name)).fetchone()
            return dict(row)

    def subscribers(self):
        with connect(self.dsn,'public') as db:
            return [dict(r) for r in db.execute('''SELECT s.*,count(sc.content_id) contents,
                count(sc.content_id) FILTER (WHERE NOT sc.delivered) pending
                FROM subscribers s LEFT JOIN subscriber_contents sc ON sc.subscriber_id=s.id
                GROUP BY s.id ORDER BY s.created_at,s.id''')]

    def contents(self, page=1, subscriber_id=None):
        with connect(self.dsn,'public') as db:
            if subscriber_id:
                query='FROM contents c JOIN subscriber_contents sc ON sc.content_id=c.id WHERE sc.subscriber_id=?'
                params=(subscriber_id,)
                columns='c.*,sc.delivered,sc.delivered_at'
            else:
                query='FROM contents c';params=();columns='c.*'
            total=db.execute('SELECT count(*) '+query,params).fetchone()[0]
            rows=db.execute('SELECT '+columns+' '+query+' ORDER BY c.created_at,c.id LIMIT 20 OFFSET ?',(*params,(page-1)*20))
            return dict(items=[dict(row) for row in rows],total=total,page=page,page_size=20)
