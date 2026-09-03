/**
 * `PATCH` / `DELETE /api/memories/:id` -> backend `/memories/{memory_id}`.
 *
 * Same-origin hop, per project decision D13. See `lib/proxy.ts`.
 *
 * NOTE on the sibling route: `app/api/memories/export/route.ts` sits beside
 * this dynamic segment. Next resolves static segments before dynamic ones, so
 * `/api/memories/export` reaches the export handler and never arrives here with
 * `id === "export"`.
 */

import { NextResponse } from "next/server";

import { proxyJson } from "@/lib/proxy";

export const dynamic = "force-dynamic";

/** Next 15 hands route params in as a promise. */
type Context = { params: Promise<{ id: string }> };

/**
 * The backend answers 422 for a non-uuid id (FastAPI path validation), which is
 * a fine but noisy way to learn the id was malformed. Rejecting it here keeps a
 * typo from costing a network round-trip and gives a clearer message.
 */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function badId(id: string): Response | null {
  return UUID.test(id)
    ? null
    : NextResponse.json({ detail: `"${id}" is not a valid memory id.` }, { status: 400 });
}

export async function PATCH(request: Request, context: Context): Promise<Response> {
  const { id } = await context.params;
  const invalid = badId(id);
  if (invalid) return invalid;

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ detail: "Request body was not valid JSON." }, { status: 400 });
  }

  const content = (body as { content?: unknown })?.content;
  if (typeof content !== "string" || content.trim() === "") {
    // Mirrors `MemoryPatch.content` (min_length=1) in api/memories.py. Checked
    // here as well so an empty edit never reaches the backend, where it would
    // otherwise be rejected only after an ownership lookup.
    return NextResponse.json(
      { detail: "A non-empty `content` is required." },
      { status: 400 },
    );
  }

  return proxyJson(request, `/memories/${id}`, { method: "PATCH", body: { content } });
}

export async function DELETE(request: Request, context: Context): Promise<Response> {
  const { id } = await context.params;
  const invalid = badId(id);
  if (invalid) return invalid;

  return proxyJson(request, `/memories/${id}`, { method: "DELETE" });
}
