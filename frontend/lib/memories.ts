/**
 * Typed client for the memory-management endpoints (M6 step 2).
 *
 * Shapes mirror `api/memories.py:serialize_memory` and `api/governance.py`
 * exactly. Do not drift from those files.
 *
 * Every call goes to this app's OWN `/api/memories*` routes, never to the
 * backend origin — the backend mounts no CORS middleware, so a direct browser
 * call would be refused. See `lib/proxy.ts` and project decision D13.
 *
 * `sendChat` lives in `lib/api.ts` and streaming lives in `lib/stream.ts`; this
 * module is deliberately separate so the memory panel does not drag the chat
 * transport into its bundle.
 */

import { ChatError } from "@/lib/api";

/**
 * One row of `GET /memories/me`.
 *
 * `serialize_memory` is called there WITHOUT `include_deleted_marker`, so the
 * curated view carries no `deleted` field — it only ever returns live rows. The
 * GDPR export adds it, which is why `ExportedMemory` extends this rather than
 * this type carrying an always-false flag the panel might start filtering on.
 */
export interface Memory {
  id: string;
  subject_id: string;
  actor_id: string;
  content: string;
  source: string | null;
  importance: number | null;
  confidence: number | null;
  weight: number | null;
  reinforcement_count: number | null;
  created_at: string | null;
  updated_at: string | null;
  last_accessed_at: string | null;
}

/** A row of `GET /memories/export`, which includes soft-deleted memories. */
export interface ExportedMemory extends Memory {
  deleted: boolean;
  deleted_at: string | null;
}

/** A page of memories plus the pagination facts, which ride in headers. */
export interface MemoryPage {
  memories: Memory[];
  /** `X-Total-Count`: live memories for this subject, ignoring the page. */
  total: number;
  limit: number;
  offset: number;
}

/** Identity to act as. Omitted entirely when the page has not learned one yet. */
export interface Identity {
  subjectId?: string | null;
  actorId?: string | null;
}

function identityHeaders(identity: Identity | undefined): HeadersInit {
  const headers: Record<string, string> = {};
  if (identity?.subjectId) headers["X-Subject-Id"] = identity.subjectId;
  if (identity?.actorId) headers["X-Actor-Id"] = identity.actorId;
  return headers;
}

/**
 * Turn a non-OK response into a `ChatError` carrying the backend's own detail.
 *
 * The status is preserved because callers act on it: the panel treats a 404 on
 * delete as "already gone" (and leaves the row removed) rather than as a
 * failure to roll back.
 */
async function fail(response: Response, fallback: string): Promise<never> {
  let detail: string | undefined;
  try {
    const payload: unknown = await response.json();
    const raw = (payload as { detail?: unknown })?.detail;
    if (typeof raw === "string") detail = raw;
    else if (raw !== undefined) detail = JSON.stringify(raw);
  } catch {
    // A non-JSON error body is not itself an error worth reporting over the
    // status; fall through with no detail.
  }
  throw new ChatError(fallback, { status: response.status, detail });
}

async function request(path: string, init: RequestInit): Promise<Response> {
  try {
    return await fetch(path, { ...init, cache: "no-store" });
  } catch (cause) {
    throw new ChatError("Could not reach the app server.", {
      detail: cause instanceof Error ? cause.message : String(cause),
    });
  }
}

/** List the caller's live memories, newest first. */
export async function listMemories(
  options: { limit?: number; offset?: number; identity?: Identity } = {},
): Promise<MemoryPage> {
  const query = new URLSearchParams();
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  if (options.offset !== undefined) query.set("offset", String(options.offset));
  const suffix = query.toString();

  const response = await request(`/api/memories${suffix ? `?${suffix}` : ""}`, {
    method: "GET",
    headers: identityHeaders(options.identity),
  });
  if (!response.ok) await fail(response, "Could not load memories.");

  const memories = (await response.json()) as Memory[];
  if (!Array.isArray(memories)) {
    throw new ChatError("The memory list came back in an unexpected shape.");
  }

  // Pagination rides in headers so the body stays a bare array. A missing
  // header is not an error — fall back to what the page itself contained.
  const header = (name: string, fallback: number): number => {
    const raw = response.headers.get(name);
    const parsed = raw === null ? Number.NaN : Number.parseInt(raw, 10);
    return Number.isFinite(parsed) ? parsed : fallback;
  };

  return {
    memories,
    total: header("X-Total-Count", memories.length),
    limit: header("X-Limit", options.limit ?? memories.length),
    offset: header("X-Offset", options.offset ?? 0),
  };
}

/** Replace a memory's content. The backend re-embeds it and audits the change. */
export async function updateMemory(
  id: string,
  content: string,
  identity?: Identity,
): Promise<Memory> {
  const response = await request(`/api/memories/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...identityHeaders(identity) },
    body: JSON.stringify({ content }),
  });
  if (!response.ok) await fail(response, "Could not save that edit.");
  return (await response.json()) as Memory;
}

/**
 * Soft-delete a memory.
 *
 * Never a hard delete: the row stays and `deleted_at` is stamped, so every read
 * path filters it out while the GDPR export still shows that it happened.
 */
export async function deleteMemory(id: string, identity?: Identity): Promise<void> {
  const response = await request(`/api/memories/${encodeURIComponent(id)}`, {
    method: "DELETE",
    headers: identityHeaders(identity),
  });
  if (!response.ok) await fail(response, "Could not delete that memory.");
}

/** The full GDPR export, soft-deleted rows included and marked. */
export async function exportMemories(identity?: Identity): Promise<unknown> {
  const response = await request("/api/memories/export", {
    method: "GET",
    headers: identityHeaders(identity),
  });
  if (!response.ok) await fail(response, "Could not export memories.");
  return response.json();
}
