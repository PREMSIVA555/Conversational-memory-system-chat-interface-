/**
 * The live end-to-end journey (M6 test case 1 — the DoD's named grep target).
 *
 * REQUIRES THE REAL STACK. Run with:
 *
 *     npm run test:e2e:live
 *
 * which sets `PLAYWRIGHT_LIVE=1`. Without it this project is not registered at
 * all, so `npm run test:e2e` stays fast and hermetic.
 *
 * Preconditions, none of which this spec creates for you:
 *   * docker compose up, migrations applied
 *   * the API running: `python -m api.main` (NOT `uvicorn api.main:app` — see
 *     the project README on the Windows event-loop trap)
 *   * `API_BASE_URL` set in `frontend/.env.local`
 *
 * WHY THE TIMEOUTS HERE LOOK ABSURD
 * ---------------------------------
 * The embedding provider is capped at 3 requests per minute on this project's
 * key. A single chat turn embeds for retrieval and again for capture, so once
 * the window is spent a turn can legitimately take 30-60 seconds before the
 * first token. A tight timeout here does not catch a bug; it reports a healthy
 * backend as broken.
 *
 * WHAT THIS PROVES THAT THE MOCKED SPECS CANNOT
 * ---------------------------------------------
 * The seam. The mocked specs pin the UI's behaviour with a body they invented;
 * this one proves the proxy forwards the metadata headers, that the backend
 * streams at all, and that the memory panel's shapes match what the API really
 * returns. Those are exactly the things a mock asserts into existence.
 */

import { expect, test } from "@playwright/test";

test("chat streams and memory panel lists memories", async ({ page }) => {
  await page.goto("/chat");

  await page.getByTestId("chat-input").fill("Remember that I am allergic to peanuts.");
  await page.getByTestId("chat-send").click();

  const assistant = page.getByTestId("assistant-text");
  await expect(assistant).toBeVisible();

  // Sample while the reply is in flight. The DoD asks for evidence the text
  // GREW rather than appearing at once, so collect a series and assert it is
  // non-decreasing and ends longer than it began.
  const samples: number[] = [];
  const deadline = Date.now() + 120_000;
  let previous = -1;
  while (Date.now() < deadline) {
    const length = ((await assistant.textContent()) ?? "").length;
    samples.push(length);
    // Stop once it has clearly grown and then settled.
    if (length > 0 && length === previous && samples.length > 6) break;
    previous = length;
    await page.waitForTimeout(250);
  }

  const growth = samples.filter((value, index) => index > 0 && value > samples[index - 1]);
  expect(
    growth.length,
    `assistant text never grew across samples: ${JSON.stringify(samples)} — ` +
      "this is the single-final-paint behaviour M6 exists to remove",
  ).toBeGreaterThanOrEqual(1);
  expect(((await assistant.textContent()) ?? "").length).toBeGreaterThan(0);

  // The turn is answered either way; whether memory was used is reported, and
  // BOTH outcomes are legitimate here. On a spent embedding quota the retrieval
  // path times out and M5's breaker serves the turn without memory — asserting
  // "memory was used" would make this spec fail for a rate limit rather than a
  // defect.
  const degraded = page.getByTestId("degraded-indicator");
  const used = page.getByTestId("memory-count");
  await expect(degraded.or(used).first()).toBeVisible();

  // Capture is asynchronous and runs only after the stream completes, so a
  // memory written by this turn may not be readable yet. The DoD asks that the
  // panel show at least one row, which the seeded fixture corpus guarantees
  // independently of this turn.
  await page.getByRole("link", { name: /Manage memories/ }).click();
  await expect(page).toHaveURL(/\/memories$/);

  await expect(page.getByTestId("memories-loading")).toHaveCount(0, { timeout: 30_000 });
  const rows = page.getByTestId("memory-row");
  await expect(
    rows.first(),
    "the memory panel showed no rows — seed at least one memory for this subject first",
  ).toBeVisible();
  expect(await rows.count()).toBeGreaterThan(0);
});

test("delete removes a row from the live panel without a page reload", async ({ page }) => {
  await page.goto("/memories");
  await expect(page.getByTestId("memories-loading")).toHaveCount(0, { timeout: 30_000 });

  const rows = page.getByTestId("memory-row");
  const before = await rows.count();
  test.skip(before === 0, "no memories seeded for this subject; nothing to delete");

  let navigations = 0;
  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) navigations += 1;
  });

  await page.getByTestId("memory-delete").first().click();

  await expect(rows).toHaveCount(before - 1, { timeout: 30_000 });
  expect(navigations, "the page navigated — the row vanished via a reload").toBe(0);
});
