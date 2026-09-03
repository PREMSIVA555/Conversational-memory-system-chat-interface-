/**
 * Typed client for the memory-system backend.
 *
 * The shapes here mirror `api/chat.py` exactly — `ChatRequest` and the
 * `stream=false` JSON body of `POST /chat`. Do not drift from that file.
 *
 * M2.5 uses `stream: false`, which returns a single JSON object. The backend
 * also supports `stream: true` (a `text/plain` chunked body); M6 will switch to
 * it for token streaming.
 *
 * What that switch involves: the proxy route already forwards the `stream` flag
 * and pipes the upstream body through without buffering, so it does not need to
 * change. `sendChat` below does — it calls `response.text()`, which waits for
 * the whole body. M6 replaces that with a reader over `response.body` and a
 * callback per chunk. So: a real change here, no change to the route.
 *
 * Transport note: the browser does not talk to the FastAPI app directly. It
 * posts to this Next.js app's own `/api/chat` route, which forwards server-side
 * to `API_BASE_URL`. See `app/api/chat/route.ts` for why.
 */

import { readTokenStream, type StreamHandlers } from "@/lib/stream";

/** Request body of `POST /chat`. Mirrors `api.chat.ChatRequest`. */
export interface ChatRequest {
  /** The user's turn. */
  message: string;
  /** Whose memory this turn belongs to. The backend generates one when omitted. */
  subject_id?: string | null;
  /** Who is writing. The backend defaults this to `subject_id`. */
  actor_id?: string | null;
  /** Stream the reply body. M2.5 sends `false`; M6 will send `true`. */
  stream?: boolean;
  /** Set false to reply without remembering the turn. */
  capture?: boolean;
}

/** JSON body returned by `POST /chat` when `stream` is false. */
export interface ChatResponse {
  reply: string;
  subject_id: string;
  actor_id: string;
}

/** A request that did not produce a usable reply. Carries what we can show a human. */
export class ChatError extends Error {
  /** HTTP status, when the failure got far enough to have one. */
  readonly status?: number;
  /** Free-text detail from the backend or the proxy, when present. */
  readonly detail?: string;

  constructor(message: string, options: { status?: number; detail?: string } = {}) {
    super(message);
    this.name = "ChatError";
    this.status = options.status;
    this.detail = options.detail;
  }
}

/**
 * Base URL of the FastAPI backend, from the environment. Never hardcode this.
 *
 * Deliberately NOT prefixed `NEXT_PUBLIC_`: this is read only server-side, by
 * the proxy route. Next inlines `NEXT_PUBLIC_*` into the client bundle wherever
 * it is referenced, so that prefix would mean any future client-side use of
 * this function silently publishes the internal backend URL in public JS.
 * Without the prefix that is impossible — the value is `undefined` in a browser.
 *
 * Missing or blank is a configuration error we surface loudly rather than
 * silently defaulting, so a misconfigured deployment fails with a readable
 * message instead of quietly hitting the wrong host.
 */
export function resolveApiBaseUrl(): string {
  const raw = process.env.API_BASE_URL?.trim();
  if (!raw) {
    throw new ChatError(
      "API_BASE_URL is not set. Copy .env.local.example to .env.local and restart the dev server.",
    );
  }
  return raw.replace(/\/+$/, "");
}

/** Path of this app's own proxy route. Same origin, so no CORS is involved. */
const PROXY_PATH = "/api/chat";

/**
 * Send one turn and resolve with the reply.
 *
 * Deliberately has no client-side timeout: the completion provider is
 * rate-limited and gpt-oss models think before emitting content, so a legitimate
 * reply can take several seconds. Aborting early would report a healthy backend
 * as broken.
 */
export async function sendChat(request: ChatRequest): Promise<ChatResponse> {
  const body: ChatRequest = {
    message: request.message,
    subject_id: request.subject_id ?? null,
    actor_id: request.actor_id ?? null,
    stream: request.stream ?? false,
    capture: request.capture ?? true,
  };

  let response: Response;
  try {
    response = await fetch(PROXY_PATH, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (cause) {
    throw new ChatError("Could not reach the app server.", {
      detail: cause instanceof Error ? cause.message : String(cause),
    });
  }

  const text = await response.text();
  let payload: unknown = undefined;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = undefined;
    }
  }

  if (!response.ok) {
    throw new ChatError(`The backend returned ${response.status}.`, {
      status: response.status,
      detail: extractDetail(payload) ?? (text ? text.slice(0, 500) : undefined),
    });
  }

  if (!isChatResponse(payload)) {
    throw new ChatError("The backend returned a reply in an unexpected shape.", {
      status: response.status,
      detail: text ? text.slice(0, 500) : undefined,
    });
  }

  return payload;
}

function isChatResponse(value: unknown): value is ChatResponse {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.reply === "string" &&
    typeof candidate.subject_id === "string" &&
    typeof candidate.actor_id === "string"
  );
}

function extractDetail(payload: unknown): string | undefined {
  if (typeof payload !== "object" || payload === null) return undefined;
  const candidate = payload as Record<string, unknown>;
  if (typeof candidate.detail === "string") return candidate.detail;
  if (typeof candidate.error === "string") return candidate.error;
  return undefined;
}

/**
 * Send one turn and render the reply as it arrives (M6 steps 3 and 5).
 *
 * This is the streaming sibling of `sendChat`. The difference is the whole
 * point of the milestone: `sendChat` calls `response.text()`, which waits for
 * the last byte before the caller sees anything, so the answer appears in a
 * single paint. Here every chunk reaches `onChunk` as it lands.
 *
 * `onMetadata` fires exactly once, before the first chunk, carrying `degraded`
 * and the memory ids. That ordering is guaranteed by HTTP rather than by
 * convention: the backend finishes retrieval and composition before committing
 * a single response byte, and the facts travel as headers, which are flushed
 * ahead of the body by definition. See `lib/stream.ts`.
 *
 * Resolves with the complete reply text. A caller that ignores `onChunk` and
 * awaits only this promise has reintroduced the single-final-paint behaviour
 * this function exists to remove.
 */
export async function streamChat(
  request: ChatRequest,
  handlers: StreamHandlers = {},
  signal?: AbortSignal,
): Promise<string> {
  const body: ChatRequest = {
    message: request.message,
    subject_id: request.subject_id ?? null,
    actor_id: request.actor_id ?? null,
    stream: true,
    capture: request.capture ?? true,
  };

  let response: Response;
  try {
    response = await fetch(PROXY_PATH, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
  } catch (cause) {
    // An abort is the caller's own doing, not a transport failure; let it
    // propagate as an AbortError so the UI can tell the two apart.
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ChatError("Could not reach the app server.", {
      detail: cause instanceof Error ? cause.message : String(cause),
    });
  }

  if (!response.ok) {
    // Errors are JSON even on the streaming path: the proxy reshapes an
    // upstream failure into `{"detail": ...}` before any body is streamed.
    const text = await response.text();
    let payload: unknown;
    try {
      payload = JSON.parse(text);
    } catch {
      payload = undefined;
    }
    throw new ChatError(`The backend returned ${response.status}.`, {
      status: response.status,
      detail: extractDetail(payload) ?? (text ? text.slice(0, 500) : undefined),
    });
  }

  return readTokenStream(response, handlers, signal);
}
