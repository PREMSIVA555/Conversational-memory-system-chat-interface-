"""Retrieval evaluation harness (M3 plan steps 9-14).

`golden_set.jsonl`      labeled query -> expected-memory-id records
`fixtures/`             deterministic seeding of the rows those records name
`metrics.py`            precision / recall / F1
`run_eval.py`           the runner: `python evals/run_eval.py --suite golden_set_v1`
`results/`              the written baseline M8 compares against
"""
