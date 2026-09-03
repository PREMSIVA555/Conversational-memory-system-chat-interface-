/**
 * `GET /api/memories` -> backend `GET /memories/me`.
 *
 * Same-origin hop, per project decision D13. See `lib/proxy.ts`.
 */

import { proxyJson } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  const incoming = new URL(request.url);
  const query = new URLSearchParams();

  // Only forward the parameters the backend declares. Passing the caller's
  // whole query string through would let a page smuggle arbitrary parameters
  // at the backend, and `limit`/`offset` are validated there (ge=1, le=200).
  for (const name of ["limit", "offset", "subject_id", "actor_id"]) {
    const value = incoming.searchParams.get(name);
    if (value) query.set(name, value);
  }

  const suffix = query.toString();
  return proxyJson(request, `/memories/me${suffix ? `?${suffix}` : ""}`);
}
