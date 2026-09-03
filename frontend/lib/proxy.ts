/**
 * Shared server-side proxy helpers for the Next route handlers.
 *
 * WHY EVERY BACKEND CALL GOES THROUGH A ROUTE HANDLER (project decision D13)
 * -------------------------------------------------------------------------
 * `api/main.py` mounts no CORS middleware, so a preflighted browser request
 * straight to `http://localhost:8000` is refused. Rather than edit a backend
 * this milestone does not own, the browser talks to this app's own origin and
 * Node forwards the call. The backend never has to be browser-reachable.
 *
 * `app/api/chat/route.ts` established that shape for `POST /chat` and is left
 * alone — it streams, and streaming has enough special handling (no buffering,
 * metadata headers, an unbounded read) to be worth keeping separate from the
 * plain JSON forwarding here.
 *
 * WHAT THIS FILE IS CAREFUL ABOUT
 * ------------------------------
 * *Identity.* Every `/memories` endpoint resolves its caller from a
 * `subject_id` query parameter, an `X-Subject-Id` header, or a
 * `DEFAULT_SUBJECT_ID` in the backend's environment, in that order
 * (`api/memories.py:resolve_identity`). The browser knows its subject only
 * after the first chat turn mints one, so the identity is forwarded when the
 * page has it and omitted when it does not — letting the backend's own fallback
 * apply rather than inventing an id here.
 *
 * *Error shape.* Backend errors are FastAPI's `{"detail": ...}`. They are
 * passed through with their status intact rather than collapsed into 500s: the
 * memory panel distinguishes 404 (already deleted elsewhere) from 400 (bad
 * identity) from 502 (backend down), and it can only do that if the status
 * survives the hop.
 */

import { NextResponse } from "next/server";

import { ChatError, resolveApiBaseUrl } from "@/lib/api";

/** Headers carrying pagination facts, copied back to the browser verbatim. */
const PASSTHROUGH_RESPONSE_HEADERS = ["X-Total-Count", "X-Limit", "X-Offset"];

/**
 * Undici reports a refused connection as `TypeError: fetch failed` whose
 * `cause` is often an `AggregateError`. Stringifying that yields the bare word
 * "AggregateError", so unwrap to something a human can act on.
 *
 * Mirrors `describeFetchFailure` in `app/api/chat/route.ts`. Deliberately not
 * shared with it: that file is the streaming path and is meant to stay
 * readable end-to-end without chasing an import, and this is twelve lines.
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

/** Copy the caller's identity onto an upstream request, when it has one. */
export function forwardIdentity(request: Request, headers: Headers): void {
  for (const name of ["X-Subject-Id", "X-Actor-Id"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
}

/**
 * Forward one JSON request to the backend and relay its JSON answer.
 *
 * `path` is everything after the base URL, query string included.
 */
export async function proxyJson(
  request: Request,
  path: string,
  init: { method: string; body?: unknown } = { method: "GET" },
): Promise<Response> {
  let baseUrl: string;
  try {
    baseUrl = resolveApiBaseUrl();
  } catch (cause) {
    const detail = cause instanceof ChatError ? cause.message : "API base URL is not configured.";
    return NextResponse.json({ detail }, { status: 500 });
  }

  const headers = new Headers();
  forwardIdentity(request, headers);
  if (init.body !== undefined) headers.set("Content-Type", "application/json");

  let upstream: Response;
  try {
    upstream = await fetch(`${baseUrl}${path}`, {
      method: init.method,
      headers,
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      cache: "no-store",
    });
  } catch (cause) {
    // The backend URL is internal: naming it helps while developing and is a
    // hostname leak in production, so it appears only outside production.
    const where =
      process.env.NODE_ENV === "production" ? "the backend" : `the backend at ${baseUrl}`;
    return NextResponse.json(
      { detail: `Could not reach ${where}. ${describeFetchFailure(cause)}` },
      { status: 502 },
    );
  }

  const text = await upstream.text();
  const responseHeaders = new Headers({ "Cache-Control": "no-store" });
  for (const name of PASSTHROUGH_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }

  // 204 has no body by definition, and `new Response("")` with status 204
  // throws in undici. Answer it directly.
  if (upstream.status === 204 || text === "") {
    return new NextResponse(null, { status: upstream.status, headers: responseHeaders });
  }

  responseHeaders.set(
    "Content-Type",
    upstream.headers.get("Content-Type") ?? "application/json",
  );
  // Relay the body verbatim with the upstream status. Not re-serialised: a
  // backend error body is already `{"detail": ...}`, and re-wrapping it would
  // nest one envelope inside another.
  return new NextResponse(text, { status: upstream.status, headers: responseHeaders });
}
