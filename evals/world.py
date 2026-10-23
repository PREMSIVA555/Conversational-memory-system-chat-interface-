"""P00 synthetic persistence skeleton. Not a banking API or authorization layer.

Reset requires a separately initialized test instance, reset role and matching
PostgreSQL/Redis identity markers. No application DSN or .env is consulted.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg import sql
import redis

ROOT = Path(__file__).resolve().parents[1]
WORLD_ROOT = ROOT / ".genesis" / "local" / "worlds"
DATABASE = "bank_p00_test"
RESET_ROLE = "p00_reset"
APP_ROLE = "p00_app"
CUSTOMERS = [("11111111-1111-4111-8111-111111111111", "Synthetic customer A"),
             ("22222222-2222-4222-8222-222222222222", "Synthetic customer B")]
FIXTURE_VERSION = "p00-two-customers-v1"


def is_link(path: Path) -> bool:
    """Also reject Windows junctions on Python 3.11."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0)
                                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


class UnsafeTarget(ValueError):
    pass


@dataclass(frozen=True)
class DomainClock:
    """Only a domain date. Never used for security expiry or elapsed budgets."""
    value: datetime

    def now(self) -> datetime:
        return self.value


@dataclass(frozen=True)
class World:
    dsn: str
    redis_url: str
    instance: str
    system_identifier: str
    trial: str

    @property
    def schema(self) -> str:
        return f"p00_trial_{self.trial}"

    @property
    def prefix(self) -> str:
        return f"p00:{self.instance}:{self.trial}:"

    @property
    def files(self) -> Path:
        return WORLD_ROOT / self.trial

    @property
    def clock(self) -> DomainClock:
        return DomainClock(datetime.fromisoformat("2026-01-15T12:00:00+00:00"))

    def validate_target(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,39}", self.trial):
            raise UnsafeTarget("invalid trial namespace")
        if not re.fullmatch(r"[a-f0-9]{32}", self.instance):
            raise UnsafeTarget("missing test instance identity")
        db, cache = urlsplit(self.dsn), urlsplit(self.redis_url)
        if (db.scheme not in ("postgres", "postgresql") or db.hostname != "127.0.0.1"
                or db.path != "/" + DATABASE or db.username != RESET_ROLE
                or db.query or db.fragment or not db.port):
            raise UnsafeTarget("reset requires explicit loopback test DB and reset role")
        if (cache.scheme != "redis" or cache.hostname != "127.0.0.1"
                or cache.path != "/0" or cache.query or cache.fragment or not cache.port):
            raise UnsafeTarget("reset requires explicit loopback test Redis")
        # Resolve and inspect every existing ancestor before recursive removal.
        for candidate in [ROOT / ".genesis", ROOT / ".genesis/local", WORLD_ROOT, self.files]:
            if is_link(candidate):
                raise UnsafeTarget("test file path contains a link/junction")
        if not self.files.resolve().is_relative_to((ROOT / ".genesis/local/worlds").resolve()):
            raise UnsafeTarget("test files escape declared root")
        if self.files.exists() and any(is_link(item) for item in self.files.rglob("*")):
            raise UnsafeTarget("test files contain a link/junction")

    def _connect(self):
        self.validate_target()
        conn = psycopg.connect(self.dsn, connect_timeout=3)
        try:
            identity = conn.execute("SELECT current_database(), current_user, system_identifier::text FROM pg_control_system()").fetchone()
            marker = conn.execute("SELECT instance FROM public.p00_test_instance").fetchall()
            if identity != (DATABASE, RESET_ROLE, self.system_identifier) or marker != [(self.instance,)]:
                raise UnsafeTarget("database identity does not match initialized test instance")
            return conn
        except BaseException:
            conn.close()
            raise

    def _redis(self):
        client = redis.Redis.from_url(self.redis_url, decode_responses=True,
                                      socket_timeout=3, socket_connect_timeout=3)
        if client.get("p00:test-instance") != self.instance:
            client.close()
            raise UnsafeTarget("Redis identity does not match initialized test instance")
        return client

    def reset(self) -> dict:
        with self._connect() as conn, self._redis() as cache:
            # Serialize same-trial resets across processes, including files/cache.
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (self.schema,))
            self.validate_target()
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema)))
            conn.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION p00_reset").format(sql.Identifier(self.schema)))
            conn.execute(sql.SQL("CREATE TABLE {}.customers (id uuid PRIMARY KEY, name text NOT NULL)").format(sql.Identifier(self.schema)))
            for row in CUSTOMERS:
                conn.execute(sql.SQL("INSERT INTO {}.customers VALUES (%s,%s)").format(sql.Identifier(self.schema)), row)
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO p00_app").format(sql.Identifier(self.schema)))
            conn.execute(sql.SQL("GRANT SELECT ON {}.customers TO p00_app").format(sql.Identifier(self.schema)))
            # Never FLUSHDB; only exact validated instance/trial-prefixed keys.
            for key in cache.scan_iter(match=self.prefix + "*"):
                cache.delete(key)
            cache.set(self.prefix + "fixture", FIXTURE_VERSION)
            if self.files.exists():
                shutil.rmtree(self.files)
            self.files.mkdir(parents=True)
            (self.files / "fixture.json").write_text(json.dumps({"fixture": FIXTURE_VERSION}), encoding="utf-8")
        return self.snapshot()

    def snapshot(self) -> dict:
        with self._connect() as conn, self._redis() as cache:
            rows = conn.execute(sql.SQL("SELECT id::text,name FROM {}.customers ORDER BY id").format(sql.Identifier(self.schema))).fetchall()
            state = dict(fixture=FIXTURE_VERSION, domain_clock=self.clock.now().isoformat(),
                         customers=rows,
                         cache={key[len(self.prefix):]: cache.get(key) for key in sorted(cache.scan_iter(match=self.prefix + "*"))},
                         files={p.relative_to(self.files).as_posix(): p.read_text(encoding="utf-8")
                                for p in sorted(self.files.rglob("*")) if p.is_file()})
        canonical = json.dumps(state, sort_keys=True, separators=(",", ":"))
        return {"state": state, "semantic_sha256": hashlib.sha256(canonical.encode()).hexdigest()}


def load_world(config: Path, trial: str) -> World:
    data = json.loads(config.read_text(encoding="utf-8"))
    return World(data["dsn"], data["redis_url"], data["instance"], data["system_identifier"], trial)
