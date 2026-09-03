/**
 * Server-side proxy from this app to the FastAPI backend.
 *
 * Why this exists: `api/main.py` mounts no CORS middleware, so a preflighted
 * `POST` straight from the browser to `http://localhost:8000/chat` is refused
 * (`OPTIONS /chat` answers 405). Rather than edit backend code this milestone
 * does not own, the browser posts to this same-origin route and Node forwards
 * the call. No preflight, no CORS, and the backend origin never has to be
 * reachable from the user's browser at all.
 *
 * If CORS middleware is added to the backend later, `lib/api.ts` can point
 * `sendChat` straight at `resolveApiBaseUrl()` and this file can go away.
 */

import { NextResponse } from "next/server";

import { ChatError, resolveApiBaseUrl, type ChatRequest } from "@/lib/api";

/** Never cache a chat turn. */
export const dynamic = "force-dynamic";

/**
 * Turn a Node fetch rejection into something a human can act on.
 *
 * Undici wraps connection failures as `TypeError: fetch failed` whose `cause` is
 * often an `AggregateError` (one entry per resolved address). Stringifying that
 * yields the bare word "AggregateError", which tells a reader nothing — so we
 * unwrap to the first real error and prefer its syscall code (`ECONNREFUSED`).
 */
function describeFetchFailure(cause: unknown): string {
  const seen = new Set<unknown>();
  let current: unknown = cause;

  while (current instanceof Error && !seen.has(current)) {
    seen.add(current);

    const aggregated = (current as { errors?: unknown }).errors;
    if (Array.isArray(aggregated) && aggregated.length > 0) {
      current = aggregated[0];
      continue;
    }

    const code = (current as { code?: unknown }).code;
    if (typeof code === "string") {
      return current.message ? `${code}: ${current.message}` : code;
    }

    const nested = (current as { cause?: unknown }).cause;
    if (nested instanceof Error) {
      current = nested;
      continue;
    }

    return current.message || current.name;
  }

  return String(cause);
}

export async function POST(request: Request): Promise<Response> {
  let body: ChatRequest;
  try {
    body = (await request.json()) as ChatRequest;
  } catch {
    return NextResponse.json({ detail: "Request body was not valid JSON." }, { status: 400 });
  }

  if (typeof body?.message !== "string" || body.message.trim() === "") {
    return NextResponse.json({ detail: "A non-empty `message` is required." }, { status: 400 });
  }

  let baseUrl: string;
  try {
    baseUrl = resolveApiBaseUrl();
  } catch (cause) {
    const detail = cause instanceof ChatError ? cause.message : "API base URL is not configured.";
    return NextResponse.json({ detail }, { status: 500 });
  }

  // The client's `stream` flag is forwarded, not overridden. M2.5's UI sends
  // `false` and gets a single JSON object; a caller that sends `true` gets the
  // backend's chunked `text/plain` body passed straight through. Hardcoding
  // `false` here would have left a trap for M6.
  //
  // Capture happens either way — the backend enqueues it after the response is
  // sent, which is the async seam this milestone exists to show off.
  const upstreamBody: ChatRequest = {
    message: body.message,
    subject_id: body.subject_id ?? null,
    actor_id: body.actor_id ?? null,
    stream: body.stream ?? false,
    capture: body.capture ?? true,
  };

  let upstream: Response;
  try {
    // No timeout on purpose: the completion provider is rate-limited and a real
    // reply can legitimately take several seconds.
    upstream = await fetch(`${baseUrl}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(upstreamBody),
      cache: "no-store",
    });
  } catch (cause) {
    // The backend URL is internal. Naming it helps enormously while developing
    // and is a hostname leak in production, so it is included only in dev.
    const where =
      process.env.NODE_ENV === "production" ? "the backend" : `the backend at ${baseUrl}`;
    return NextResponse.json(
      { detail: `Could not reach ${where}. ${describeFetchFailure(cause)}` },
      { status: 502 },
    );
  }

  // A failure is short and needs reshaping into our JSON error envelope, so
  // buffering it is fine — and it is the only path here that buffers.
  if (!upstream.ok) {
    const text = await upstream.text();
    return NextResponse.json(
      {
        detail: text
          ? text.slice(0, 1000)
          : `Backend responded ${upstream.status} with an empty body.`,
      },
      { status: upstream.status },
    );
  }

  // Pass the body through as a stream rather than `await upstream.text()`.
  // Buffering here would hold every byte until the backend finished, which
  // defeats the point of `stream: true` and would have made M6's switch to
  // token streaming a rewrite of this route instead of a change to the client.
  const headers = new Headers({
    // Mirror whatever the backend sent — `application/json` for stream:false,
    // `text/plain; charset=utf-8` for stream:true.
    "Content-Type": upstream.headers.get("Content-Type") ?? "application/json",
    "Cache-Control": "no-store, no-transform",
    // Ask any intermediary proxy not to buffer the stream either.
    "X-Accel-Buffering": "no",
  });

  // Forward identity AND retrieval metadata.
  //
  // M6 NOTE — this list was `["X-Subject-Id", "X-Actor-Id"]` and the four
  // X-Memory-* headers were being dropped here. Everything still "worked":
  // replies streamed, nothing errored, and `readStreamMetadata()` simply saw no
  // `X-Memory-Degraded` and reported not-degraded for every turn. The
  // "answering without memory" indicator (step 6) could therefore never appear,
  // and its e2e test would have failed against a backend that was behaving
  // perfectly.
  //
  // The backend already names all six in `Access-Control-Expose-Headers`
  // (`api/chat.py`), which is the list a *browser* needs to reveal them to page
  // JavaScript on a cross-origin response. That does nothing here: this route is
  // a server-side hop, so the headers have to be copied across by hand. Two
  // different mechanisms, and satisfying one says nothing about the other.
  for (const name of [
    "X-Subject-Id",
    "X-Actor-Id",
    "X-Memory-Degraded",
    "X-Memory-Count",
    "X-Memory-Ids",
    "X-Memory-Degraded-Reason",
  ]) {
    // Only forward what the backend actually sent; an empty header is worse
    // than an absent one, because `readStreamMetadata` can distinguish "absent"
    // from "explicitly false" but not from "".
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }

  return new NextResponse(upstream.body, { status: 200, headers });
}
