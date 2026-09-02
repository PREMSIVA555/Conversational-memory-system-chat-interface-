"""Reflection: consolidate a cluster of raw memories into one summary (M8 steps 7, 9).

    select_cluster -> summarize -> pii -> embed -> write

WHERE THE CLUSTER COMES FROM
----------------------------
No new embedding call. Every candidate row already has a vector in the
`embedding` column, so "which memories belong together" is answered by pgvector
in the database rather than by a provider round-trip. The query self-joins the
subject's un-consolidated rows on cosine distance, counts each row's neighbours
within `CLUSTER_MAX_DISTANCE`, and takes the densest one as the seed; the
cluster is that seed plus its nearest neighbours, closest first.

That matters more than it looks. Voyage is metered at 3 requests/minute on this
account, so a clustering strategy that re-embedded anything would make the
reflection job the most expensive thing in the system while adding nothing —
the vectors it would produce are the vectors already stored.

Ties are broken on `id` so a run over an unchanged table picks the same cluster
twice, which is what makes the job debuggable.

WHY `consolidated_at` EXISTS
---------------------------
Without it the job re-summarises the same densest cluster every night and the
store fills with near-identical summaries — and each summary, being about the
same theme, joins the cluster and makes it denser still. Marking the sources
consolidated is what makes the job converge. Rows already folded into a summary
are excluded from cluster selection, as are summaries themselves
(`source = 'reflection'`), so reflection never reflects on its own output.

RLS AND THE CONNECTION — the opposite choice from the decay job
---------------------------------------------------------------
Reflection runs on the **application** connection, through
`store.db.session(subject_id, actor_id)`, inside row-level security, exactly
like the capture writer. It has to: it reads `content`, it creates a new
`memories` row, and it writes `audit_log` rows — and `audit_log`'s INSERT policy
reads the `app.subject_id` / `app.actor_id` GUCs, so an unscoped connection is
rejected by the database rather than quietly writing an unattributable row.

It is also *possible*, unlike the decay claim query: reflection is inherently
per-subject (you cannot summarise a theme across two people's memories), so
there is always a subject whose GUCs can be set. The owner-connection carve-out
in `jobs/claims.py` exists because the decay sweep genuinely has no such
subject; it is not a general licence, and this module is where that shows.

`list_subjects_with_candidates()` — which does span subjects — is the one owner-
connection read here, and it returns nothing but subject ids and counts.

AUDIT (step 9)
--------------
Two kinds of row, both through M7's `write_audit()` on the same connection and
therefore in the same transaction as the write they describe:

  action='write'   one row for the summary memory that was created.
  action='update'  one row per source memory, because marking a user's memory
                   as consolidated is a mutation of their data and the trail is
                   supposed to say what happened to it.

Different `memory_id`s throughout, so M7's same-transaction duplicate guard
never fires and `allow_repeat` is not needed. Nothing here ever UPDATEs or
DELETEs `audit_log`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Sequence

from store.audit import UPDATE as AUDIT_UPDATE
from store.audit import WRITE as AUDIT_WRITE
from store.audit import write_audit
from store.db import admin_session, session
from store.memories import insert_memory

__all__ = [
    "REFLECTION_SOURCE",
    "ClusterMember",
    "Cluster",
    "cluster_max_distance",
    "min_cluster_size",
    "max_cluster_size",
    "select_cluster",
    "summarize_cluster",
    "write_summary",
    "list_subjects_with_candidates",
    "SUMMARY_SYSTEM_PROMPT",
]

#: The `source` value that marks a memory as machine-derived rather than
#: captured from a conversation. Read by the cluster query (to exclude
#: summaries) and asserted on by `test_reflection_writes_summary_memory`.
REFLECTION_SOURCE = "reflection"


DEFAULTS = {
    # Cosine DISTANCE (not similarity) below which two memories count as
    # neighbours. 0.45 on voyage-3.5 over natural sentences: unrelated text sits
    # around 0.75 and a genuine same-topic pair around 0.25-0.45, so this is the
    # loose end of "about the same thing". Loose on purpose — a cluster the job
    # declines to find costs a whole night, while a slightly ragged cluster
    # costs one mediocre sentence in a summary.
    "REFLECTION_MAX_DISTANCE": "0.45",
    # Fewer than this and there is nothing to consolidate: a "summary" of two
    # memories is just a worse third memory.
    "REFLECTION_MIN_CLUSTER": "3",
    # Upper bound on one summary's sources, so the prompt stays small and one
    # run cannot swallow a subject's entire history into a single row.
    "REFLECTION_MAX_CLUSTER": "8",
    # How many candidate rows the density query considers. The self-join is
    # O(n^2) in this number, so it is bounded rather than "the whole table".
    "REFLECTION_CANDIDATE_LIMIT": "200",
}


def _env(name: str) -> str:
    from store.db import load_env

    load_env()
    value = os.environ.get(name)
    return DEFAULTS[name] if value is None or value == "" else value


def cluster_max_distance() -> float:
    return float(_env("REFLECTION_MAX_DISTANCE"))


def min_cluster_size() -> int:
    return max(2, int(_env("REFLECTION_MIN_CLUSTER")))


def max_cluster_size() -> int:
    return max(min_cluster_size(), int(_env("REFLECTION_MAX_CLUSTER")))


def candidate_limit() -> int:
    return max(min_cluster_size(), int(_env("REFLECTION_CANDIDATE_LIMIT")))


# ---------------------------------------------------------------------------
# step 7a — cluster selection
# ---------------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class ClusterMember:
    id: str
    content: str
    distance: float


@dataclass(slots=True, frozen=True)
class Cluster:
    subject_id: str
    seed_id: str
    members: tuple[ClusterMember, ...]

    @property
    def ids(self) -> list[str]:
        return [m.id for m in self.members]

    @property
    def texts(self) -> list[str]:
        return [m.content for m in self.members]

    def __len__(self) -> int:
        return len(self.members)


# Candidate rows: live, embedded, not already consolidated, and not themselves
# summaries. Bounded, most-recently-relevant first.
_CANDIDATES_CTE = """
WITH candidates AS (
    SELECT id, content, embedding
      FROM memories
     WHERE subject_id      = %(subject_id)s::uuid
       AND deleted_at      IS NULL
       AND consolidated_at IS NULL
       AND embedding       IS NOT NULL
       AND (source IS DISTINCT FROM %(reflection_source)s)
     ORDER BY last_accessed_at DESC, id
     LIMIT %(candidate_limit)s
)
"""

_SEED_SQL = _CANDIDATES_CTE + """
SELECT a.id AS seed_id, count(*) AS neighbours
  FROM candidates a
  JOIN candidates b
    ON b.id <> a.id
   AND (a.embedding <=> b.embedding) <= %(max_distance)s
 GROUP BY a.id
 ORDER BY neighbours DESC, a.id
 LIMIT 1
"""

_CLUSTER_SQL = _CANDIDATES_CTE + """
, seed AS (SELECT embedding FROM candidates WHERE id = %(seed_id)s::uuid)
SELECT c.id, c.content, (c.embedding <=> s.embedding) AS distance
  FROM candidates c CROSS JOIN seed s
 WHERE c.id = %(seed_id)s::uuid
    OR (c.embedding <=> s.embedding) <= %(max_distance)s
 ORDER BY distance, c.id
 LIMIT %(max_cluster)s
"""


async def select_cluster(
    conn: Any, *, subject_id: str, max_distance: float | None = None
) -> Cluster | None:
    """The densest un-consolidated cluster for `subject_id`, or None.

    Returns None — never raises, never returns a one-row "cluster" — when the
    subject has fewer than `min_cluster_size()` mutually-near memories. "There
    is nothing worth consolidating tonight" is the *normal* outcome for a small
    or diverse store, and the caller treats it as a no-op rather than an error.
    """
    distance = cluster_max_distance() if max_distance is None else float(max_distance)
    params = {
        "subject_id": str(subject_id),
        "reflection_source": REFLECTION_SOURCE,
        "candidate_limit": candidate_limit(),
        "max_distance": distance,
    }

    cursor = await conn.execute(_SEED_SQL, params)
    seed = await cursor.fetchone()
    if not seed:
        return None

    cursor = await conn.execute(
        _CLUSTER_SQL,
        {**params, "seed_id": str(seed["seed_id"]), "max_cluster": max_cluster_size()},
    )
    rows = await cursor.fetchall()
    if len(rows) < min_cluster_size():
        return None

    return Cluster(
        subject_id=str(subject_id),
        seed_id=str(seed["seed_id"]),
        members=tuple(
            ClusterMember(str(r["id"]), r["content"], float(r["distance"])) for r in rows
        ),
    )


# ---------------------------------------------------------------------------
# step 7b — summarization
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM_PROMPT = (
    "You consolidate a person's stored memories into ONE durable summary "
    "sentence.\n"
    "Rules:\n"
    "- Write a single sentence, at most 40 words, in the third person about the "
    "user.\n"
    "- State only what the source memories state. Invent nothing.\n"
    "- Name the shared theme explicitly, using the source memories' own nouns, "
    "so the summary is findable by the same searches the sources are.\n"
    "- Do not mention that this is a summary, and do not number or list the "
    "sources.\n"
    "Reply with the sentence and nothing else."
)


async def summarize_cluster(cluster: Cluster, *, max_tokens: int | None = None) -> str:
    """One completion. Returns the summary sentence (never None, may be empty).

    Reached through the `llm.config` MODULE rather than a `from ... import
    complete`, for the same reason `capture/embed.py` documents: a test that
    monkeypatches `llm.config.complete` must actually be observed here, and a
    name imported at module scope binds the original function forever.
    """
    from llm import config as llm_config

    body = "\n".join(f"- {text}" for text in cluster.texts)
    text = await llm_config.complete(
        [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": f"Source memories:\n{body}"},
        ],
        max_tokens=max_tokens,
    )
    return (text or "").strip()


# ---------------------------------------------------------------------------
# step 7c / step 9 — the write, its audit rows, and the consolidation marks
# ---------------------------------------------------------------------------

_CONSOLIDATE_SQL = """
UPDATE memories
   SET consolidated_at   = now(),
       consolidated_into = %(summary_id)s::uuid
 WHERE id = ANY(%(ids)s::uuid[])
   AND deleted_at      IS NULL
   AND consolidated_at IS NULL
RETURNING id
"""


async def write_summary(
    conn: Any,
    *,
    subject_id: str,
    actor_id: str,
    content: str,
    embedding: Sequence[float] | None,
    source_ids: Sequence[str],
    importance: float = 0.6,
    confidence: float = 0.7,
) -> dict[str, Any]:
    """Insert the summary, audit it, mark its sources, audit those. One transaction.

    `conn` must be an RLS-scoped application connection (`store.db.session`).
    Everything here is one transaction on purpose — the same rule M7's
    `store/audit.py` states: the audit row must commit or roll back with the
    action it describes. A summary that exists with no audit row, or a
    consolidation mark whose audit row survived a rolled-back write, would each
    be a hole in the trail.

    `consolidated_at IS NULL` in the UPDATE means a source another run already
    folded in is left alone and is absent from `consolidated`, so the returned
    count is the honest number of rows this call claimed.
    """
    summary_id = await insert_memory(
        subject_id,
        actor_id,
        content,
        embedding,
        REFLECTION_SOURCE,
        importance,
        confidence,
        conn=conn,
    )

    await write_audit(
        conn,
        subject_id=subject_id,
        actor_id=actor_id,
        action=AUDIT_WRITE,
        memory_id=summary_id,
        metadata={
            "job": "reflection",
            "source_count": len(source_ids),
            "source_ids": [str(i) for i in source_ids],
        },
    )

    consolidated: list[str] = []
    if source_ids:
        cursor = await conn.execute(
            _CONSOLIDATE_SQL,
            {"summary_id": summary_id, "ids": [str(i) for i in source_ids]},
        )
        consolidated = [str(row["id"]) for row in await cursor.fetchall()]

    for source_id in consolidated:
        await write_audit(
            conn,
            subject_id=subject_id,
            actor_id=actor_id,
            action=AUDIT_UPDATE,
            memory_id=source_id,
            metadata={"job": "reflection", "consolidated_into": summary_id},
        )

    return {"summary_id": summary_id, "consolidated": consolidated}


# ---------------------------------------------------------------------------
# subject discovery (scheduled runs)
# ---------------------------------------------------------------------------

_SUBJECTS_SQL = """
SELECT subject_id, actor_id, count(*) AS n
  FROM memories
 WHERE deleted_at      IS NULL
   AND consolidated_at IS NULL
   AND embedding       IS NOT NULL
   AND (source IS DISTINCT FROM %(reflection_source)s)
 GROUP BY subject_id, actor_id
HAVING count(*) >= %(minimum)s
 ORDER BY n DESC, subject_id
 LIMIT %(limit)s
"""


async def list_subjects_with_candidates(*, limit: int = 100) -> list[tuple[str, str]]:
    """`(subject_id, actor_id)` pairs with enough un-consolidated rows to try.

    THE ONE cross-subject read in this module, and the only reason it uses the
    owner connection: a scheduled reflection run has to discover *which*
    subjects to run for before it can scope itself to any of them, and that
    question is unanswerable from inside RLS. It returns ids and counts only —
    no `content`, no `embedding` — and every subsequent statement for each
    subject goes back through `store.db.session()`.
    """
    async with admin_session() as conn:
        cursor = await conn.execute(
            _SUBJECTS_SQL,
            {
                "reflection_source": REFLECTION_SOURCE,
                "minimum": min_cluster_size(),
                "limit": int(limit),
            },
        )
        return [(str(r["subject_id"]), str(r["actor_id"])) for r in await cursor.fetchall()]


# ---------------------------------------------------------------------------
# the whole job
# ---------------------------------------------------------------------------

async def run_reflection_worker(
    *,
    subject_id: str | None = None,
    actor_id: str | None = None,
    worker: str = "worker-0",
    run_id: str | None = None,
    max_subjects: int = 25,
) -> Any:
    """Run the reflection graph for one subject, or for every eligible subject.

    Lazily imports the graph for the same reason `jobs/decay.py` does: this
    module stays importable — and `select_cluster` stays testable — without
    pulling LangGraph in.
    """
    from graphs.reflection_graph import run_reflection
    from jobs.claims import new_run_id
    from jobs.metrics import RunStats, log_event

    run = str(run_id or new_run_id())
    record = RunStats(job="reflection", run_id=run, worker=worker).start()

    if subject_id is not None:
        targets = [(str(subject_id), str(actor_id or subject_id))]
    else:
        targets = (await list_subjects_with_candidates(limit=max_subjects))

    log_event(
        "reflection.worker.start",
        run_id=run,
        worker=worker,
        pid=record.pid,
        subjects=len(targets),
    )

    try:
        for subject, actor in targets:
            state = await run_reflection(subject_id=subject, actor_id=actor)
            if state.get("summary_id"):
                record.count_summary()
                record.sources_consolidated += len(state.get("consolidated") or [])
                record.add_processed([state["summary_id"]])
            log_event(
                "reflection.subject.complete",
                run_id=run,
                worker=worker,
                pid=record.pid,
                subject_id=subject,
                summary_id=state.get("summary_id"),
                cluster_size=len(state.get("cluster_ids") or []),
                skipped=state.get("skipped"),
            )
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        record.error = f"{type(exc).__name__}: {exc}"
        record.finish(outcome="error")
        record.log()
        raise

    record.finish(outcome="ok")
    record.log()
    return record
