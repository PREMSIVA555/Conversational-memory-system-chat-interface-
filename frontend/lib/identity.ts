/**
 * Where the browser remembers who it is.
 *
 * THE GAP THIS CLOSES
 * -------------------
 * The backend mints a `subject_id` on the first chat turn and returns it as a
 * response header. M2.5's chat page kept it in React state, which was fine when
 * the chat WAS the whole app. M6 adds `/memories`, a separate route with its own
 * component tree and no shared state — so the panel had no way to learn which
 * subject to ask about, and `GET /memories/me` with no identity answers:
 *
 *     400 {"detail": "subject_id is required (query parameter, X-Subject-Id
 *          header, or DEFAULT_SUBJECT_ID in the environment)"}
 *
 * The panel would have shown an error for a backend behaving exactly correctly.
 *
 * WHY localStorage AND NOT A CONTEXT PROVIDER
 * -------------------------------------------
 * A provider would share the id between the two routes but lose it on reload,
 * and losing it means the user's memories become unreachable while still
 * existing in the database — the identity IS the key to them. This is a
 * single-user, self-service system, so persisting per-browser is both adequate
 * and closest to what the id actually means.
 *
 * NOT AUTHENTICATION, and nothing here pretends otherwise. The backend's own
 * `resolve_identity` says the same about its environment fallback. Anyone can
 * put any uuid in this key; the security seam is RLS on the server, which is
 * what M1 built and what would matter the moment this stopped being single-user.
 */

const STORAGE_KEY = "memory-system.subject-id";

/** A uuid, loosely — enough to reject junk left in storage by something else. */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * The remembered subject, or null.
 *
 * Returns null rather than throwing when storage is unavailable. Private
 * browsing, disabled site data, and SSR all make `localStorage` inaccessible —
 * and none of those is an error worth showing a user, because the app works
 * fine without a remembered id: the next chat turn simply mints a new one.
 */
export function readSubjectId(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw && UUID.test(raw) ? raw : null;
  } catch {
    return null;
  }
}

/** Remember the subject the backend gave us. Silently a no-op if it cannot. */
export function writeSubjectId(subjectId: string): void {
  if (typeof window === "undefined") return;
  if (!UUID.test(subjectId)) return;
  try {
    window.localStorage.setItem(STORAGE_KEY, subjectId);
  } catch {
    // Storage full, or blocked. The session still works; it just will not
    // survive a reload, which is a degradation and not a failure.
  }
}

/** Forget the current subject — the next turn starts a fresh one. */
export function clearSubjectId(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // nothing to do
  }
}
