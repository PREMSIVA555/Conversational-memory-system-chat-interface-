# `retrieve/` — the read path

Two retrieval paths, run concurrently, merged by `memory_id`.

| module | path | index it depends on |
| --- | --- | --- |
| `semantic.py` | pgvector cosine (`embedding <=> query`) | `memories_embedding_hnsw_idx` (HNSW, `vector_cosine_ops`) |
| `keyword.py` | tsvector (`content_tsv @@ websearch_to_tsquery(...)`) | `memories_content_tsv_gin_idx` (GIN) |
| `hybrid.py` | fan-out, per-path timeout + isolation, normalize, merge | — |
| `types.py` | `RetrievalQuery`, `RetrievalCandidate`, `HybridResult` | — |
| `config.py` | `SEMANTIC_TOP_K`, `KEYWORD_TOP_K`, `PATH_TIMEOUT_MS` | — |

Entry point: `retrieve.hybrid.hybrid_search(RetrievalQuery) -> HybridResult`.
`retrieve.hybrid.retrieve(...)` is the same thing returning just the candidate
list. `api/retrieval_service.py` is the API-facing wrapper (M3 plan step 15);
M5 is what puts it on the chat critical path.

## Graph search is deferred to a v2 shelf

**No graph-search module exists here, and none should be added in M3.**

The original architecture sketched a third retrieval path over a memory graph
(entity/relation edges between memories, traversed to pull in facts that are
neither lexically nor semantically similar to the query but are *connected* to
something that is). It is deliberately shelved:

- It needs an entity-extraction and edge-writing stage that M2's capture graph
  does not have, so building the read side first would leave it reading an empty
  graph and impossible to evaluate.
- The eval harness landing in this same milestone (`evals/golden_set.jsonl`) is
  the instrument that would tell us whether a third path earns its latency. That
  instrument does not exist until M3 ships. Adding the path in the same
  milestone as its own measuring device means shipping it unmeasured.
- Two paths already cover the two failure modes the golden set encodes: a rare
  literal token the embedder has no useful representation for (keyword-only),
  and a paraphrase with zero lexical overlap (semantic-only).

M3's Definition of Done asserts this: `retrieve/` contains only the semantic,
keyword, and hybrid modules (plus the `types`/`config` support the plan's steps
1 and 7 call for). Revisit graph search only after M8's expanded evals show a
recall ceiling the two existing paths cannot lift.

## Score normalization

Cosine similarity and `ts_rank` are on incomparable scales, so raw scores are
never compared across paths. Each path maps its raw score through a **fixed
reference scale** before the merge — cosine similarity clamped to `[0, 1]` for
the semantic path, and a saturating `rank / (rank + TS_RANK_REFERENCE)` for the
keyword path — and the merged score is the zero-filled mean over the paths that
returned results.

The scales are deliberately *absolute*, not min-max within each result set.
Min-max is corpus-relative: it maps whichever result happens to be best to
`1.0` however bad that result actually is, which discards the information that a
path found nothing good. That caused a measured tie — a lone `ts_rank` 0.06
keyword hit scoring identically to the best semantic match, with the winner
decided by UUID sort order. Full reasoning inline in `hybrid.py:_normalize`.

## Degradation

Each path runs under its own `asyncio.wait_for(PATH_TIMEOUT_MS)` and its own
exception handler. A path that fails contributes an empty list plus an entry in
`HybridResult.degraded` and a WARNING log line; the other path's results are
returned regardless. `hybrid_search` never raises for a path failure.

## RLS

Every query runs inside `store.db.session(subject_id, actor_id)`, which sets the
`app.subject_id` / `app.actor_id` GUCs. The application role is `NOSUPERUSER` /
`NOBYPASSRLS` and the tables are `FORCE ROW LEVEL SECURITY`, so a session
without those GUCs reads **zero rows** rather than erroring. If retrieval
mysteriously returns nothing, check the GUCs first.
