"""Actual persistence checks, intentionally outside the legacy live-provider suites."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import time
import unittest
import uuid

import psycopg
from psycopg import sql

from evals.world import CUSTOMERS, FIXTURE_VERSION, ROOT, UnsafeTarget, load_world

SNAPSHOTS = []


class WorldChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config_path = Path(os.environ.get("P00_WORLD_CONFIG", ROOT / ".genesis/local/p00-world.json"))
        cls.config = json.loads(cls.config_path.read_text())
        cls.trial = "check_" + uuid.uuid4().hex[:12]
        cls.world = load_world(cls.config_path, cls.trial)

    def test_two_resets_match_declared_fixture(self):
        a, b = self.world.reset(), self.world.reset()
        fixture = json.loads((ROOT / "evals/fixtures/p00_world.json").read_text())
        self.assertEqual(a, b)
        self.assertEqual(a["state"]["customers"], [tuple(row) for row in fixture["customers"]])
        self.assertEqual(a["state"]["domain_clock"], fixture["domain_clock"])
        self.assertEqual(a["state"]["fixture"], fixture["fixture_version"])
        SNAPSHOTS.append({"check": "two-resets", "first": a, "second": b})

    def test_parallel_trials_are_independent(self):
        worlds = [replace(self.world, trial=self.trial + suffix) for suffix in ("_a", "_b")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda w: w.reset(), worlds))
        self.assertEqual(results[0], results[1])
        before = worlds[1].snapshot()
        with worlds[0]._connect() as db, worlds[0]._redis() as cache:
            db.execute(sql.SQL("UPDATE {}.customers SET name='contaminated'").format(sql.Identifier(worlds[0].schema)))
            cache.set(worlds[0].prefix + "foreign", "canary")
        (worlds[0].files / "contamination.txt").write_text("canary")
        self.assertEqual(worlds[1].snapshot(), before)
        self.assertNotEqual(worlds[0].snapshot(), before)
        self.assertEqual(worlds[0].reset(), before)
        SNAPSHOTS.append({"check": "parallel-and-contamination", "initial": results, "unaffected": before})

    def test_reset_refuses_non_test_targets(self):
        invalid = [replace(self.world, trial="../../outside"),
                   replace(self.world, trial="a; DROP SCHEMA public"),
                   replace(self.world, dsn=self.world.dsn.replace("/bank_p00_test", "/memory_system")),
                   replace(self.world, dsn=self.world.dsn.replace("127.0.0.1", "example.com")),
                   replace(self.world, redis_url=self.world.redis_url.replace("/0", "/1")),
                   replace(self.world, dsn=self.world.dsn + "?options=-csearch_path=public")]
        for world in invalid:
            with self.subTest(trial=world.trial), self.assertRaises(UnsafeTarget):
                world.reset()

    def test_application_role_cannot_reset(self):
        self.world.reset()
        with self.assertRaises(UnsafeTarget):
            replace(self.world, dsn=self.config["app_dsn"]).reset()
        with psycopg.connect(self.config["app_dsn"]) as conn:
            flags = conn.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
            self.assertEqual(flags, (False, False, False, False))
            rows = conn.execute(sql.SQL("SELECT count(*) FROM {}.customers").format(sql.Identifier(self.world.schema))).fetchone()[0]
            self.assertEqual(rows, 2)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.world.schema)))

    def test_instance_identity_mismatch_leaves_state_untouched(self):
        before = self.world.reset()
        with self.assertRaises(UnsafeTarget):
            replace(self.world, system_identifier="wrong-cluster").reset()
        with self.assertRaises(UnsafeTarget):
            replace(self.world, instance="f" * 32).reset()
        with self.world._redis() as cache:
            cache.set("p00:test-instance", "wrong-instance")
        try:
            with self.assertRaises(UnsafeTarget):
                self.world.reset()
        finally:
            import redis
            client = redis.Redis.from_url(self.world.redis_url)
            client.set("p00:test-instance", self.world.instance)
            client.close()
        self.assertEqual(self.world.snapshot(), before)

    def test_domain_clock_does_not_replace_wall_or_monotonic_time(self):
        fixed = self.world.clock.now()
        monotonic, wall = time.monotonic(), time.time()
        time.sleep(0.02)
        self.assertEqual(self.world.clock.now(), fixed)
        self.assertGreater(time.monotonic(), monotonic)
        self.assertGreater(time.time(), wall)
        # P01 session expiry is not implemented or claimed by this check.


if __name__ == "__main__":
    unittest.main()
