"""Isolated empty legacy-schema fixture on the configured real PG16 cluster.

Never writes test rows to the original database or changes global login roles.
Generated databases are retained for evidence; only synthetic rows are created.
"""
from dataclasses import dataclass,field
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
from uuid import uuid4
import psycopg
from psycopg import sql
from store.db import admin_dsn,app_dsn,load_env
from store.migrate import render,template_values

@dataclass
class LegacyFixture:
    database:str
    admin_dsn:str=field(repr=False)
    app_dsn:str=field(repr=False)
    records:list=field(default_factory=list)

def database_dsn(value,database):
    parsed=urlsplit(value)
    return urlunsplit((parsed.scheme,parsed.netloc,'/'+database,parsed.query,parsed.fragment))

def create_legacy_fixture(*,protected_rows=32):
    load_env(); database='t025_'+uuid4().hex
    owner=admin_dsn(); application=app_dsn(); role=urlsplit(application).username
    with psycopg.connect(owner,autocommit=True) as c:
        c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
    fixture=LegacyFixture(database,database_dsn(owner,database),database_dsn(application,database))
    values=template_values(); directory=Path(__file__).resolve().parents[2]/'store'/'migrations'
    with psycopg.connect(fixture.admin_dsn) as c:
        # DDL from the actual legacy migrations, excluding role/password mutation.
        for number in ('0001','0002','0003','0005','0007','0009','0010'):
            path=next(directory.glob(number+'*.sql'))
            c.execute(render(path.read_text(encoding='utf-8'),values))
        c.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(role)))
        c.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON public.memories,public.audit_log,public.feedback TO {}').format(sql.Identifier(role)))
        for table in ('memories','audit_log'):
            c.execute(sql.SQL('ALTER TABLE public.{} ENABLE ROW LEVEL SECURITY').format(sql.Identifier(table)))
            c.execute(sql.SQL('ALTER TABLE public.{} FORCE ROW LEVEL SECURITY').format(sql.Identifier(table)))
            c.execute(sql.SQL("CREATE POLICY fixture_owner ON public.{} USING(subject_id=nullif(current_setting('app.subject_id',true),'')::uuid AND actor_id=nullif(current_setting('app.actor_id',true),'')::uuid) WITH CHECK(subject_id=nullif(current_setting('app.subject_id',true),'')::uuid AND actor_id=nullif(current_setting('app.actor_id',true),'')::uuid)").format(sql.Identifier(table)))
        for index in range(protected_rows):
            subject=uuid4(); row=uuid4()
            c.execute("INSERT INTO public.memories(id,subject_id,actor_id,content,source,attribute,embedding) VALUES(%s,%s,%s,%s,'user_statement','preferred_response_language',%s::vector)",
                (row,subject,subject,'synthetic legacy English '+str(index),'['+','.join(['1']+['0']*(int(values['EMBEDDING_DIM'])-1))+']'))
            fixture.records.append(dict(legacy_id=str(row),legacy_subject_id=str(subject)))
    return fixture
