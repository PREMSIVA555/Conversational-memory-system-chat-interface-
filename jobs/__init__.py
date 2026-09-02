"""Background maintenance jobs (M8).

    jobs.claims      the cooperative `FOR UPDATE SKIP LOCKED` claim query
    jobs.decay       the pure decay function, the archive rule, the worker loop
    jobs.reflection  cluster selection, summarization, consolidation
    jobs.scheduler   APScheduler wiring for the two cron jobs
    jobs.metrics     run-level counters and the RunStats record
    jobs.run         `python -m jobs.run --job decay|reflection`

Nothing in here sits on the request path. Every module is written on the
assumption that it may be running as one of several processes against the same
database at the same time.
"""
