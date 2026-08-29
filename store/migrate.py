"""Apply every ``store/migrations/*.sql`` file, in filename order, idempotently.

    python -m store.migrate            # apply everything
    python -m store.migrate --status   # show the ledger without applying

Design notes
------------
*Templating.* Migration files may contain ``${VAR}`` placeholders. They are
substituted from the environment before execution. This is how
``EMBEDDING_DIM`` reaches the ``embedding vector(N)`` column: the width is never
hardcoded in the .sql file, so moving to a different-width embedder is an env
change plus a re-embed rather than a hand-edited migration.

*Idempotency.* Every file is written to be safely re-runnable (``IF NOT
EXISTS``, ``DROP POLICY IF EXISTS`` before ``CREATE POLICY``, guarded role
creation), and this runner re-applies **all** files on every invocation rather
than skipping already-applied ones. That is deliberate: skipping would make the
idempotency claim untestable, since the second run would simply do nothing. The
``schema_migrations`` ledger records what ran and when, but is not used to skip.

*Transactions.* Each file is executed as a single statement string inside its
own transaction, so dollar-quoted ``DO $$ ... $$`` blocks survive intact (no
naive splitting on semicolons).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import psycopg

from store.db import admin_dsn, load_env

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Variables a migration file is allowed to interpolate.
TEMPLATE_VARS = (
    "EMBEDDING_DIM",
    "APP_DB_USER",
    "APP_DB_PASSWORD",
    "POSTGRES_DB",
)

# APP_DB_PASSWORD is deliberately absent: it is the live credential for the role
# RLS depends on, so it must come from the environment and never from tracked
# source. template_values() raises when it is missing.
DEFAULTS = {
    "EMBEDDING_DIM": "1024",
    "APP_DB_USER": "memory_app",
    "POSTGRES_DB": "memory_system",
}

NO_DEFAULT: tuple[str, ...] = ("APP_DB_PASSWORD",)

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    text        PRIMARY KEY,
    checksum    text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    run_count   integer     NOT NULL DEFAULT 1
);
"""

_PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")


def template_values() -> dict[str, str]:
    load_env()
    values: dict[str, str] = {}
    for name in TEMPLATE_VARS:
        value = os.environ.get(name)
        if not value and name in NO_DEFAULT:
            raise RuntimeError(
                f"{name} is not set. Copy infra/.env.example to infra/.env and set it "
                "(see README). No default is provided on purpose."
            )
        values[name] = value or DEFAULTS[name]
    # fail loudly on a nonsense embedding width rather than producing vector(0)
    dim = values["EMBEDDING_DIM"]
    if not dim.isdigit() or int(dim) <= 0:
        raise RuntimeError(f"EMBEDDING_DIM must be a positive integer, got {dim!r}")
    return values


def render(sql: str, values: dict[str, str]) -> str:
    def sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise RuntimeError(
                f"migration references ${{{key}}}, which is not an allowed template var "
                f"({', '.join(TEMPLATE_VARS)})"
            )
        return values[key]

    return _PLACEHOLDER.sub(sub, sql)


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def apply_all(dsn: str | None = None, *, verbose: bool = True) -> int:
    values = template_values()
    files = migration_files()
    if not files:
        raise RuntimeError(f"no migration files found in {MIGRATIONS_DIR}")

    dsn = dsn or admin_dsn()
    applied = 0

    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(LEDGER_DDL)
        conn.commit()

        for path in files:
            raw = path.read_text(encoding="utf-8")
            sql = render(raw, values)
            checksum = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        """
                        INSERT INTO schema_migrations (filename, checksum)
                        VALUES (%s, %s)
                        ON CONFLICT (filename) DO UPDATE
                          SET checksum   = EXCLUDED.checksum,
                              applied_at = now(),
                              run_count  = schema_migrations.run_count + 1
                        """,
                        (path.name, checksum),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                print(f"[migrate] FAILED {path.name}", file=sys.stderr)
                raise
            applied += 1
            if verbose:
                print(f"[migrate] applied {path.name}")

    if verbose:
        print(f"[migrate] ok — {applied} migration file(s) applied to "
              f"{_redact(dsn)} (embedding dim {values['EMBEDDING_DIM']})")
    return applied


def show_status(dsn: str | None = None) -> None:
    dsn = dsn or admin_dsn()
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('public.schema_migrations') IS NOT NULL"
        )
        row = cur.fetchone()
        if not row or not row[0]:
            print("[migrate] no schema_migrations table — nothing applied yet")
            return
        cur.execute(
            "SELECT filename, checksum, run_count, applied_at "
            "FROM schema_migrations ORDER BY filename"
        )
        print(f"{'filename':<32} {'checksum':<18} {'runs':>5}  applied_at")
        for filename, checksum, run_count, applied_at in cur.fetchall():
            print(f"{filename:<32} {checksum:<18} {run_count:>5}  {applied_at}")


def _redact(dsn: str) -> str:
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m store.migrate")
    parser.add_argument("--status", action="store_true", help="print the ledger and exit")
    parser.add_argument("--dsn", default=None, help="override DATABASE_URL")
    args = parser.parse_args(argv)

    if args.status:
        show_status(args.dsn)
        return 0

    apply_all(args.dsn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
