"""T00.3: real persistence/retrieval evidence from explicitly synthetic capture inputs.

Only a newly created database on the identity-verified P00 test cluster is
migrated. Existing fixture schemas and application data are never reset.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import importlib.metadata
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "evals/fixtures/p00_memory_incident.json"
SOURCE_FILES = ["scripts/p00_reproduce_memory.py", "evals/fixtures/p00_memory_incident.json",
                "capture/extract.py", "capture/attributes.py", "store/db.py", "store/migrate.py",
                "store/memories.py", "store/audit.py", "retrieve/keyword.py", "retrieve/types.py",
                "retrieve/ranking.py", "retrieve/features.py", "retrieve/config.py",
                "context/composer.py", "context/config.py", "context/tokens.py",
                "graphs/response_graph.py", "graphs/response_state.py", "graphs/capture_state.py",
                "capture/config.py", "capture/metrics.py", "llm/config.py"]


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes() -> dict:
    names = SOURCE_FILES + [p.relative_to(ROOT).as_posix() for p in sorted((ROOT / "store/migrations").glob("*.sql"))]
    return {name: hash_file(ROOT / name) for name in names}


def provision(run: str) -> dict:
    import psycopg
    from psycopg import sql
    from evals.world import load_world

    config = read_json(ROOT / ".genesis/local/p00-world.json")
    world = load_world(ROOT / ".genesis/local/p00-world.json", "memory_repro")
    # This checks database/role, instance marker and physical cluster identity.
    with world._connect() as conn:
        actual = Path(conn.execute("SHOW data_directory").fetchone()[0])
        if actual.resolve() != (ROOT / ".genesis/local/p00-services/pgdata").resolve():
            raise RuntimeError("Refusing migration outside the dedicated P00 data directory")
        conn.commit()
        conn.autocommit = True
        database = "bank_p00_memory_test_" + run
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    parts = urlsplit(config["dsn"])
    dsn = urlunsplit(parts._replace(path="/" + database))
    role, password = "p00_mem_" + run, secrets.token_hex(24)
    app_dsn = urlunsplit(parts._replace(netloc=f"{role}:{password}@127.0.0.1:{parts.port}", path="/" + database))
    local = dict(dsn=dsn, app_dsn=app_dsn, role=role, password=password,
                 database=database, instance=world.instance, system_identifier=world.system_identifier,
                 backend=config["backend"], postgres_version=config["postgres_version"])
    write_json(ROOT / f".genesis/local/memory-repro-{run}.json", local)
    # Every migration DSN and template value is explicit; infra/.env is not used.
    os.environ.update(DATABASE_URL=dsn, APP_DATABASE_URL=app_dsn, APP_DB_USER=role,
                      APP_DB_PASSWORD=password, POSTGRES_DB=database, EMBEDDING_DIM="1024",
                      LITELLM_LOCAL_MODEL_COST_MAP="True", CONTEXT_TOP_K="10")
    from store import db
    db._env_loaded = True
    return local


def restrict_network(port: int):
    original = socket.socket.connect

    def connect(sock, address):
        if not isinstance(address, tuple) or address[:2] != ("127.0.0.1", port):
            raise RuntimeError("T00.3 reproduction permits only its dedicated local PostgreSQL endpoint")
        return original(sock, address)

    return patch.object(socket.socket, "connect", connect)


async def exercise(fixture: dict, admin: str, destination: Path) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row
    from capture.extract import _coerce_candidates
    from store.db import close_pools
    from store.memories import persist_candidates
    from retrieve.keyword import keyword_search
    from retrieve.types import RetrievalQuery
    from context.composer import compose
    from graphs import response_graph

    cases = []
    try:
        for case in fixture["cases"]:
            subject = str(uuid.uuid4())
            trace = []
            snapshots = []
            for ordinal, item in enumerate(case["completion_order"], start=1):
                raw = dict(fixture[item])
                if item == "old":
                    raw["attribute"] = case["old_attribute"]
                # Real coercion, explicitly scripted extraction output.
                candidates = _coerce_candidates([raw])
                if len(candidates) != 1:
                    raise AssertionError("Synthetic extraction fixture did not yield exactly one candidate")
                vector = [0.0] * 1024
                vector[case["source_order"].index(item)] = 1.0
                candidate = candidates[0].with_(embedding=vector, importance=0.7, confidence=0.9)
                accepted_position = case["source_order"].index(item) + 1
                result = await persist_candidates(subject, subject, [candidate])
                trace.append(dict(scripted_source_position=accepted_position, completion_position=ordinal,
                                  completed_at=datetime.now(timezone.utc).isoformat(), input_name=item,
                                  extraction_origin="scripted-provider-output/real-coercion", raw_extraction_output=raw,
                                  coerced_candidate=candidate.summary(), persistence_results=result))
                with psycopg.connect(admin, row_factory=dict_row) as conn:
                    rows = conn.execute("SELECT id,subject_id,actor_id,content,source,attribute,created_at,updated_at,"
                                        "superseded_at,superseded_by,deleted_at,stale_at,reinforcement_count "
                                        "FROM memories WHERE subject_id=%s ORDER BY created_at,id", (subject,)).fetchall()
                snapshots.append(dict(after_completion=ordinal, rows=rows))
            retrieved = await keyword_search(RetrievalQuery(text=fixture["keyword_query"],
                                                            subject_id=subject, actor_id=subject))
            if not retrieved:
                raise AssertionError("Empty retrieval cannot characterize stale-context exposure")
            context = compose(retrieved, budget=4096, top_k=10)
            if set(context.memory_ids) != {c.memory_id for c in retrieved}:
                raise AssertionError("Incident context unexpectedly dropped a fixture row")
            messages = [{"role": "user", "content": fixture["query"]}]
            # Real prompt assembly and respond node; only the responder is stubbed.
            async def deterministic_responder(prompt):
                yield "[DETERMINISTIC STUB; not a model answer] Retrieved fixture memories: " + " | ".join(c.content for c in retrieved)

            with patch.object(response_graph, "stream_tokens", deterministic_responder):
                response = await response_graph.respond_node(dict(memory_block=context.block, messages=messages))
            with psycopg.connect(admin, row_factory=dict_row) as conn:
                audit = conn.execute("SELECT id,action,memory_id,metadata,created_at FROM audit_log "
                                     "WHERE subject_id=%s ORDER BY created_at,id", (subject,)).fetchall()
            contents = [c.content for c in retrieved]
            latest = fixture["new"]["text"]
            old = fixture["old"]["text"]
            record = dict(case_id=case["id"], subject_id=subject, source_order=case["source_order"],
                          completion_order=case["completion_order"], trace=trace, row_snapshots=snapshots,
                          retrieval=dict(layer="production keyword SQL; no semantic provider", contents=contents,
                                         candidates=[c.to_dict() for c in retrieved]),
                          context=context.to_dict(), prompt=response["prompt"],
                          answer=dict(kind="deterministic-stub", text=response["reply"], live_model_answer=None), audit_rows=audit,
                          observations=dict(old_value_exposed=old in contents,
                                            newest_source_value_exposed=latest in contents,
                                            sole_value_is_newest_source=contents == [latest]))
            if case["id"] == "typed_control" and not record["observations"]["sole_value_is_newest_source"]:
                raise AssertionError("Typed in-order control failed; cannot attribute the other cases")
            write_json(destination / (case["id"] + ".json"), record)
            cases.append(record)
    finally:
        await close_pools()
    return cases


def verify(destination: Path) -> None:
    report = read_json(destination / "report.json")
    required = {"task_id", "status", "source_hashes", "environment", "cases", "limitations",
                "artifacts", "original_live_incident", "model", "reset", "grader", "command"}
    if not required.issubset(report) or report["task_id"] != "T00.3":
        raise ValueError("Incomplete T00.3 report identity")
    if report["original_live_incident"]["status"] != "not-reproduced":
        raise ValueError("Synthetic fixture evidence must not claim the original live incident")
    if report["status"] != "observed" or len(report["cases"]) != 3:
        raise ValueError("Reproduction run is incomplete or invalid")
    case_ids = {"legacy_null_attribute", "typed_control", "inverted_completion"}
    if {case["case_id"] for case in report["cases"]} != case_ids:
        raise ValueError("Missing or duplicate reproduction case")
    if not {"migrations.json", *(name + ".json" for name in case_ids)}.issubset(report["artifacts"]):
        raise ValueError("Missing required reproduction artifacts")
    for name, expected in report["artifacts"].items():
        file = destination / name
        if file.resolve().parent != destination.resolve() or hash_file(file) != expected:
            raise ValueError("Reproduction artifact hash mismatch: " + name)
    for case in report["cases"]:
        detail = read_json(destination / (case["case_id"] + ".json"))
        if (detail["observations"] != case["observations"]
                or detail["answer"]["live_model_answer"] is not None
                or detail["answer"]["kind"] != "deterministic-stub"):
            raise ValueError("Summary/detail or answer-provenance mismatch")
        if case["case_id"] == "typed_control" and not detail["observations"]["sole_value_is_newest_source"]:
            raise ValueError("Typed control failed")
    if source_hashes() != report["source_hashes"]:
        raise ValueError("Reproduction sources changed since observation")
    print("T00.3 report and artifact/source hashes verified. Original live incident remains not-reproduced.")


def capture(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    run = uuid.uuid4().hex[:12]
    before = source_hashes()
    fixture = read_json(FIXTURE)
    config = provision(run)
    report = dict(task_id="T00.3", status="invalid", run_id=run,
                  recorded_at=datetime.now(timezone.utc).isoformat(), command=[sys.executable, *sys.argv],
                  source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                  source_hashes=before, data=fixture["fixture_version"], policy="not-applicable-to-memory-fixture",
                  model="not-called; scripted extraction and deterministic responder", tool="production-memory-keyword-prompt-seams",
                  grader="p00-memory-observation-and-control-v1", reset="new-database-and-role-per-run; no existing data reset",
                  environment={k: config[k] for k in ("database", "role", "instance", "system_identifier", "backend", "postgres_version")},
                  original_live_incident=dict(status="not-reproduced", reason="Original rows, source turns, extraction results and provider configuration unavailable."),
                  cases=[], artifacts={}, reviewer=None,
                  limitations=fixture["limitations"] + ["Service-layer fixture; no HTTP/authentication or full capture graph exercised.",
                                                       "Recorded source positions are harness metadata; current application has no accepted_user_events sequence.",
                                                       "No P02 memory correction/migration fix is implemented by this task."])
    report["environment"].update(python=sys.version,
                                dependencies={name: importlib.metadata.version(name) for name in
                                              ("psycopg", "psycopg-pool", "langgraph", "tiktoken", "litellm")})
    try:
        from store.migrate import apply_all
        migration_log = io.StringIO()
        with redirect_stdout(migration_log):
            count = apply_all(config["dsn"], verbose=False)
        write_json(destination / "migrations.json", dict(applied_files=count, status="applied-on-new-test-database"))
        from store.db import ensure_selector_event_loop_policy
        ensure_selector_event_loop_policy()
        # Windows implements the event loop's self-pipe using a local socket
        # pair. Initialize it before restricting application network endpoints.
        with asyncio.Runner() as runner:
            with restrict_network(urlsplit(config["dsn"]).port):
                cases = runner.run(exercise(fixture, config["dsn"], destination))
        report["cases"] = [dict(case_id=c["case_id"], observations=c["observations"],
                                finding_status="reproduced-in-synthetic-persistence" if c["observations"]["old_value_exposed"] else "not-reproduced-in-control") for c in cases]
        if source_hashes() != before:
            raise RuntimeError("Source changed during reproduction")
        report["status"] = "observed"
    except Exception as exc:
        # Retain failure type without printing credential-bearing connection data.
        report["failure_type"] = type(exc).__name__
        message = str(exc)
        for secret in (config["dsn"], config["app_dsn"], config["password"], urlsplit(config["dsn"]).password):
            if secret:
                message = message.replace(secret, "[redacted]")
        write_json(destination / "failure.json", dict(type=type(exc).__name__, message=message))
        raise
    finally:
        report["artifacts"] = {p.name: hash_file(p) for p in sorted(destination.glob("*.json")) if p.name != "report.json"}
        write_json(destination / "report.json", report)
    verify(destination)
    for case in report["cases"]:
        print(case["case_id"], json.dumps(case["observations"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["capture", "verify"])
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    (capture if args.action == "capture" else verify)(args.directory)
