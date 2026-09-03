/**
 * `GET /api/memories/export` -> backend `GET /memories/export`.
 *
 * The GDPR export (M7 step 10). Unlike the curated list this includes
 * soft-deleted rows, each marked `deleted: true` — a user is entitled to see
 * that a deletion happened.
 *
 * Same-origin hop, per project decision D13. See `lib/proxy.ts`.
 */

import { proxyJson } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  const incoming = new URL(request.url);
  const query = new URLSearchParams();
  for (const name of ["subject_id", "actor_id"]) {
    const value = incoming.searchParams.get(name);
    if (value) query.set(name, value);
  }
  const suffix = query.toString();
  return proxyJson(request, `/memories/export${suffix ? `?${suffix}` : ""}`);
}
