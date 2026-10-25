"""Separate constrained P01 migration path. Bootstrap role creation is offline."""
from pathlib import Path
import re
import hashlib
from psycopg import sql


def apply(conn, roles, *, through='0001'):
    for name in roles.values():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,60}", name):
            raise ValueError("Unsafe database role name")
    if through not in ('0001', '0002', '0003', '0004', '0005', '0006', '0007', '0008', '0009', '0010', '0011', '0012', '0013', '0014', '0015', '0016', '0017', '0018', '0019', '0020', '0021', '0022', '0023', '0024', '0025', '0026', '0027', '0028', '0029', '0030'):
        raise ValueError('Unknown banking migration target')
    if through >= '0002':
        with conn.transaction():
            conn.execute('CREATE TABLE IF NOT EXISTS banking.schema_migrations (name text PRIMARY KEY, sha256 text NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now())')
    for path in sorted((Path(__file__).parent / 'banking_migrations').glob('*.sql')):
        if path.name[:4] > through:
            continue
        script = path.read_text(encoding='utf-8')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        for name in ('RUNTIME', 'AUTH', 'WORKER', 'DISPATCHER', 'MIGRATOR'):
            if '@' + name + '@' in script:
                script = script.replace('@' + name + '@', sql.Identifier(roles[name.lower()]).as_string(conn))
        with conn.transaction():
            if through >= '0002':
                conn.execute("SELECT pg_advisory_xact_lock(hashtext('banking.migration'))")
                previous = conn.execute('SELECT sha256 FROM banking.schema_migrations WHERE name=%s',(path.name,)).fetchone()
                if previous:
                    if previous[0] != digest:
                        raise ValueError('Recorded migration checksum changed')
                    continue
                # P01 is already installed; do not rewrite its policies/functions during P02.
                if path.name.startswith('0001') and conn.execute("SELECT to_regclass('banking.customers')").fetchone()[0]:
                    conn.execute('INSERT INTO banking.schema_migrations(name,sha256) VALUES(%s,%s)',(path.name,digest))
                    continue
            conn.execute(script)
            if through >= '0002':
                conn.execute('INSERT INTO banking.schema_migrations(name,sha256) VALUES(%s,%s)',(path.name,digest))

