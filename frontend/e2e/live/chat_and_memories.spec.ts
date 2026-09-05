/**
 * The live end-to-end journey (M6 test case 1 — the DoD's named grep target).
 *
 * REQUIRES THE REAL STACK. Run with `npm run test:e2e:live`.
 *
 * Preconditions this spec does NOT create for you:
 *   * docker compose up, migrations applied
 *   * the API running: `python -m api.main` (NOT `uvicorn api.main:app` — see
 *     the README on the Windows event-loop trap)
 *   * `API_BASE_URL` set in `frontend/.env.local`
 *
 * WHAT IT DOES CREATE, and why that was missing
 * ---------------------------------------------
 * Plan step 12 asks for "an e2e setup that boots the backend fixtures, seeds at
 * least one memory". That half was never built, and its absence was not
 * harmless: without it these specs assumed a seeded corpus and SKIPPED when they
 * found none. A spec that skips because its precondition is missing reports
 * success while testing nothing — the exact failure mode three M8 verifications
 * kept surfacing elsewhere in this project. `beforeAll` now seeds the fixture
 * corpus itself, and the specs fail rather than skip if it is absent.
 *
 * WHOSE MEMORIES THE BROWSER SEES
 * -------------------------------
 * The backend mints a fresh `subject_id` on the first chat turn, and a fresh
 * subject owns nothing — so a test that chatted and then opened the panel would
 * correctly find it empty, and "the panel lists at least one row" would fail for
 * a system behaving perfectly. Capture is asynchronous and runs only after the
 * stream completes, so waiting it out is both slow and racy.
 *
 * Instead the browser is given the eval fixture's subject up front, through the
 * same `localStorage` key the app itself uses. That is not a shortcut around the
 * seam: the id still travels browser -> Next route handler -> FastAPI -> RLS, and
 * every assertion below still depends on that whole path working.
 *
 * WHY THE TIMEOUTS LOOK ABSURD
 * ----------------------------
 * The embedding provider is capped at 3 requests/minute. A turn embeds for
 * retrieval and again for capture, so once the window is spent the first token
 * can legitimately be 30-60 seconds away. A tight timeout here does not catch a
 * bug; it reports a healthy backend as broken.
 */

import { execFileSync } from "node:child_process";
import path from "node:path";

import { expect, test, type Page } from "@playwright/test";

/** The eval fixture's subject — a uuid5, so it is stable across runs. */
const FIXTURE_SUBJECT = "93e6200f-ff8f-5502-98fb-a4643a815412";
const REPO_ROOT = path.resolve(__dirname, "..", "..", "..");

test.beforeAll(() => {
  // Seed the fixture corpus (plan step 12). Uses the on-disk embedding cache,
  // so it costs no provider requests. Throws — and therefore fails the run —
  // if the backend or database is unreachable, which is the point: a missing
  // precondition must be a failure, not a skip.
  const python = path.join(REPO_ROOT, ".venv", "Scripts", "python.exe");
  execFileSync(python, ["-m", "evals.fixtures.seed_memories"], {
    cwd: REPO_ROOT,
    env: { ...process.env, PYTHONPATH: REPO_ROOT, PYTHONIOENCODING: "utf-8" },
    timeout: 300_000,
    stdio: "pipe",
  });
});

/** Give the browser the fixture's identity before any page script runs. */
async function asFixtureSubject(page: Page): Promise<void> {
  await page.addInitScript((subject: string) => {
    window.localStorage.setItem("memory-system.subject-id", subject);
  }, FIXTURE_SUBJECT);
}

test("chat streams and memory panel lists memories", async ({ page }) => {
  await asFixtureSubject(page);
  await page.goto("/chat");

  await page.getByTestId("chat-input").fill("What do you know about my hobbies?");
  await page.getByTestId("chat-send").click();

  const assistant = page.getByTestId("assistant-text");
  await expect(assistant).toBeVisible();

  // Sample while the reply is in flight. The DoD asks for evidence the text
  // GREW rather than appearing at once.
  const samples: number[] = [];
  const deadline = Date.now() + 150_000;
  let previous = -1;
  let settled = 0;
  while (Date.now() < deadline) {
    const length = ((await assistant.textContent()) ?? "").length;
    samples.push(length);
    if (length > 0 && length === previous) {
      settled += 1;
      if (settled >= 4) break;
    } else {
      settled = 0;
    }
    previous = length;
    await page.waitForTimeout(200);
  }

  const distinct = [...new Set(samples)].sort((a, b) => a - b);
  const finalLength = ((await assistant.textContent()) ?? "").length;
  expect(finalLength, `the assistant produced no text at all; samples=${JSON.stringify(distinct)}`)
    .toBeGreaterThan(0);

  // An intermediate length proves incremental rendering. A buffered
  // implementation only ever shows 0 and then the final length.
  const intermediate = distinct.filter((v) => v > 0 && v < distinct[distinct.length - 1]);
  expect(
    intermediate.length,
    `no partial render observed against the LIVE backend; sampled lengths ${JSON.stringify(distinct)}. ` +
      "Either the reply arrived in one chunk (short answers can) or the UI is buffering.",
  ).toBeGreaterThanOrEqual(1);

  // The turn reports how it was built. BOTH outcomes are legitimate: on a spent
  // embedding quota the retrieval path times out and M5's breaker serves the
  // turn without memory, so asserting "memory was used" would fail this spec
  // for a rate limit rather than a defect.
  const degraded = page.getByTestId("degraded-indicator");
  const used = page.getByTestId("memory-count");
  await expect(degraded.or(used).first()).toBeVisible();

  // ---- the memory panel, over the same identity -------------------------
  await page.getByRole("link", { name: /Manage memories/ }).click();
  await expect(page).toHaveURL(/\/memories$/);
  await expect(page.getByTestId("memories-loading")).toHaveCount(0, { timeout: 60_000 });

  // A seeded corpus must NOT produce the no-identity or empty states. Asserting
  // their absence is what stops this spec passing on an empty panel.
  await expect(page.getByTestId("memories-no-identity")).toHaveCount(0);
  await expect(page.getByTestId("memories-empty")).toHaveCount(0);

  const rows = page.getByTestId("memory-row");
  await expect(rows.first()).toBeVisible({ timeout: 60_000 });
  expect(await rows.count()).toBeGreaterThan(0);
});

test("delete removes a row from the live panel without a page reload", async ({ page }) => {
  await asFixtureSubject(page);
  await page.goto("/memories");
  await expect(page.getByTestId("memories-loading")).toHaveCount(0, { timeout: 60_000 });

  const rows = page.getByTestId("memory-row");
  const before = await rows.count();
  expect(before, "the fixture corpus seeded no rows — beforeAll did not do its job").toBeGreaterThan(0);

  const firstText = await page.getByTestId("memory-content").first().textContent();

  let navigations = 0;
  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) navigations += 1;
  });

  await page.getByTestId("memory-delete").first().click();

  await expect(rows).toHaveCount(before - 1, { timeout: 60_000 });
  expect(navigations, "the page navigated — the row vanished via a reload, not an in-place update").toBe(0);

  // And it is really gone server-side, not just hidden: a reload re-fetches
  // from the backend, and the soft-deleted row must not come back.
  await page.reload();
  await expect(page.getByTestId("memories-loading")).toHaveCount(0, { timeout: 60_000 });
  await expect(rows).toHaveCount(before - 1);
  if (firstText) {
    await expect(page.getByTestId("memory-content").first()).not.toHaveText(firstText);
  }
});
