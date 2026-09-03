/**
 * Mocked memory-panel specs (M6 steps 8, 9, 11).
 *
 * These intercept `/api/memories*`, so they assert the panel's own behaviour —
 * optimistic update, rollback, no-reload delete, the empty state — without a
 * database. The rollback and no-reload cases in particular are near-impossible
 * to provoke reliably against a real backend, because they need a delete to
 * FAIL on demand.
 */

import { expect, test, type Page, type Route } from "@playwright/test";

const SUBJECT = "11111111-2222-3333-4444-555555555555";

function memory(id: string, content: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    subject_id: SUBJECT,
    actor_id: SUBJECT,
    content,
    source: "chat",
    importance: 0.7,
    confidence: 0.9,
    weight: 1,
    reinforcement_count: 0,
    created_at: "2026-09-01T10:00:00+00:00",
    updated_at: "2026-09-01T10:00:00+00:00",
    last_accessed_at: "2026-09-01T10:00:00+00:00",
    ...extra,
  };
}

const ID_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa";
const ID_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb";

/** Serve a fixed list from `GET /api/memories`. */
async function mockList(page: Page, rows: ReturnType<typeof memory>[]) {
  await page.route("**/api/memories?**", async (route: Route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "X-Total-Count": String(rows.length), "X-Limit": "100", "X-Offset": "0" },
      body: JSON.stringify(rows),
    });
  });
  // The page may request without a query string too.
  await page.route("**/api/memories", async (route: Route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "X-Total-Count": String(rows.length), "X-Limit": "100", "X-Offset": "0" },
      body: JSON.stringify(rows),
    });
  });
}

test("test_memory_panel_lists_at_least_one_row", async ({ page }) => {
  await mockList(page, [memory(ID_A, "I am lactose intolerant."), memory(ID_B, "I cycle to work.")]);

  await page.goto("/memories");

  await expect(page.getByTestId("memory-row")).toHaveCount(2);
  await expect(page.getByTestId("memory-content").first()).toContainText("lactose intolerant");
  await expect(page.getByTestId("memory-source").first()).toContainText("chat");
});

test("test_empty_memory_list_shows_empty_state", async ({ page }) => {
  await mockList(page, []);

  await page.goto("/memories");

  // Step 11: an explicit empty state, never a blank page.
  const empty = page.getByTestId("memories-empty");
  await expect(empty).toBeVisible();
  await expect(empty).toContainText("No memories yet");
  await expect(page.getByTestId("memory-row")).toHaveCount(0);
});

test("test_delete_removes_row_without_page_reload", async ({ page }) => {
  await mockList(page, [memory(ID_A, "First memory."), memory(ID_B, "Second memory.")]);
  await page.route(`**/api/memories/${ID_A}`, async (route: Route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ id: ID_A, deleted: true }),
    });
  });

  await page.goto("/memories");
  await expect(page.getByTestId("memory-row")).toHaveCount(2);

  // Count navigations. A full reload would fire this again; an in-place update
  // must not. This is the assertion the DoD line is about — the row vanishing
  // is necessary but not sufficient, because a reload also makes it vanish.
  const navigations = { count: 0 };
  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) navigations.count += 1;
  });

  await page.getByTestId("memory-delete").first().click();

  await expect(page.getByTestId("memory-row")).toHaveCount(1);
  await expect(page.getByTestId("memory-content").first()).toContainText("Second memory.");
  expect(navigations.count).toBe(0);
});

test("test_failed_delete_restores_row", async ({ page }) => {
  await mockList(page, [memory(ID_A, "Fragile memory."), memory(ID_B, "Other memory.")]);
  await page.route(`**/api/memories/${ID_A}`, async (route: Route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: "database is on fire" }),
    });
  });

  await page.goto("/memories");
  await expect(page.getByTestId("memory-row")).toHaveCount(2);

  await page.getByTestId("memory-delete").first().click();

  // Optimistically removed, then restored when the call failed.
  await expect(page.getByTestId("memory-row")).toHaveCount(2);
  await expect(page.getByTestId("memory-content").first()).toContainText("Fragile memory.");

  const notice = page.getByTestId("memories-notice");
  await expect(notice).toBeVisible();
  await expect(notice).toContainText("database is on fire");
});

test("a 404 on delete keeps the row removed rather than restoring it", async ({ page }) => {
  // Deliberate asymmetry with the test above: 404 means the row is genuinely
  // gone (another tab, or a previous click whose response was lost). Restoring
  // it would show the user a memory that does not exist.
  await mockList(page, [memory(ID_A, "Already deleted elsewhere.")]);
  await page.route(`**/api/memories/${ID_A}`, async (route: Route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    await route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "memory not found" }),
    });
  });

  await page.goto("/memories");
  await page.getByTestId("memory-delete").first().click();

  await expect(page.getByTestId("memory-row")).toHaveCount(0);
  await expect(page.getByTestId("memories-notice")).toContainText("already been deleted");
});

test("inline edit updates the row optimistically and keeps the server's answer", async ({
  page,
}) => {
  await mockList(page, [memory(ID_A, "Original content.")]);
  await page.route(`**/api/memories/${ID_A}`, async (route: Route) => {
    if (route.request().method() !== "PATCH") return route.fallback();
    const body = route.request().postDataJSON() as { content?: string };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(
        memory(ID_A, body.content ?? "", { updated_at: "2026-09-02T12:00:00+00:00" }),
      ),
    });
  });

  await page.goto("/memories");
  await page.getByTestId("memory-edit").first().click();

  const input = page.getByTestId("memory-edit-input");
  await expect(input).toBeVisible();
  await input.fill("Corrected content.");
  await page.getByTestId("memory-save").click();

  await expect(page.getByTestId("memory-content").first()).toContainText("Corrected content.");
  await expect(page.getByTestId("memories-notice")).toHaveCount(0);
});

test("a failed edit rolls the row back to its previous content", async ({ page }) => {
  await mockList(page, [memory(ID_A, "Original content.")]);
  await page.route(`**/api/memories/${ID_A}`, async (route: Route) => {
    if (route.request().method() !== "PATCH") return route.fallback();
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: "re-embedding failed" }),
    });
  });

  await page.goto("/memories");
  await page.getByTestId("memory-edit").first().click();
  await page.getByTestId("memory-edit-input").fill("Doomed edit.");
  await page.getByTestId("memory-save").click();

  await expect(page.getByTestId("memory-content").first()).toContainText("Original content.");
  await expect(page.getByTestId("memories-notice")).toContainText("re-embedding failed");
});

test("a failed load shows an error state with a retry that works", async ({ page }) => {
  let attempt = 0;
  const handler = async (route: Route) => {
    if (route.request().method() !== "GET") return route.fallback();
    attempt += 1;
    if (attempt === 1) {
      return route.fulfill({
        status: 502,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Could not reach the backend." }),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "X-Total-Count": "1" },
      body: JSON.stringify([memory(ID_A, "Recovered.")]),
    });
  };
  await page.route("**/api/memories?**", handler);
  await page.route("**/api/memories", handler);

  await page.goto("/memories");
  await expect(page.getByTestId("memories-error")).toBeVisible();

  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByTestId("memory-content").first()).toContainText("Recovered.");
});
