"""SPEC08/INV09: offline immutable synthetic catalog; exact eligible retrieval.

No customer records, generated prose, HTTP routes or live embedding calls. The
caller supplies an embedding from the manifest's model. Runtime has SELECT only.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Annotated
import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

Label = Annotated[str, Field(pattern=r'^[a-zA-Z0-9_.:-]{1,80}$')]


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


def instant(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Explicit timezone required')
    return value.astimezone(timezone.utc)


def vector(value, dimensions):
    if not isinstance(value, list) or len(value) != dimensions:
        raise ValueError('Embedding dimensions do not match')
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in value):
        raise ValueError('Invalid embedding')
    norm = math.hypot(*value)
    if not math.isfinite(norm) or norm == 0:
        raise ValueError('Invalid embedding norm')
    return [x / norm for x in value]


class Chunk(Strict):
    section: Label
    text: Annotated[str, Field(min_length=1, max_length=20000)]
    embedding: list[float | int]


class Document(Strict):
    version: Label
    rule: Label
    product: Label
    scope: Label
    locale: Label
    source: Annotated[str, Field(min_length=1, max_length=200000)]
    source_hash: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
    effective_from: str
    effective_until: str
    expected_chunks: Annotated[int, Field(ge=1, le=1000)]
    chunks: list[Chunk]

    @model_validator(mode='after')
    def complete(self):
        start, end = instant(self.effective_from), instant(self.effective_until)
        if start >= end:
            raise ValueError('Empty interval')
        if hashlib.sha256(self.source.encode()).hexdigest() != self.source_hash:
            raise ValueError('Source hash mismatch')
        if len(self.chunks) != self.expected_chunks or len({c.section for c in self.chunks}) != len(self.chunks):
            raise ValueError('Incomplete or duplicate sections')
        if any(c.text not in self.source for c in self.chunks):
            raise ValueError('Chunk is not present in source')
        self.effective_from, self.effective_until = start.isoformat(), end.isoformat()
        return self


class Manifest(Strict):
    synthetic: bool
    embedding_model: Label
    dimensions: Annotated[int, Field(ge=1, le=4096)]
    expected_documents: Annotated[int, Field(ge=1, le=1000)]
    documents: list[Document]

    @model_validator(mode='after')
    def complete(self):
        if not self.synthetic or len(self.documents) != self.expected_documents:
            raise ValueError('Incomplete synthetic manifest')
        if sum(len(d.chunks) for d in self.documents) > 10000:
            raise ValueError('Exact retrieval corpus limit exceeded')
        if len({d.version for d in self.documents}) != len(self.documents):
            raise ValueError('Duplicate version')
        for i, doc in enumerate(self.documents):
            for chunk in doc.chunks:
                vector(chunk.embedding, self.dimensions)
            for other in self.documents[:i]:
                if (doc.rule, doc.product, doc.scope, doc.locale) == (other.rule, other.product, other.scope, other.locale):
                    if max(instant(doc.effective_from), instant(other.effective_from)) < min(instant(doc.effective_until), instant(other.effective_until)):
                        raise ValueError('Conflicting policy intervals')
        return self


def stage(conn, value):
    """Offline migrator connection only; invalid/partial data never reaches SQL."""
    manifest = Manifest.model_validate(value).model_dump()
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with conn.transaction():
        conn.execute('INSERT INTO banking.policy_sets(id,manifest) VALUES(%s,%s) ON CONFLICT(id) DO NOTHING', (digest, Jsonb(manifest)))
    return digest


def activate(conn, digest, *, expected):
    """Compare-and-swap prevents a stale operator replacing a newer catalog."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('banking.policy_activation'))")
        row = conn.execute('SELECT set_id FROM banking.policy_active WHERE singleton').fetchone()
        if (row[0] if row else None) != expected:
            raise ValueError('Activation conflict')
        row = conn.execute('SELECT manifest FROM banking.policy_sets WHERE id=%s', (digest,)).fetchone()
        if not row:
            raise ValueError('Unknown policy set')
        Manifest.model_validate(row[0])
        conn.execute('INSERT INTO banking.policy_active(singleton,set_id) VALUES(true,%s) ON CONFLICT(singleton) DO UPDATE SET set_id=excluded.set_id', (digest,))


def lexical(conn, candidates, question):
    return dict(conn.execute("""SELECT item->>'key', banking.policy_lexical(item->>'text',%s)
        FROM jsonb_array_elements(%s::jsonb) AS item
        WHERE banking.policy_lexical(item->>'text',%s)>0""", (question, Jsonb(candidates), question)).fetchall())


def retrieve(conn, *, product, scope, locale, question, embedding=None, embedding_model=None, at=None, limit=5, include_context=False):
    """Own a read-only snapshot; return citations/evidence, never answer support.

    Exact cosine is computed over the eligible snapshot (bounded 10k chunks).
    Reciprocal rank fusion k=60 combines measured lanes; no ANN/reranker/cache.
    A missing vector is an explicit embedding-lane outage, not a silent fallback.
    """
    for label in (product, scope, locale):
        if not isinstance(label, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,80}', label):
            raise ValueError('Invalid policy filter')
    if type(question) is not str or not 1 <= len(question) <= 4000 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('Invalid retrieval query')
    if type(include_context) is not bool:raise ValueError('Invalid context option')
    requested = instant(at) if at is not None else datetime.now(timezone.utc)
    base = dict(applicability='requested' if at is not None else 'current', effective_at=requested.isoformat(),
                evidence=[], lanes={}, answer_support='not_evaluated')
    # Existing customer/session services call this only after authorization. This
    # corpus is deliberately public synthetic policy, with no tenant payloads.
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValueError('Retrieval requires an idle dedicated connection')
    with conn.transaction():
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.execute("SET LOCAL statement_timeout='1800ms'")
        row = conn.execute('SELECT s.id,s.manifest FROM banking.policy_active a JOIN banking.policy_sets s ON s.id=a.set_id WHERE a.singleton').fetchone()
        if row is None:
            return dict(base, status='missing', set_id=None)
        digest, raw = row
        try:
            manifest = Manifest.model_validate(raw)
        except ValueError:
            return dict(base, status='invalid_catalog', set_id=digest)
        candidates = []
        for d in manifest.documents:
            if (d.product, d.scope, d.locale) != (product, scope, locale) or not instant(d.effective_from) <= requested < instant(d.effective_until):
                continue
            for c in d.chunks:
                candidates.append(dict(key=json.dumps([d.version,c.section],separators=(',',':')), version=d.version, section=c.section,
                    text=c.text, source_hash=d.source_hash, product=d.product, scope=d.scope, locale=d.locale,
                    effective_from=d.effective_from, effective_until=d.effective_until, embedding=c.embedding))
        if not candidates:
            return dict(base, status='missing', set_id=digest)
        scores = {}
        lanes = {}
        if embedding is None:
            lanes['vector'] = 'unavailable'
        else:
            if embedding_model != manifest.embedding_model:
                raise ValueError('Embedding model does not match')
            unit = vector(embedding, manifest.dimensions)
            scores['vector'] = {c['key']: math.fsum(a*b for a,b in zip(unit, vector(c['embedding'], manifest.dimensions))) for c in candidates}
            lanes['vector'] = 'ok'
        try:
            with conn.transaction():  # savepoint: lexical SQL failure must not poison snapshot
                scores['lexical'] = lexical(conn, candidates, question)
            lanes['lexical'] = 'ok'
        except psycopg.Error:
            lanes['lexical'] = 'unavailable'
        fused = {}
        rankings = {}
        for lane, values in scores.items():
            ranked = sorted(values, key=lambda k: (-values[k], k))
            rankings[lane] = ranked
            for rank, key in enumerate(ranked, 1):
                fused[key] = fused.get(key, 0) + 1/(60+rank)
        evidence = []
        bykey = {c['key']:c for c in candidates}
        for key in sorted(fused, key=lambda k: (-fused[k], k))[:limit]:
            evidence.append({k:v for k,v in bykey[key].items() if k != 'embedding'} | dict(score=fused[key]))
        result=dict(base, set_id=digest, status=('unavailable' if not scores else 'degraded' if 'unavailable' in lanes.values() else 'ok'),
                    lanes=lanes, rankings=rankings, evidence=evidence,complete=len(evidence)==len(candidates))
        if include_context:
            # Required context is fetched from this SAME eligible immutable
            # snapshot, independently of ranking. Never silently drop clauses
            # that did not match a lexical query. Over budget => narrow answer.
            result['context']=[{k:v for k,v in c.items() if k!='embedding'} for c in candidates] if scores and len(candidates)<=limit else []
            result['complete']=bool(scores) and len(result['context'])==len(candidates)
        return result
