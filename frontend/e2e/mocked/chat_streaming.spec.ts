/**
 * Mocked chat specs — the rendering behaviour M6 exists to produce.
 *
 * WHY THESE OVERRIDE `window.fetch` INSTEAD OF USING `page.route`
 * ---------------------------------------------------------------
 * `route.fulfill()` hands the browser one complete body. A page that buffers
 * the whole response and paints once would therefore look identical to one that
 * renders progressively: both go from empty to full in a single step, and a
 * length-sampling assertion would pass for both. The test would assert nothing.
 *
 * So these install a `fetch` that returns a real `ReadableStream`, enqueuing
 * chunks with a delay between them. That is the only way an assertion about
 * INCREMENTAL rendering can actually fail when the implementation regresses —
 * which is the whole point of `test_streamed_tokens_render_incrementally`.
 *
 * No backend, no database, no provider quota, so these are deterministic and
 * fast. The live counterpart in `../live/` proves the wiring instead.
 */

import { expect, test, type Page } from "@playwright/test";

const SUBJECT = "11111111-2222-3333-4444-555555555555";

interface StreamOptions {
  chunks: string[];
  headers?: Record<string, string>;
  /** Delay before each chunk, so the page must paint between them. */
  delayMs?: number;
}

/**
 * Shape handed to the browser.
 *
 * NOTE: `addInitScript` serialises the callback and runs it in the PAGE, where
 * this file's module scope does not exist. Every value it needs — including the
 * subject id — has to travel inside this argument. Referencing a module
 * constant instead fails at runtime with "X is not defined", which surfaces as
 * the UI's own "Could not reach the app server" error rather than as an obvious
 * test bug.
 */
interface BrowserStreamOptions {
  chunks: string[];
  headers: Record<string, string>;
  delayMs: number;
  subject: string;
}

/**
 * Install a streaming `fetch` for `/api/chat` before any page script runs.
 *
 * `addInitScript` is used rather than `evaluate` so the override is in place
 * before React hydrates and captures a reference to the original.
 */
async function installStreamingChat(page: Page, options: StreamOptions): Promise<void> {
  await page.addInitScript(
    (opts: BrowserStreamOptions) => {
      const realFetch = window.fetch.bind(window);
      window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : (input as Request).url;

        if (!url.includes("/api/chat")) {
          return realFetch(input as RequestInfo, init);
        }

        const stream = new ReadableStream<Uint8Array>({
          async start(controller) {
            const encoder = new TextEncoder();
            for (const chunk of opts.chunks) {
              await new Promise((resolve) => setTimeout(resolve, opts.delayMs));
              controller.enqueue(encoder.encode(chunk));
            }
            controller.close();
          },
        });

        return new Response(stream, {
          status: 200,
          headers: {
            "Content-Type": "text/plain; charset=utf-8",
            "X-Subject-Id": opts.subject,
            "X-Actor-Id": opts.subject,
            ...opts.headers,
          },
        });
      };
    },
    {
      chunks: options.chunks,
      headers: options.headers ?? {},
      delayMs: options.delayMs ?? 60,
      subject: SUBJECT,
    },
  );
}

/** Install a `fetch` for `/api/chat` that fails with a JSON error envelope. */
async function installFailingChat(page: Page, status: number, detail: string): Promise<void> {
  await page.addInitScript(
    (opts: { status: number; detail: string }) => {
      const realFetch = window.fetch.bind(window);
      window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : (input as Request).url;
        if (!url.includes("/api/chat")) return realFetch(input as RequestInfo, init);
        return new Response(JSON.stringify({ detail: opts.detail }), {
          status: opts.status,
          headers: { "Content-Type": "application/json" },
        });
      };
    },
    { status, detail },
  );
}

async function send(page: Page, message: string): Promise<void> {
  await page.getByTestId("chat-input").fill(message);
  await page.getByTestId("chat-send").click();
}

test("test_streamed_tokens_render_incrementally", async ({ page }) => {
  const words = "The quick brown fox jumps over the lazy dog and keeps running".split(" ");
  await installStreamingChat(page, {
    chunks: words.map((word, index) => (index === 0 ? word : ` ${word}`)),
    headers: { "X-Memory-Degraded": "false", "X-Memory-Count": "2" },
    delayMs: 80,
  });

  await page.goto("/chat");
  await send(page, "tell me something");

  const assistant = page.getByTestId("assistant-text");
  await expect(assistant).toBeVisible();

  // Sample while the response is in flight. With 12 chunks at 80ms each the
  // stream runs for roughly a second, so these samples land mid-stream.
  const samples: number[] = [];
  for (let i = 0; i < 14; i += 1) {
    samples.push(((await assistant.textContent()) ?? "").length);
    await page.waitForTimeout(70);
  }
  await expect(assistant).toContainText("running");
  samples.push(((await assistant.textContent()) ?? "").length);

  // Text is appended, never replaced.
  for (let i = 1; i < samples.length; i += 1) {
    expect(samples[i]).toBeGreaterThanOrEqual(samples[i - 1]);
  }

  // The real assertion: at least one INTERMEDIATE length. A buffered
  // implementation produces only 0s followed by the final length, so the set of
  // distinct values would be {0, final} and this fails.
  const distinct = [...new Set(samples)].sort((a, b) => a - b);
  const intermediate = distinct.filter(
    (value) => value > 0 && value < distinct[distinct.length - 1],
  );
  expect(
    intermediate.length,
    `no partial render observed; sampled lengths were ${JSON.stringify(distinct)} — ` +
      "the reply appeared in one paint, which is the behaviour M6 removes",
  ).toBeGreaterThanOrEqual(1);
});

test("test_degraded_flag_shows_no_memory_indicator", async ({ page }) => {
  await installStreamingChat(page, {
    chunks: ["Answering ", "from general ", "knowledge."],
    headers: {
      "X-Memory-Degraded": "true",
      "X-Memory-Degraded-Reason": "timeout",
      "X-Memory-Count": "0",
    },
  });

  await page.goto("/chat");
  await send(page, "what do you know about me?");

  const indicator = page.getByTestId("degraded-indicator");
  await expect(indicator).toBeVisible();
  await expect(indicator).toContainText("Answering without memory");
  // The reason is surfaced, not swallowed: "timeout" and "breaker open" send a
  // reader to very different places.
  await expect(indicator).toContainText("timeout");
});

test("a non-degraded turn reports how many memories were used", async ({ page }) => {
  await installStreamingChat(page, {
    chunks: ["You mentioned ", "that before."],
    headers: { "X-Memory-Degraded": "false", "X-Memory-Count": "3" },
  });

  await page.goto("/chat");
  await send(page, "what did I say?");

  await expect(page.getByTestId("memory-count")).toContainText("3 memories used");
  await expect(page.getByTestId("degraded-indicator")).toHaveCount(0);
});

test("test_chat_input_disabled_while_streaming", async ({ page }) => {
  // A long delay before the first chunk leaves the in-flight state observable.
  await installStreamingChat(page, { chunks: ["done"], delayMs: 1500 });

  await page.goto("/chat");
  await send(page, "hello");

  await expect(page.getByTestId("chat-input")).toBeDisabled();
  await expect(page.getByTestId("chat-send")).toBeDisabled();

  // ...and re-enabled once the stream finishes, or the UI would be stuck.
  await expect(page.getByTestId("chat-input")).toBeEnabled({ timeout: 10_000 });
});

test("the streamed reply renders inside the aria-live region", async ({ page }) => {
  // M2.5 failed verification once for rendering the reply NEXT TO the announced
  // region rather than within it, so this asserts containment structurally.
  await installStreamingChat(page, { chunks: ["Announced ", "correctly."] });

  await page.goto("/chat");
  await send(page, "hi");
  await expect(page.getByTestId("assistant-text")).toContainText("Announced correctly.");

  const inside = await page.evaluate(() => {
    const region = document.querySelector('[role="log"][aria-live="polite"]');
    const text = document.querySelector('[data-testid="assistant-text"]');
    return Boolean(region && text && region.contains(text));
  });
  expect(inside, "the streamed text is not inside the aria-live region").toBe(true);
});

test("a backend failure is surfaced and leaves no empty reply bubble", async ({ page }) => {
  await installFailingChat(page, 502, "Could not reach the backend. ECONNREFUSED");

  await page.goto("/chat");
  await send(page, "hello");

  const error = page.getByTestId("chat-error");
  await expect(error).toBeVisible();
  await expect(error).toContainText("ECONNREFUSED");
  // The placeholder assistant turn must be gone: a blank bubble above an error
  // reads as "the assistant replied with nothing".
  await expect(page.getByTestId("assistant-turn")).toHaveCount(0);
  // And the composer must be usable again.
  await expect(page.getByTestId("chat-input")).toBeEnabled();
});
