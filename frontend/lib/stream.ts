/**
 * Reader over a streaming `POST /chat` response body.
 *
 * WHERE THE METADATA ACTUALLY LIVES — read this before "fixing" anything here.
 * ---------------------------------------------------------------------------
 * The plan (M6 step 3) describes "a reader that consumes the SSE/chunked
 * response and emits token chunks plus the leading metadata event (`degraded`,
 * `memory_ids`)". The backend does emit exactly that pair of facts first, but
 * **not inside the body**. From `api/chat.py`:
 *
 *     Body bytes are still plain UTF-8 text, deliberately:
 *     `frontend/app/api/chat/route.ts` pipes the body straight through, and
 *     switching to SSE would have broken it for no gain -- the metadata has a
 *     header to travel in.
 *
 * So the transport is: `text/plain; charset=utf-8`, no framing, no `data:`
 * prefixes, no JSON preamble line — just reply text — and `degraded` /
 * `memory_ids` ride out as `X-Memory-Degraded`, `X-Memory-Count`,
 * `X-Memory-Ids` and `X-Memory-Degraded-Reason` response headers, written by
 * `_metadata_headers()`.
 *
 * That is *stronger* than a leading body event, and the module docstring in
 * `api/chat.py` says why: headers are flushed before the first body byte by
 * definition, so "retrieval finished before the first token" is guaranteed by
 * HTTP itself rather than by the server remembering to yield metadata first.
 *
 * This module therefore keeps the plan's *interface* — `onMetadata` is always
 * invoked exactly once, before the first `onChunk` — while sourcing it from
 * headers. A future switch to SSE would change this file and nothing above it.
 *
 * PARSING NOTE: chunk boundaries fall wherever the network put them, which can
 * be mid-UTF-8-sequence. `TextDecoder` is used in streaming mode (`{ stream:
 * true }`) so a split multi-byte character is held back rather than decoded
 * into a replacement character.
 */

/** The leading facts about how the answer was built. */
export interface StreamMetadata {
  /** True when the memory layer was skipped — the answer used no memory. */
  degraded: boolean;
  /** Why it was skipped, when the backend said. */
  degradedReason?: string;
  /** Ids of the memories that reached the prompt. Empty when none matched. */
  memoryIds: string[];
  /**
   * `X-Memory-Count`, sent separately from the id list on purpose so a consumer
   * can tell "no memories" from "a proxy truncated the id header".
   */
  memoryCount: number;
  /** Identity the backend used, minted on the first turn when none was sent. */
  subjectId?: string;
  actorId?: string;
}

/** Callbacks a caller supplies. Both are optional; neither may throw. */
export interface StreamHandlers {
  /** Called once, before the first chunk. */
  onMetadata?: (metadata: StreamMetadata) => void;
  /** Called once per received chunk with the newly decoded text. */
  onChunk?: (text: string) => void;
}

/**
 * Pull `degraded` / `memory_ids` / identity out of a response's headers.
 *
 * Absent headers are not an error: `stream: false` responses and hand-written
 * test fixtures may omit them, and a missing `X-Memory-Degraded` means "we were
 * not told", which is reported as not-degraded rather than as a failure.
 */
export function readStreamMetadata(headers: Headers): StreamMetadata {
  const rawIds = headers.get("X-Memory-Ids") ?? "";
  const memoryIds = rawIds
    .split(",")
    .map((id) => id.trim())
    .filter((id) => id.length > 0);

  const rawCount = headers.get("X-Memory-Count");
  const parsedCount = rawCount === null ? Number.NaN : Number.parseInt(rawCount, 10);

  const metadata: StreamMetadata = {
    degraded: (headers.get("X-Memory-Degraded") ?? "").toLowerCase() === "true",
    memoryIds,
    // Trust the explicit count when it parses; otherwise fall back to what the
    // id list actually contained.
    memoryCount: Number.isFinite(parsedCount) ? parsedCount : memoryIds.length,
  };

  const reason = headers.get("X-Memory-Degraded-Reason");
  if (reason) metadata.degradedReason = reason;

  const subjectId = headers.get("X-Subject-Id");
  if (subjectId) metadata.subjectId = subjectId;

  const actorId = headers.get("X-Actor-Id");
  if (actorId) metadata.actorId = actorId;

  return metadata;
}

/**
 * Consume a streaming response, emitting each chunk as it lands.
 *
 * Resolves with the full concatenated text once the body ends. The return value
 * is a convenience for callers that also want the whole reply; it is NOT the
 * mechanism by which the UI renders — a caller that ignores `onChunk` and waits
 * for this promise has reintroduced exactly the single-final-paint bug M6
 * exists to remove.
 *
 * `signal` aborts the read; the underlying reader is cancelled so the upstream
 * socket is released rather than left draining into nothing.
 */
export async function readTokenStream(
  response: Response,
  handlers: StreamHandlers = {},
  signal?: AbortSignal,
): Promise<string> {
  handlers.onMetadata?.(readStreamMetadata(response.headers));

  const body = response.body;
  if (!body) {
    // No streaming body available (an older runtime, or a mocked Response built
    // from a string). Degrade to a single chunk rather than throwing — the text
    // is still correct, only the progressive render is lost.
    const text = await response.text();
    if (text) handlers.onChunk?.(text);
    return text;
  }

  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8");
  const parts: string[] = [];

  const abort = () => {
    void reader.cancel().catch(() => undefined);
  };
  signal?.addEventListener("abort", abort, { once: true });

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      // `stream: true` holds back a trailing partial code point until the next
      // chunk completes it.
      const text = decoder.decode(value, { stream: true });
      if (!text) continue;
      parts.push(text);
      handlers.onChunk?.(text);
    }

    // Flush whatever the decoder was holding at end of stream.
    const tail = decoder.decode();
    if (tail) {
      parts.push(tail);
      handlers.onChunk?.(tail);
    }
  } finally {
    signal?.removeEventListener("abort", abort);
    reader.releaseLock();
  }

  return parts.join("");
}
