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

/** Reseed the fixture corpus. Also used as cleanup — see `afterAll`. */
function seedFixtureCorpus(): void {
  const python = path.join(REPO_ROOT, ".venv", "Scripts", "python.exe");
  execFileSync(python, ["-m", "evals.fixtures.seed_memories"], {
    cwd: REPO_ROOT,
    env: { ...process.env, PYTHONPATH: REPO_ROOT, PYTHONIOENCODING: "utf-8" },
    timeout: 300_000,
    stdio: "pipe",
  });
}

test.beforeAll(() => {
  // Seed the fixture corpus (plan step 12). Uses the on-disk embedding cache,
  // so it costs no provider requests. Throws — and therefore fails the run —
  // if the backend or database is unreachable, which is the point: a missing
  // precondition must be a failure, not a skip.
  seedFixtureCorpus();
});

/**
 * Wait until asynchronous capture has stopped writing, then reseed.
 *
 * Reseeding alone is not enough, and a cold verifier proved it: capture runs
 * ~15s AFTER a reply completes, so it can commit `assistant_note` rows into the
 * golden-set subject *after* cleanup has already run. The verifier measured
 * exactly that — `assistant_note | 5` written after the final reseed — so the
 * clean result the previous run reported was luck, not structure.
 *
 * `scripts/e2e_settle.py` polls until the row count holds steady (or gives up
 * after 45s, exiting 0 either way, because a slow capture is not a reason to
 * fail a frontend test run). Only then is the corpus reseeded.
 */
function settleThenReseed(): void {
  const python = path.join(REPO_ROOT, ".venv", "Scripts", "python.exe");
  execFileSync(python, ["scripts/e2e_settle.py"], {
    cwd: REPO_ROOT,
    env: { ...process.env, PYTHONPATH: REPO_ROOT, PYTHONIOENCODING: "utf-8" },
    timeout: 120_000,
    stdio: "pipe",
  });
  seedFixtureCorpus();
}

test.afterAll(() => {
  // THIS SUITE MUTATES THE M8 EVALUATION CORPUS, and that is not cosmetic.
  //
  // It shares a subject with the golden set, so a chat turn's capture writes
  // `assistant_note` rows into it and the delete spec soft-deletes an
  // `eval_fixture` row. A cold verifier measured the residue after one run:
  // `assistant_note | 3` and `eval_fixture | 43 live, 1 deleted`. An eval run
  // following an e2e run would then score against a polluted corpus — silently,
  // because nothing fails, the numbers merely stop meaning what they claim.
  //
  // Re-seeding restores exactly 44 `eval_fixture` rows and drops everything
  // else under the subject, because `seed()` DELETEs by subject before
  // inserting. Cheap: the vectors come from the on-disk cache, so no provider
  // request is made.
  //
  // But it must WAIT FIRST — see `settleThenReseed`. Capture commits well after
  // the reply that triggered it, so a bare reseed races writes that have not
  // happened yet.
  settleThenReseed();
});

test.beforeEach(() => {
  // Reseed BETWEEN tests, not just once for the file.
  //
  // The chat test's capture is asynchronous — the backend answers first and
  // writes the memory afterwards, up to ~15s later — and it writes into the
  // SAME subject the panel tests read. So rows appeared in the middle of the
  // delete test, and `toHaveCount(before - 1)` failed against a system working
  // correctly. Measured: the delete test passes alone and failed immediately
  // after the chat test.
  //
  // Reseeding per test removes the ordering dependence. It does NOT remove the
  // race on its own — a capture in flight can still land afterwards — which is
  // why the assertions below identify the specific row rather than counting.
  seedFixtureCorpus();
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

  // OBSERVE EVERY RENDER, do not sample.
  //
  // The first version polled `textContent` every 200ms, and a cold verifier
  // showed that is flaky by construction: against a short fast reply it
  // recorded `[0, 239]` and failed, even though the UI had rendered
  // progressively. The reply simply finished between two polls. That is a
  // FALSE FAILURE about the DoD's headline claim, which is the worst kind of
  // unreliable evidence to leave in place.
  //
  // A MutationObserver installed BEFORE the first chunk records the length at
  // every DOM update, so no intermediate state can be missed. If the sequence
  // still shows only 0 -> final, the UI genuinely painted once.
  await page.evaluate(() => {
    const w = window as unknown as { __lengths?: number[] };
    w.__lengths = [];
    const observer = new MutationObserver(() => {
      const node = document.querySelector('[data-testid="assistant-text"]');
      if (node) w.__lengths!.push((node.textContent ?? "").length);
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  });

  // Wait for the stream to finish: the component clears data-streaming when the
  // body ends. Falls back to text simply being present, in case the attribute
  // is ever renamed.
  await expect
    .poll(
      async () =>
        ((await assistant.textContent()) ?? "").length > 0 &&
        (await page.getByTestId("assistant-turn").getAttribute("data-streaming")) === "false",
      { timeout: 150_000, message: "the assistant reply never completed" },
    )
    .toBe(true);

  const lengths: number[] = await page.evaluate(
    () => (window as unknown as { __lengths: number[] }).__lengths ?? [],
  );
  const distinct = [...new Set(lengths)].sort((a, b) => a - b);
  const finalLength = ((await assistant.textContent()) ?? "").length;
  expect(finalLength, `the assistant produced no text at all; observed ${JSON.stringify(distinct)}`)
    .toBeGreaterThan(0);

  // An intermediate length proves incremental rendering. A buffered
  // implementation only ever goes 0 -> final, with nothing in between.
  const intermediate = distinct.filter((v) => v > 0 && v < finalLength);
  expect(
    intermediate.length,
    `no partial render observed against the LIVE backend. Every DOM update was recorded, ` +
      `so this is not a sampling artefact: observed lengths ${JSON.stringify(distinct)}, ` +
      `final ${finalLength}. Either the backend delivered the whole body in one chunk or ` +
      "the UI is buffering — check the proxy is not awaiting response.text().",
  ).toBeGreaterThanOrEqual(1);

  // ---- what the turn reports about memory, asserted so a dropped header
  //      cannot satisfy it -------------------------------------------------
  //
  // The previous assertion here was `expect(degraded.or(used)).toBeVisible()`,
  // and a cold verifier showed it was VACUOUS: if every X-Memory-* header were
  // dropped by the proxy — the exact defect found and fixed during this
  // milestone — `readStreamMetadata` yields `degraded=false, memoryCount=0`,
  // the `memory-count` element still renders ("No stored memories matched this
  // question"), and the assertion passes. Nothing in either suite would have
  // caught that regression returning; the mocked degraded test fakes the
  // headers inside the browser and never exercises the proxy at all.
  //
  // So assert a disjunction that only REAL headers can satisfy:
  //   * a non-zero memory count  (needs X-Memory-Count / X-Memory-Ids), or
  //   * the degraded banner WITH its reason  (needs X-Memory-Degraded and
  //     X-Memory-Degraded-Reason)
  //
  // Both branches are legitimate outcomes here — on a spent embedding quota the
  // retrieval path times out and M5's breaker serves the turn without memory —
  // but with the headers stripped, neither holds.
  const degraded = page.getByTestId("degraded-indicator");
  const used = page.getByTestId("memory-count");
  await expect(degraded.or(used).first()).toBeVisible();

  if ((await degraded.count()) > 0) {
    // The component parenthesises the reason ("...skipped (timeout)"). Its
    // absence is what a missing X-Memory-Degraded-Reason header looks like.
    await expect(degraded).toContainText(/\(.+\)/);
  } else {
    const text = (await used.textContent()) ?? "";
    const count = Number.parseInt(text.match(/(\d+)/)?.[1] ?? "0", 10);
    expect(
      count,
      `the turn was not degraded yet reported ${count} memories used. Against a seeded ` +
        "corpus that means the X-Memory-* headers did not survive the proxy — the exact " +
        `defect this milestone fixed. Element read: ${JSON.stringify(text)}`,
    ).toBeGreaterThan(0);
  }

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
  expect(before, "the fixture corpus seeded no rows — beforeEach did not do its job").toBeGreaterThan(0);

  // Identify the row by its TEXT, not by position or by the total count.
  //
  // `toHaveCount(before - 1)` is the obvious assertion and it is wrong here: a
  // chat turn's capture can commit into this same subject while the test runs,
  // pushing the count back up and failing a delete that in fact succeeded. The
  // claim being tested is "this row disappeared", so assert exactly that.
  const doomed = (await page.getByTestId("memory-content").first().textContent()) ?? "";
  expect(doomed.trim().length).toBeGreaterThan(0);

  let navigations = 0;
  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) navigations += 1;
  });

  const deleted = page.waitForResponse(
    (r) => r.url().includes("/api/memories/") && r.request().method() === "DELETE",
    { timeout: 60_000 },
  );
  await page.getByTestId("memory-delete").first().click();

  await expect(page.getByTestId("memory-content").filter({ hasText: doomed })).toHaveCount(0, {
    timeout: 60_000,
  });
  expect(navigations, "the page navigated — the row vanished via a reload, not an in-place update").toBe(0);

  const response = await deleted;
  expect(
    response.status(),
    `DELETE /memories/{id} answered ${response.status()}`,
  ).toBe(200);

  // And it is really gone server-side, not just hidden: a reload refetches from
  // FastAPI, and a soft-deleted row must not come back.
  await page.reload();
  await expect(page.getByTestId("memories-loading")).toHaveCount(0, { timeout: 60_000 });
  await expect(
    page.getByTestId("memory-content").filter({ hasText: doomed }),
    "the deleted row came back after a reload — it was hidden client-side but not soft-deleted",
  ).toHaveCount(0);
});

test("test_inline_edit_persists", async ({ page }) => {
  /*
    THE PLAN TICKED THIS TEST BEFORE IT EXISTED. A cold verifier grepped for it,
    found nothing, and made it the blocker that failed M6 — correctly, because
    of everything the panel does this is the call with the most behind it:
    `PATCH /memories/{id}` re-embeds the new content against a provider capped
    at 3 requests/minute, then writes an audit row, all under RLS. It had no
    live coverage at all. The mocked edit test cannot supply any: its stub echoes
    back whatever content was PATCHed, so it would pass against a backend that
    persisted nothing.

    Persistence is the whole claim, so the test reloads. A reload refetches from
    FastAPI, which means the new text has to have survived the round trip and be
    in Postgres — not merely in React state from the optimistic update.
  */
  await asFixtureSubject(page);
  await page.goto("/memories");
  await expect(page.getByTestId("memories-loading")).toHaveCount(0, { timeout: 60_000 });

  const rows = page.getByTestId("memory-row");
  const before = await rows.count();
  expect(before, "the fixture corpus seeded no rows — beforeAll did not do its job").toBeGreaterThan(0);

  const original = (await page.getByTestId("memory-content").first().textContent()) ?? "";
  expect(original.trim().length).toBeGreaterThan(0);

  // Marked with the run's timestamp so a stale row cannot masquerade as a fresh
  // one, and so a failed run leaves a traceable artefact rather than a mystery.
  const edited = `Edited by the live e2e at ${new Date().toISOString()}.`;

  await page.getByTestId("memory-edit").first().click();
  const input = page.getByTestId("memory-edit-input");
  await expect(input).toBeVisible();
  await input.fill(edited);

  // WAIT FOR THE ACTUAL PATCH RESPONSE, armed BEFORE the click.
  //
  // The first version of this test asserted `memories-notice` had count 0 and
  // then reloaded, believing that meant the write had landed. It does not:
  // `toHaveCount(0)` is satisfied instantly by an absent element, so the reload
  // raced the round trip. `PATCH /memories/{id}` re-embeds the new content
  // against a provider capped at 3 requests/minute, so it can take tens of
  // seconds — and the audit log showed the write committing two seconds AFTER
  // the reload had already refetched the old text.
  //
  // That failure looked exactly like "the edit did not persist" while the
  // backend was behaving perfectly. Worse, no amount of waiting after the
  // reload could fix it: the panel fetches once on mount, so polling the DOM
  // re-reads a response that was already stale when it arrived.
  const patched = page.waitForResponse(
    (r) => r.url().includes("/api/memories/") && r.request().method() === "PATCH",
    { timeout: 120_000 },
  );
  await page.getByTestId("memory-save").click();

  // Optimistic first: the new text appears without waiting for the round trip.
  //
  // Located by TEXT, not by position. The fix write-up for the delete test
  // claimed "both panel tests now identify rows by text" — that was FALSE of
  // this test, and a cold verifier caught it under `--repeat-each=2`: an
  // `assistant_note` row captured asynchronously by the CHAT test landed after
  // `beforeEach` had reseeded, took position 1 by `created_at DESC`, and the
  // positional assertion failed against a perfectly healthy system. (The tell
  // was the text: the fixture row reads "I have been learning fingerstyle
  // guitar", the received row read "The user has been learning…" — third
  // person, i.e. machine-captured.)
  const editedRow = page.getByTestId("memory-content").filter({ hasText: edited });
  await expect(editedRow).toHaveCount(1);

  const response = await patched;
  expect(
    response.status(),
    `PATCH /memories/{id} answered ${response.status()}: ${await response.text()}`,
  ).toBe(200);

  // THE ASSERTION THAT MATTERS. A reload throws away all client state and asks
  // the backend again.
  await page.reload();
  await expect(page.getByTestId("memories-loading")).toHaveCount(0, { timeout: 60_000 });

  await expect(
    page.getByTestId("memory-content").filter({ hasText: edited }),
    "the edit did not survive a reload — it was applied optimistically in the browser " +
      "but never persisted through PATCH /memories/{id} to Postgres",
  ).toHaveCount(1, { timeout: 60_000 });

  // The edit REPLACED content; the original text must be gone. Asserted by
  // text rather than by comparing counts, because a concurrent capture can add
  // a row at any moment and `toHaveCount(before)` would then fail for a reason
  // that has nothing to do with editing.
  await expect(
    page.getByTestId("memory-content").filter({ hasText: original }),
    "the original text is still present — the edit added a row instead of replacing one",
  ).toHaveCount(0);
});
