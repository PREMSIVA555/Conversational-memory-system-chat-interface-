"""Deterministic seeding for the golden set (plan step 10).

The golden set names memory ids. Those ids therefore cannot be random: an eval
run from a clean database has to produce byte-identical row ids to the ones in
`golden_set.jsonl`, or every expected-id comparison silently scores zero. So:

* Every id is `uuid5(NAMESPACE_URL, "memory-system/evals/golden_set_v1/<slug>")`.
  Same slug, same uuid, forever, on any machine.
* The subject id is derived the same way, and is used by nothing else — the
  eval corpus never collides with a real user's rows or with M2's capture
  output.
* `seed()` deletes every row for that subject before inserting, so a rerun after
  a test has soft-deleted or mutated a row restores the exact starting state.

EMBEDDING CACHE
---------------
Seeding needs a vector per memory. Calling Voyage on every run would make the
eval non-reproducible (embedding endpoints are not contractually deterministic
across model revisions) and slow the test suite by a network round-trip per
run. Vectors are therefore cached to `embedding_cache.json`, keyed by
`{model}:{sha256(content)}` — so the cache invalidates itself if either the
model or the text changes, and a clean checkout simply re-embeds once.

This file writes rows through `store.db.session()`, which sets the RLS GUCs.
It does not import from `capture/` — the eval corpus is independent of M2 by
design, so retrieval quality can be measured before the capture graph exists.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm.config import embed, resolve_embedding_model
from store.db import session

_NS = uuid.NAMESPACE_URL
_PREFIX = "memory-system/evals/golden_set_v1"

CACHE_PATH = Path(__file__).resolve().parent / "embedding_cache.json"

GOLDEN_SET_SUBJECT_ID = str(uuid.uuid5(_NS, f"{_PREFIX}/subject"))
# Single-user assistant: the M1 schema seam says these are equal today.
GOLDEN_SET_ACTOR_ID = GOLDEN_SET_SUBJECT_ID


def memory_id(slug: str) -> str:
    """The stable uuid for a corpus slug. Also used to write golden_set.jsonl."""
    return str(uuid.uuid5(_NS, f"{_PREFIX}/memory/{slug}"))


@dataclass(frozen=True, slots=True)
class SeedMemory:
    slug: str
    content: str
    source: str = "eval_fixture"
    importance: float = 0.5
    confidence: float = 0.9

    @property
    def id(self) -> str:
        return memory_id(self.slug)


# ---------------------------------------------------------------------------
# the corpus
# ---------------------------------------------------------------------------
#
# Forty-four memories across eight loose themes. Both the size and the theme
# spread are load-bearing, and both were arrived at by measurement:
#
#   * With SEMANTIC_TOP_K = 5 over 44 rows the semantic path returns roughly the
#     top ninth of the table. On a small corpus it would return most of the
#     table and "this query is only matchable by keyword" would stop being
#     testable — the semantic path would surface the target by sheer coverage.
#
#   * The nine-row FAMILY cluster gives the semantic-leaning `Polish family
#     history` query several genuinely correct answers, so its expected set is a
#     real relevance judgement rather than a single lucky row.
#
#   * The keyword-only probe's target is the `tiles` row; its comment carries
#     the full design history, including five designs that were measured and
#     rejected. Read that before changing any row's text — corpus edits move
#     cosine rankings, and that probe depends on a measured margin.
#
MEMORIES: tuple[SeedMemory, ...] = (
    # -- diet / body -------------------------------------------------------
    SeedMemory("lactose", "I am lactose intolerant, so I skip dairy entirely.", importance=0.8),
    SeedMemory("onions", "Raw onions give me heartburn for hours.", importance=0.6),
    SeedMemory(
        "catallergy",
        "Antihistamines are necessary whenever I visit friends who keep cats.",
        importance=0.6,
    ),
    # -- family / heritage --------------------------------------------------
    SeedMemory(
        "tortoise",
        "Priya named her tortoise Zbigniewicz after her great-uncle.",
        importance=0.3,
    ),
    SeedMemory("halina", "My grandmother Halina emigrated from Krakow in 1962.", importance=0.7),
    SeedMemory(
        "surname",
        "Our family surname was shortened from Wojciechowski when we arrived.",
        importance=0.7,
    ),
    SeedMemory(
        "godmother",
        "My godmother is called Agnieszka and she lives in Gdansk.",
        importance=0.5,
    ),
    SeedMemory("duolingo", "I practise Polish vocabulary on Duolingo most evenings.", importance=0.4),
    SeedMemory(
        "sister",
        "My sister Mira is finishing her doctorate in marine biology.",
        importance=0.6,
    ),
    SeedMemory("uncle", "My uncle Tadeusz Nowak still farms outside Poznan.", importance=0.5),
    SeedMemory("cousin", "My cousin Kasia Lewandowska got married last spring.", importance=0.5),
    SeedMemory("neighbour", "The neighbours at number 14 are the Szymanskis.", importance=0.4),
    # -- running ------------------------------------------------------------
    SeedMemory("bristol", "I run the Bristol half marathon every October.", importance=0.7),
    SeedMemory("shoes", "My running shoes are Saucony Endorphin, size 11.", importance=0.4),
    SeedMemory(
        "physio",
        "My physiotherapist told me to stretch my calves before long runs.",
        importance=0.6,
    ),
    SeedMemory("commute", "I cycle to the office on Tuesdays and Thursdays.", importance=0.4),
    # -- work ---------------------------------------------------------------
    SeedMemory(
        "deadline",
        "The Q3 compliance report is due on the fifteenth of September.",
        importance=0.8,
    ),
    SeedMemory(
        "manager",
        "My manager Dara prefers written updates over stand-up meetings.",
        importance=0.7,
    ),
    # -- music --------------------------------------------------------------
    SeedMemory("guitar", "I have been learning fingerstyle guitar for about two years.", importance=0.5),
    SeedMemory(
        "strings",
        "I restring the guitar with phosphor bronze strings every couple of months.",
        importance=0.4,
    ),
    SeedMemory("amp", "I want a small valve amplifier for practising at home.", importance=0.4),
    SeedMemory(
        "lessons",
        "My guitar teacher moved the Thursday lesson to Wednesday evenings.",
        importance=0.5,
    ),
    SeedMemory("chords", "I still cannot play a clean barre chord on the fifth fret.", importance=0.4),
    SeedMemory(
        "openmic",
        "There is an open mic night at the pub on the last Friday of the month.",
        importance=0.4,
    ),
    # -- body / medical + church instrument ---------------------------------
    #
    # Corpus bulk and topical spread. `SEMANTIC_TOP_K = 5` is only a meaningful
    # cut if the corpus is large and varied enough that returning five rows is
    # genuinely selective; these twelve widen the subject range beyond the
    # original six themes.
    #
    # They were originally added to try to rescue a keyword-only probe built on
    # the "organ"/"organic" stemming collision, on the theory that rows about
    # transplants, anatomy and a church instrument would sit closer to the bare
    # token "organ" and push the "organic vegetables" carrier down the ranking.
    # MEASUREMENT SHOWED THAT THEORY IS WRONG and it is worth recording why: a
    # single out-of-context word does not embed near sentences about its
    # subject. For the query "organ" the whole 43-row corpus landed in a narrow
    # 0.58-0.72 cosine-distance band, `commute` ("I cycle to the office on
    # Tuesdays and Thursdays") ranked FIRST, and `transplant`, `heartvalve` and
    # `anatomy` did not even make the top 22. A bare-token query produces an
    # essentially arbitrary ordering, which is exactly why it cannot be used as
    # the basis of a probe that has to hold to a margin. See the `tiles` row
    # below for what replaced it.
    SeedMemory(
        "transplant",
        "My father has been on the kidney transplant list since March.",
        importance=0.8,
    ),
    SeedMemory(
        "donorcard",
        "I registered as a donor when I renewed my driving licence.",
        importance=0.6,
    ),
    SeedMemory(
        "heartvalve",
        "My aunt had a heart valve replaced at the Royal Infirmary.",
        importance=0.7,
    ),
    SeedMemory(
        "liverscan",
        "The liver specialist wants another scan before Christmas.",
        importance=0.7,
    ),
    SeedMemory(
        "anatomy",
        "She is studying human anatomy in her second year of medicine.",
        importance=0.5,
    ),
    SeedMemory(
        "coordinator",
        "The transplant coordinator rang about a possible match.",
        importance=0.8,
    ),
    SeedMemory(
        "tissue",
        "The surgeon explained how the donated tissue would be preserved.",
        importance=0.6,
    ),
    SeedMemory(
        "cornea",
        "Her cornea graft restored most of the sight in one eye.",
        importance=0.6,
    ),
    SeedMemory(
        "lungs",
        "My lungs took months to recover after the pneumonia.",
        importance=0.6,
    ),
    SeedMemory(
        "blooddonation",
        "I gave blood for the twelfth time last month.",
        importance=0.5,
    ),
    SeedMemory(
        "pipeinstrument",
        "The church has a Victorian pipe instrument that still needs restoring.",
        importance=0.4,
    ),
    SeedMemory(
        "pedalkeyboard",
        "He plays the pedal keyboard at St Mary's on Sunday mornings.",
        importance=0.4,
    ),
    # -- home / misc --------------------------------------------------------
    SeedMemory(
        "wifi",
        "The wifi network in the flat is still called Beethoven because the previous "
        "tenant set the router up years ago and the landlord has never once changed "
        "the password or the network name since then.",
        importance=0.4,
    ),
    # Ordinary corpus filler. This row was the target of a RETIRED keyword-only
    # probe built on the "organ"/"organic" stemming collision; that probe failed
    # its separation requirement (semantic rank 6 of 31, 0.00128 outside the
    # top-5 cut) and was replaced by the `tiles` row above. The word "organic"
    # is left in place because it is natural here and nothing depends on it now.
    SeedMemory("veg", "I buy organic vegetables from the market stall on Saturday mornings.", importance=0.4),
    SeedMemory("charger", "The universal charger for the old laptop lives in the hall cupboard.", importance=0.3),
    SeedMemory("coffee", "I drink my coffee black, two cups before noon.", importance=0.3),
    SeedMemory("flat", "The flat I rent in Totterdown has a leaking skylight.", importance=0.6),
    #
    # `tiles` IS THE KEYWORD-ONLY PROBE'S TARGET (golden set gs-002, query
    # "origin"). It is an ordinary memory about a rented flat; the whole trick
    # lives in the query, and it took six measured designs to get here.
    #
    # THE REQUIREMENT is not just that the keyword path finds the target — it is
    # that the semantic path genuinely MISSES it, by a margin large enough to
    # survive re-embedding. Voyage is not bit-deterministic (~1e-3 elementwise
    # drift between two calls on the same text), so a target sitting a
    # thousandth of a cosine outside the top-k cut is a coin flip, not a result.
    #
    # WHAT FAILED, all measured against the whole corpus with no LIMIT:
    #   1. "Zbigniewicz" in a short row  -> semantic rank 1. In a nine-word
    #      sentence the rare token IS the embedding.
    #   2. "Kowalczyk" in a 35-word row  -> rank 2.
    #   3. Same, plus nine competitor rows from the same sense -> rank 3.
    #   4. "Beethoven" in a 35-word row with a six-row music cluster -> rank 1.
    #      Voyage rewards the row containing a literal query token so strongly
    #      that same-sense competition never dislodges it.
    #   5. "organ" via the organ/organic stemming collision -> rank 6 of 31,
    #      just 0.00128 outside SEMANTIC_TOP_K=5. The collision was sound but
    #      the separation was not: a live re-embed moved it to rank 5 and the
    #      test failed, and it was inside top-10 either way.
    #
    # WHAT WORKS is a stemming collision whose two surface forms are far apart
    # in the embedding space as well as in meaning. Snowball stems both "origin"
    # and "original" to the lexeme 'origin':
    #
    #     select to_tsvector('english','origin');    -- 'origin':1
    #     select to_tsvector('english','original');  -- 'origin':1
    #
    # So `content_tsv @@ websearch_to_tsquery('english','origin')` matches this
    # row and, verified corpus-wide, nothing else. But the embedder never sees
    # the word "origin" here — it sees cracked hallway tiles in a rented flat —
    # and it reads the query "origin" as provenance or beginnings, which is
    # nowhere near a home-repair complaint. Unlike "organic"/"organ", the two
    # forms do not look alike enough for the literal-token bonus to fire.
    #
    # Measured on the 44-row corpus: keyword returns exactly ['tiles'];
    # semantic ranks it 31st, +0.13039 cosine below the top-5 boundary and
    # +0.10306 below top-10 — about a hundred times the embedding noise.
    #
    # THOSE NUMBERS ARE ENFORCED, not just recorded, which matters because a
    # comment claiming a margin is worth nothing once someone edits the corpus:
    #
    #   evals/separation.py           ranks the WHOLE corpus with no LIMIT and
    #                                 computes rank + margin.
    #   run_eval.py                   prints "separation: semantic rank 31/44,
    #                                 margin@5 +0.13039" on every single run, so
    #                                 erosion is visible rather than silent.
    #   test_keyword_only_probe_has_a_robust_semantic_margin
    #                                 asserts rank >= MIN_SEPARATION_RANK and
    #                                 margin >= MIN_SEPARATION_MARGIN, at both
    #                                 the configured k and k=10.
    #
    # If you edit any row's text and that test fails, the fix is to re-derive the
    # design — not to lower the floors.
    SeedMemory(
        "tiles",
        "The original tiles in the hallway are cracked beyond repair.",
        importance=0.4,
    ),
    SeedMemory("holiday", "We booked a fortnight in the Dolomites for next summer.", importance=0.5),
    SeedMemory(
        "dentist",
        "My dentist appointment is always the first Monday of the quarter.",
        importance=0.4,
    ),
)

BY_SLUG: dict[str, SeedMemory] = {m.slug: m for m in MEMORIES}
BY_ID: dict[str, SeedMemory] = {m.id: m for m in MEMORIES}


# ---------------------------------------------------------------------------
# embedding cache
# ---------------------------------------------------------------------------

def _cache_key(model: str, content: str) -> str:
    return f"{model}:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def _load_cache() -> dict[str, list[float]]:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict[str, list[float]]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")


async def embeddings_for(texts: list[str], *, use_cache: bool = True) -> list[list[float]]:
    """Vectors for `texts`, embedding only the ones not already cached."""
    model = resolve_embedding_model()
    cache = _load_cache() if use_cache else {}

    missing = [t for t in texts if _cache_key(model, t) not in cache]
    if missing:
        # One batched call for everything missing, never N calls.
        fresh = await embed(missing)
        for text, vector in zip(missing, fresh):
            cache[_cache_key(model, text)] = vector
        if use_cache:
            _save_cache(cache)

    return [cache[_cache_key(model, t)] for t in texts]


def prune_cache(texts: list[str]) -> int:
    """Drop cached vectors that no live corpus row asks for. Returns the count.

    Without this the cache only ever grows: editing a row's text changes its
    sha256 key, so the old vector is orphaned rather than replaced, and the file
    slowly fills with embeddings of superseded fixture text. That is confusing
    (32 entries for a 31-row corpus invites the question of which row is
    missing) and it makes the cache a poor record of what the corpus actually
    is. `seed()` calls this on every run, so the file always mirrors MEMORIES
    exactly.
    """
    model = resolve_embedding_model()
    cache = _load_cache()
    if not cache:
        return 0
    live = {_cache_key(model, t) for t in texts}
    orphans = [k for k in cache if k not in live]
    if orphans:
        for key in orphans:
            del cache[key]
        CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    return len(orphans)


def to_vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------

_INSERT = """
INSERT INTO memories
    (id, subject_id, actor_id, content, embedding, source, importance, confidence)
VALUES
    (%(id)s::uuid, %(subject_id)s::uuid, %(actor_id)s::uuid, %(content)s,
     %(embedding)s::vector, %(source)s, %(importance)s, %(confidence)s)
"""


async def seed(
    *,
    subject_id: str = GOLDEN_SET_SUBJECT_ID,
    actor_id: str | None = None,
    memories: tuple[SeedMemory, ...] = MEMORIES,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Reset and reseed the eval corpus. Returns a small summary dict.

    Runs inside one `store.db.session()` transaction with the RLS GUCs set, so
    the DELETE and the INSERTs are atomic and both pass the row-level policies.
    """
    actor_id = actor_id or subject_id
    contents = [m.content for m in memories]
    vectors = await embeddings_for(contents, use_cache=use_cache)
    pruned = prune_cache(contents) if use_cache else 0

    async with session(subject_id, actor_id) as conn:
        cur = await conn.execute(
            "DELETE FROM memories WHERE subject_id = %s::uuid", (subject_id,)
        )
        deleted = cur.rowcount
        for mem, vector in zip(memories, vectors):
            await conn.execute(
                _INSERT,
                {
                    "id": mem.id,
                    "subject_id": subject_id,
                    "actor_id": actor_id,
                    "content": mem.content,
                    "embedding": to_vector_literal(vector),
                    "source": mem.source,
                    "importance": mem.importance,
                    "confidence": mem.confidence,
                },
            )

    return {
        "subject_id": subject_id,
        "actor_id": actor_id,
        "deleted": deleted,
        "inserted": len(memories),
        "cache_pruned": pruned,
        "embedding_model": resolve_embedding_model(),
    }


async def _main() -> None:
    summary = await seed()
    print(json.dumps(summary, indent=2))
    for mem in MEMORIES:
        print(f"  {mem.id}  {mem.slug:<12} {mem.content}")


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from store.db import ensure_selector_event_loop_policy

    ensure_selector_event_loop_policy()
    asyncio.run(_main())
