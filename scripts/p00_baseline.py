"""Capture an immutable P00 baseline without loading credentials or calling models.

Run with the intended project interpreter. Failed probes are evidence, not hidden.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def probe(command: list[str], timeout: int = 45) -> dict:
    started = datetime.now(timezone.utc).isoformat()
    try:
        result = subprocess.run(command, cwd=ROOT, text=True, encoding="utf-8",
                                errors="replace", capture_output=True, timeout=timeout)
        return dict(command=command, started_at=started, exit_code=result.returncode,
                    stdout=result.stdout, stderr=result.stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return dict(command=command, started_at=started, exit_code=None,
                    stdout="", stderr=str(exc), status="unavailable-or-timeout")


def capture(destination: Path) -> None:
    # Exclusive directory creation prevents rewriting an earlier observation.
    destination.mkdir(parents=True, exist_ok=False)
    commands = [["git", "rev-parse", "HEAD"], ["git", "branch", "--show-current"],
                ["git", "status", "--short"], ["docker", "version"],
                ["docker", "compose", "version"], ["node", "--version"],
                [sys.executable, "-m", "pip", "check"],
                [sys.executable, "-c", "import fastapi, psycopg, redis, langgraph, litellm; print('imports ok')"],
                [sys.executable, "-m", "pytest", "--version"]]
    observations = []
    for index, command in enumerate(commands):
        result = probe(command)
        log = destination / f"command-{index:02d}.json"
        log.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        observations.append(dict(path=log.name, sha256=sha256(log),
                                 command=command, exit_code=result["exit_code"]))
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    source_hashes = {name: sha256(ROOT / name) for name in tracked if name and (ROOT / name).is_file()}
    with tarfile.open(destination / "source.tar.gz", "w:gz") as archive:
        for name in source_hashes:
            archive.add(ROOT / name, arcname=name, recursive=False)
    inventory = []
    for file in sorted((ROOT / "tests").rglob("test_*.py")):
        content = file.read_text(encoding="utf-8")
        tree = ast.parse(content)
        signals = [token for token in ("session(", "psycopg", "redis", "llm_config",
                   "complete(", "embed(", "httpx", "subprocess") if token in content]
        inventory.append(dict(path=file.relative_to(ROOT).as_posix(),
                              tests=sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                                        and n.name.startswith("test_") for n in ast.walk(tree)),
                              classification="requires-source-review", dependency_signals=signals))
    manifest = dict(schema_version=1, task_id="T00.1", status="observed",
                    recorded_at=datetime.now(timezone.utc).isoformat(),
                    reviewed_commit="e5206789b5914d3c93f989e210bebd497f2e4f47",
                    implementation_start_commit=json.loads((destination / "command-00.json").read_text())["stdout"].strip(),
                    branch=json.loads((destination / "command-01.json").read_text())["stdout"].strip(),
                    runtime=dict(python=sys.version, executable=sys.executable, platform=platform.platform()),
                    dependencies=sorted((d.metadata["Name"], d.version) for d in importlib.metadata.distributions()),
                    source_hashes=source_hashes, source_archive_sha256=sha256(destination / "source.tar.gz"),
                    observations=observations, test_inventory=inventory,
                    package_entries={"python": "pyproject.toml [tool.setuptools].packages",
                                     "frontend": "frontend/package.json"},
                    model="not-called", tool="p00-baseline-v1", policy="not-applicable-to-baseline",
                    grader="command-exit-and-record-integrity-v1", reset="not-run",
                    data="tracked-source-only", reviewer=None,
                    limitations=["Import/service failures are retained; this is not a passing application suite.",
                                 "Test directory names and markers do not establish offline safety.",
                                 "No infra/.env, inherited credentials, model calls or migrations are used."])
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Baseline captured: {destination}; {len(observations)} observations (inspect failures).")


def verify(destination: Path) -> None:
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["implementation_start_commit"], "missing source identity"
    assert sha256(destination / "source.tar.gz") == manifest["source_archive_sha256"]
    for item in manifest["observations"]:
        assert sha256(destination / item["path"]) == item["sha256"], item["path"]
    assert manifest["model"] == "not-called"
    print("Baseline artifact integrity verified; individual probe failures remain recorded.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["capture", "verify"])
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    (capture if args.action == "capture" else verify)(args.directory)
