"""Initialize dedicated local PostgreSQL + Redis-compatible test processes.

Uses installed binaries, never an existing service/cluster or application .env.
All data, test-only passwords, PID/log/config files live under .genesis/local.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.world import DATABASE, ROOT, RESET_ROLE, APP_ROLE, load_world
import psycopg
from psycopg import sql
import redis

BASE = ROOT / ".genesis/local/p00-services"
CONFIG = ROOT / ".genesis/local/p00-world.json"


def resume() -> None:
    """Restore stopped test processes without overwriting an identity mismatch."""
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    world = load_world(CONFIG, "readiness")
    world.validate_target()
    data = Path(config["data_directory"])
    if data.resolve() != (BASE / "pgdata").resolve() or data.is_symlink():
        raise RuntimeError("Refusing unexpected test cluster data directory")
    creation = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        conn = world._connect()
    except psycopg.OperationalError:
        pg_ctl = Path(config["pg_bin"]) / "pg_ctl.exe"
        with (BASE / "resume-postgres.log").open("a", encoding="utf-8") as log:
            subprocess.run([str(pg_ctl), "-D", str(data), "-l", str(BASE / "postgres.log"),
                            "-o", f"-h 127.0.0.1 -p {urlsplit(world.dsn).port}", "-w", "start"],
                           stdout=log, stderr=log, check=True, timeout=60, **creation)
        conn = world._connect()
    with conn:
        actual = Path(conn.execute("SHOW data_directory").fetchone()[0])
        if actual.resolve() != data.resolve():
            raise RuntimeError("PostgreSQL data directory identity mismatch")
    cache = redis.Redis.from_url(world.redis_url, decode_responses=True,
                                socket_connect_timeout=1, socket_timeout=1)
    try:
        cache.ping()
    except redis.ConnectionError:
        with (BASE / "resume-redis.log").open("ab") as log:
            subprocess.Popen([config["redis_executable"], str(BASE / "redis.conf")],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, **creation)
        for _ in range(40):
            try:
                cache.ping()
                break
            except redis.ConnectionError:
                time.sleep(0.25)
        else:
            raise RuntimeError("Test Redis readiness timed out")
    try:
        marker = cache.get("p00:test-instance")
        if marker is None and cache.dbsize() == 0:
            # Redis is intentionally ephemeral. Only bootstrap an EMPTY server
            # authenticated with this test cluster's random credential, after
            # validating the paired PostgreSQL marker/system ID/data directory.
            cache.set("p00:test-instance", world.instance, nx=True)
        if cache.get("p00:test-instance") != world.instance:
            raise RuntimeError("Refusing mismatched or nonempty unmarked Redis")
    finally:
        cache.close()
    print("Dedicated test identities verified; test processes ready. No fixture reset performed.")


def port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start(pg_bin: Path, redis_bin: Path) -> None:
    if CONFIG.exists() or BASE.exists():
        raise SystemExit("Test service state already exists; inspect it, do not overwrite/reinitialize.")
    if not (pg_bin / "initdb.exe").is_file() or not redis_bin.is_file():
        raise SystemExit("Required local test binaries unavailable; supply --pg-bin and --redis-bin.")
    BASE.mkdir(parents=True)
    password, app_password, instance = secrets.token_hex(24), secrets.token_hex(24), uuid.uuid4().hex
    (BASE / "bootstrap.json").write_text(json.dumps(dict(password=password, app_password=app_password, instance=instance)), encoding="utf-8")
    pwfile = BASE / "initdb-password.txt"
    pwfile.write_text(password, encoding="utf-8")
    pg_port, redis_port = port(), port()
    creation = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    def run(args):
        with (BASE / "setup.log").open("a", encoding="utf-8") as log:
            # Server descendants must not inherit capture pipes: on Windows that
            # keeps communicate() waiting even after pg_ctl itself has exited.
            result = subprocess.run([str(a) for a in args], stdout=log, stderr=log, timeout=60, **creation)
        if result.returncode:
            raise RuntimeError("Test service setup failed; inspect local setup.log")
    run([pg_bin / "initdb.exe", "-D", BASE / "pgdata", "-U", RESET_ROLE,
         "--pwfile", pwfile, "--auth=scram-sha-256", "--encoding=UTF8", "--locale=C"])
    pwfile.unlink()
    run([pg_bin / "pg_ctl.exe", "-D", BASE / "pgdata", "-l", BASE / "postgres.log",
         "-o", f"-h 127.0.0.1 -p {pg_port}", "-w", "start"])
    # Password is stored only in ignored local config files, never command args.
    redis_conf = BASE / "redis.conf"
    redis_conf.write_text(f'bind 127.0.0.1\nport {redis_port}\nprotected-mode yes\nrequirepass {password}\nsave ""\nappendonly no\ndir "{BASE.as_posix()}"\n', encoding="utf-8")
    with (BASE / "redis.log").open("ab") as log:
        process = subprocess.Popen([str(redis_bin), str(redis_conf)], stdout=log, stderr=log, **creation)
    (BASE / "redis.pid").write_text(str(process.pid))
    cache_url = f"redis://:{password}@127.0.0.1:{redis_port}/0"
    cache = redis.Redis.from_url(cache_url, socket_connect_timeout=1, socket_timeout=1)
    for _ in range(40):
        try:
            cache.ping()
            break
        except redis.RedisError:
            if process.poll() is not None:
                raise RuntimeError("Test Redis process stopped; inspect local redis.log")
            time.sleep(0.25)
    else:
        raise RuntimeError("Test Redis readiness timed out")
    admin = f"postgresql://{RESET_ROLE}:{password}@127.0.0.1:{pg_port}/postgres"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} ").format(sql.Identifier(DATABASE)))
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS").format(sql.Identifier(APP_ROLE), sql.Literal(app_password)))
    dsn = admin.rsplit("/", 1)[0] + "/" + DATABASE
    with psycopg.connect(dsn) as conn:
        conn.execute("CREATE TABLE public.p00_test_instance (instance text PRIMARY KEY)")
        conn.execute("INSERT INTO public.p00_test_instance VALUES (%s)", (instance,))
        system_id = str(conn.execute("SELECT system_identifier FROM pg_control_system()").fetchone()[0])
        pg_version = conn.execute("SHOW server_version").fetchone()[0]
    cache.set("p00:test-instance", instance)
    info = cache.info("server")
    cache.close()
    data = dict(dsn=dsn, app_dsn=f"postgresql://{APP_ROLE}:{app_password}@127.0.0.1:{pg_port}/{DATABASE}",
                redis_url=cache_url, instance=instance, system_identifier=system_id,
                postgres_version=pg_version, redis_version=info.get("redis_version"),
                redis_executable=str(redis_bin), pg_bin=str(pg_bin), data_directory=str(BASE / "pgdata"),
                backend="dedicated-native-processes; not Docker PG16/Redis7")
    CONFIG.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Dedicated test processes ready. Local credential config: {CONFIG.relative_to(ROOT)}")
    print(f"PostgreSQL {pg_version}; Redis-compatible {data['redis_version']}. Existing services untouched.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true", help="resume only the already initialized test services")
    parser.add_argument("--pg-bin", type=Path, default=Path("C:/Program Files/PostgreSQL/18/bin"))
    parser.add_argument("--redis-bin", type=Path, default=Path("C:/Program Files/Memurai/memurai.exe"))
    args = parser.parse_args()
    if args.resume:
        resume()
    else:
        start(args.pg_bin, args.redis_bin)
