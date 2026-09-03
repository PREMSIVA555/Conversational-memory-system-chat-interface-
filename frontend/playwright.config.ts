/**
 * Playwright configuration (M6 step 12).
 *
 * TWO PROJECTS, AND THE SPLIT IS DELIBERATE
 * -----------------------------------------
 * `mocked` — every network call is intercepted with `page.route`. No backend,
 *            no database, no provider quota, so these run anywhere in seconds
 *            and deterministically. They guard the UI's own logic: incremental
 *            rendering, optimistic update and rollback, the empty state, the
 *            degraded banner, the aria-live containment.
 *
 * `live`   — drives the real stack. This is the milestone's Definition of Done
 *            and it proves the WIRING, which a mock asserts into existence
 *            rather than testing.
 *
 * The split exists because the embedding provider is capped at **3 requests per
 * minute**. A suite that hit it for every assertion would take tens of minutes
 * and be flaky for reasons unrelated to the frontend. So behaviour is pinned by
 * `mocked`, and `live` proves the seam.
 *
 * BOTH ARE REGISTERED ALWAYS, on purpose. The Definition of Done runs
 *
 *     npm run test:e2e -- --grep "chat streams and memory panel lists memories"
 *
 * and that name belongs to a LIVE spec — so gating the live project behind an
 * env var would make the DoD's own command silently match nothing and report
 * success. `npm run test:e2e:mocked` is there for the hermetic subset.
 *
 * Consequence worth stating plainly: plain `npm run test:e2e` needs the stack
 * up (compose, migrations, and `python -m api.main` — never `uvicorn
 * api.main:app`, see the README on the Windows event-loop trap). That is what
 * the last DoD line asks for.
 */

import { defineConfig, devices } from "@playwright/test";

const PORT = Number(process.env.PLAYWRIGHT_PORT ?? 3100);
const BASE_URL = process.env.PLAYWRIGHT_BASE_URL ?? `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  // One worker: the live specs mutate shared backend state (a delete is not
  // undoable), and two browsers racing on the same subject's memories would
  // make failures depend on interleaving.
  workers: 1,
  reporter: [["list"]],

  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
  },

  projects: [
    {
      name: "mocked",
      testDir: "./e2e/mocked",
      use: { ...devices["Desktop Chrome"] },
      timeout: 30_000,
      expect: { timeout: 5_000 },
    },
    {
      name: "live",
      testDir: "./e2e/live",
      use: { ...devices["Desktop Chrome"], video: "retain-on-failure" },
      // Absurd-looking on purpose. The embedding provider is capped at 3
      // requests/minute; a turn embeds for retrieval and again for capture, so
      // once the window is spent a legitimate first token can be 30-60 seconds
      // away. A tight timeout here reports a healthy backend as broken.
      timeout: 180_000,
      expect: { timeout: 30_000 },
    },
  ],

  // Serve the BUILT app, not `next dev`: the dev server compiles on first
  // request, which appears as a multi-second stall in the middle of a streaming
  // assertion and looks exactly like a backend slow to deliver its first token.
  webServer: {
    command: `npx next start --port ${PORT}`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    stdout: "ignore",
    stderr: "pipe",
  },
});
