"""Node 2 -- Presidio PII scrubbing (plan step 3).

This node is the reason the pipeline has a fixed order. It sits between
`extract` and everything that touches the database, so raw PII has no path to
the `content` column: the redacted string replaces the original on the
`Candidate` the moment it leaves this node, and `store/memories.py` is only
ever handed `Candidate.text`. The original survives only on `raw_text`, which
lives in process memory for logging and is never passed to a query.

Required entity coverage (plan): SSN, email, phone, credit card *at minimum*.
Presidio's stock recognizers cover all four, but two of them are soft:

* ``US_SSN`` scores low and can miss the unpunctuated ``123456789`` form.
* ``PHONE_NUMBER`` depends on the `phonenumbers` heuristics and skips some
  otherwise-obvious US formats.

So the two are reinforced with extra ``PatternRecognizer``s registered into
Presidio's own registry -- this is Presidio's supported extension point, not a
regex layer bolted on beside it, so the engine still does the analysis and
overlap resolution.

The spaCy model is ``en_core_web_sm``. TRADEOFF: it is ~12 MB against
``en_core_web_lg``'s ~560 MB, and it is measurably weaker at *NER-driven*
entities (PERSON, LOCATION, NRP). Every entity this milestone is required to
redact -- SSN, EMAIL_ADDRESS, PHONE_NUMBER, CREDIT_CARD -- is detected by
pattern/checksum recognizers that do not consult the NER model at all, so the
small model costs nothing on the required set. Swap to `lg` (`python -m spacy
download en_core_web_lg`, then set ``PII_SPACY_MODEL``) if name redaction
becomes a requirement.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any

from capture import config as capture_config
from capture.metrics import PII_ENTITIES, log_warning, node_span
from graphs.capture_state import Candidate, CaptureState

# Entities we always ask Presidio for. Ordering is irrelevant; Presidio resolves
# overlapping spans itself.
TARGET_ENTITIES = [
    "US_SSN",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "CREDIT_CARD",
    "IBAN_CODE",
    "IP_ADDRESS",
    "US_PASSPORT",
    "US_DRIVER_LICENSE",
    "US_BANK_NUMBER",
]

_engines: dict[str, Any] = {}
_engine_lock = threading.Lock()


def spacy_model_name() -> str:
    return os.environ.get("PII_SPACY_MODEL", "en_core_web_sm")


def _reinforcement_recognizers() -> list[Any]:
    """Extra patterns for the two stock recognizers that under-fire."""
    from presidio_analyzer import Pattern, PatternRecognizer

    ssn = PatternRecognizer(
        supported_entity="US_SSN",
        name="ssn_reinforcement",
        patterns=[
            # 123-45-6789 / 123 45 6789 / 123.45.6789
            Pattern(name="ssn_delimited", regex=r"\b\d{3}[-.\s]\d{2}[-.\s]\d{4}\b", score=0.85),
            # 123456789 -- weak on its own, hence the lower score; Presidio will
            # prefer a higher-scoring overlapping match if one exists.
            Pattern(name="ssn_plain", regex=r"\b(?!000|666|9\d\d)\d{3}(?!00)\d{2}(?!0000)\d{4}\b", score=0.45),
        ],
        context=["ssn", "social security", "social-security", "social security number"],
    )
    phone = PatternRecognizer(
        supported_entity="PHONE_NUMBER",
        name="phone_reinforcement",
        patterns=[
            Pattern(
                name="us_phone",
                regex=r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b",
                score=0.7,
            ),
        ],
        context=["phone", "call", "mobile", "cell", "number"],
    )
    return [ssn, phone]


def _build_engines() -> dict[str, Any]:
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_anonymizer import AnonymizerEngine

    provider = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": spacy_model_name()}],
        }
    )
    analyzer = AnalyzerEngine(nlp_engine=provider.create_engine(), supported_languages=["en"])
    for recognizer in _reinforcement_recognizers():
        analyzer.registry.add_recognizer(recognizer)
    return {"analyzer": analyzer, "anonymizer": AnonymizerEngine()}


def get_engines() -> dict[str, Any]:
    """Lazily build the Presidio engines once per process.

    Loading spaCy costs seconds; doing it at import time would make every
    unrelated test import pay for it. Guarded by a lock because the capture
    worker runs several jobs concurrently.
    """
    global _engines
    if not _engines:
        with _engine_lock:
            if not _engines:
                _engines = _build_engines()
    return _engines


def placeholder_for(entity_type: str) -> str:
    return f"[REDACTED_{entity_type}]"


def redact(text: str, *, score_threshold: float | None = None) -> tuple[str, list[str]]:
    """Return `(redacted_text, sorted_entity_types_found)`.

    On any Presidio failure this **fails closed**: it returns a wholly redacted
    string rather than the original. A PII scrubber that passes text through
    when it breaks is worse than no scrubber, because everything downstream
    assumes this node succeeded.
    """
    if not text or not text.strip():
        return text, []

    threshold = (
        score_threshold if score_threshold is not None else capture_config.pii_score_threshold()
    )

    try:
        engines = get_engines()
        results = engines["analyzer"].analyze(
            text=text,
            language="en",
            entities=TARGET_ENTITIES,
            score_threshold=threshold,
        )
    except Exception as exc:
        log_warning("capture.pii.analyzer_error", error=f"{type(exc).__name__}: {exc}")
        return "[REDACTED_UNSCANNABLE]", ["UNSCANNABLE"]

    if not results:
        return text, []

    entity_types = sorted({r.entity_type for r in results})

    try:
        from presidio_anonymizer.entities import OperatorConfig

        operators = {
            entity: OperatorConfig("replace", {"new_value": placeholder_for(entity)})
            for entity in entity_types
        }
        anonymized = engines["anonymizer"].anonymize(
            text=text, analyzer_results=results, operators=operators
        )
        return anonymized.text, entity_types
    except Exception as exc:
        log_warning("capture.pii.anonymizer_error", error=f"{type(exc).__name__}: {exc}")
        return "[REDACTED_UNSCANNABLE]", ["UNSCANNABLE"]


def redact_candidate(candidate: Candidate) -> Candidate:
    """Replace a candidate's text with its redacted form, recording entity types."""
    redacted_text, entities = redact(candidate.text)
    return candidate.with_(
        text=redacted_text,
        raw_text=candidate.text,
        pii_entities=entities,
    )


async def pii_node(state: CaptureState) -> dict[str, Any]:
    """LangGraph node: state['candidates'] -> state['redacted'].

    Runs on the event loop thread. Presidio is CPU-bound but operates on short
    single-sentence candidates, so the per-call cost is sub-millisecond after
    the one-time model load; offloading to a thread would cost more in context
    switches than it saves. The one-time spaCy load happens inside the worker
    task, never on the request path.
    """
    subject_id = state.get("subject_id", "")
    candidates = state.get("candidates") or []

    with node_span("pii", subject_id, n_in=len(candidates)) as span:
        redacted = [redact_candidate(c) for c in candidates]
        found: list[str] = []
        for candidate in redacted:
            for entity in candidate.pii_entities:
                PII_ENTITIES.labels(entity_type=entity).inc()
                found.append(entity)
        span["out"] = len(redacted)
        span["pii_entities"] = sorted(set(found))
        span["pii_entity_count"] = len(found)

    return {"redacted": redacted}
