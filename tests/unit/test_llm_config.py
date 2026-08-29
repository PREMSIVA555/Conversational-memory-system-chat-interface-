"""M1 unit test — the LLM seam reads its model from the environment.

This is the guard for plan step 14: "no model name hardcoded anywhere else in
the codebase". If someone pins a model literal inside a caller, this test still
passes — so it is paired with an explicit grep-style check below that the only
model literals in the source tree live in llm/config.py's DEFAULTS.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from llm import config as llm_config

ROOT = Path(__file__).resolve().parents[2]


def test_llm_config_reads_model_from_env(monkeypatch: pytest.MonkeyPatch):
    """Monkeypatching LLM_MODEL changes the resolved model — nothing is baked in."""
    monkeypatch.setenv("LLM_MODEL", "groq/openai/gpt-oss-120b")
    assert llm_config.resolve_completion_model() == "groq/openai/gpt-oss-120b"

    monkeypatch.setenv("LLM_MODEL", "some/other-provider/model-x")
    assert llm_config.resolve_completion_model() == "some/other-provider/model-x"

    monkeypatch.setenv("EMBEDDING_MODEL", "voyage/voyage-3-large")
    assert llm_config.resolve_embedding_model() == "voyage/voyage-3-large"

    monkeypatch.setenv("EMBEDDING_DIM", "512")
    assert llm_config.resolve_embedding_dim() == 512


def test_max_tokens_never_drops_below_the_gpt_oss_floor(monkeypatch: pytest.MonkeyPatch):
    """gpt-oss burns budget on reasoning before emitting content; 512 is the floor."""
    monkeypatch.setenv("LLM_MAX_TOKENS", "16")
    assert llm_config.default_max_tokens() >= llm_config.MIN_MAX_TOKENS == 512


def test_no_model_literals_outside_llm_config():
    """The only provider-prefixed model strings in the tree are llm/config.py DEFAULTS."""
    pattern = re.compile(r"\b(groq|voyage|openai|anthropic)/[A-Za-z0-9._\-]+/?[A-Za-z0-9._\-]*")
    offenders: list[str] = []

    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith((".venv/", "tests/", "build/")) or rel == "llm/config.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # documentation, not a call site
            if pattern.search(line):
                offenders.append(f"{rel}:{lineno}: {stripped}")

    assert not offenders, (
        "model names must only appear in llm/config.py:\n  " + "\n  ".join(offenders)
    )
