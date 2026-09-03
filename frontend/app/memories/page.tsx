"use client";

/**
 * `/memories` — the memory management panel (M6 steps 7, 8, 9, 11, 14).
 *
 * WIRED TO REAL ENDPOINTS. Plan step 10 offers a mock module for the case where
 * M7 has not landed; M7 HAS landed, so there is no mock and no feature flag
 * here — the panel talks to `GET /memories/me`, `PATCH /memories/{id}` and
 * `DELETE /memories/{id}` through this app's own proxy routes.
 *
 * WHY DELETE AND EDIT ARE OPTIMISTIC
 * ----------------------------------
 * `PATCH` re-embeds the new content, which is a call to a provider capped at 3
 * requests per minute — it can sit out a rate-limit window measured in tens of
 * seconds (`api/memories.py` documents this). Waiting for the round-trip before
 * updating the row would make every edit feel broken. So the UI applies the
 * change immediately and rolls back on failure, which `test_failed_delete_
 * restores_row` and `test_inline_edit_persists` both check.
 *
 * ONE DELIBERATE ASYMMETRY: a 404 on delete is treated as SUCCESS, not as a
 * failure to roll back. The row is gone — deleted in another tab, or by a
 * previous click whose response was lost — and restoring it would show the user
 * a memory that no longer exists.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ChatError } from "@/lib/api";
import {
  deleteMemory,
  listMemories,
  updateMemory,
  type Memory,
} from "@/lib/memories";

type LoadState = "loading" | "ready" | "error";

interface Notice {
  kind: "error" | "info";
  text: string;
}

/** Render an ISO timestamp without exploding on a null or a malformed one. */
function formatDate(iso: string | null): string {
  if (!iso) return "—";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatScore(value: number | null): string {
  return value === null || value === undefined ? "—" : value.toFixed(2);
}

export default function MemoriesPage() {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [total, setTotal] = useState(0);
  const [state, setState] = useState<LoadState>("loading");
  const [notice, setNotice] = useState<Notice | null>(null);

  /** Id of the row being edited, and the draft text for it. */
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  /** Ids with an in-flight mutation, so their controls can be disabled. */
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());

  const load = useCallback(async () => {
    setState("loading");
    try {
      const page = await listMemories({ limit: 100 });
      setMemories(page.memories);
      setTotal(page.total);
      setState("ready");
    } catch (cause) {
      setState("error");
      setNotice({
        kind: "error",
        text:
          cause instanceof ChatError
            ? [cause.message, cause.detail].filter(Boolean).join(" ")
            : String(cause),
      });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function markBusy(id: string, isBusy: boolean) {
    setBusy((current) => {
      const next = new Set(current);
      if (isBusy) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  async function handleDelete(memory: Memory) {
    // Optimistic: drop the row now, restore it if the call fails. No reload,
    // no refetch — `test_delete_removes_row_without_page_reload` counts
    // navigations and asserts none happened.
    const snapshot = memories;
    setNotice(null);
    setMemories((current) => current.filter((row) => row.id !== memory.id));
    setTotal((current) => Math.max(0, current - 1));
    markBusy(memory.id, true);

    try {
      await deleteMemory(memory.id);
    } catch (cause) {
      const status = cause instanceof ChatError ? cause.status : undefined;
      if (status === 404) {
        // Already gone. Keep it removed and say so plainly.
        setNotice({ kind: "info", text: "That memory had already been deleted." });
      } else {
        setMemories(snapshot);
        setTotal(snapshot.length);
        setNotice({
          kind: "error",
          text:
            cause instanceof ChatError
              ? [cause.message, cause.detail].filter(Boolean).join(" ")
              : String(cause),
        });
      }
    } finally {
      markBusy(memory.id, false);
    }
  }

  function startEdit(memory: Memory) {
    setNotice(null);
    setEditingId(memory.id);
    setDraft(memory.content);
  }

  function cancelEdit() {
    setEditingId(null);
    setDraft("");
  }

  async function handleSave(memory: Memory) {
    const content = draft.trim();
    if (!content) {
      setNotice({ kind: "error", text: "A memory cannot be empty." });
      return;
    }
    if (content === memory.content) {
      cancelEdit();
      return;
    }

    const snapshot = memories;
    setNotice(null);
    // Optimistic: show the new text immediately. The PATCH re-embeds, which can
    // block on the embedding provider's rate limit for tens of seconds.
    setMemories((current) =>
      current.map((row) => (row.id === memory.id ? { ...row, content } : row)),
    );
    cancelEdit();
    markBusy(memory.id, true);

    try {
      const updated = await updateMemory(memory.id, content);
      // Adopt the server's row: it carries the new `updated_at`, and the
      // backend is the authority on what was actually stored.
      setMemories((current) =>
        current.map((row) => (row.id === memory.id ? updated : row)),
      );
    } catch (cause) {
      setMemories(snapshot);
      setNotice({
        kind: "error",
        text:
          cause instanceof ChatError
            ? [cause.message, cause.detail].filter(Boolean).join(" ")
            : String(cause),
      });
    } finally {
      markBusy(memory.id, false);
    }
  }

  return (
    <main className="shell">
      <header className="masthead">
        <h1>Memories</h1>
        <p>
          Everything the assistant has stored for you.{" "}
          {state === "ready" ? `${total} ${total === 1 ? "memory" : "memories"}.` : ""}
        </p>
        <nav className="nav" aria-label="Sections">
          <Link href="/">← Back to chat</Link>
        </nav>
      </header>

      {/* Announced when it appears; kept outside the table so a row change
          does not re-announce the whole list. */}
      <div className="status">
        {notice ? (
          <div
            className={notice.kind === "error" ? "error" : "info"}
            role={notice.kind === "error" ? "alert" : "status"}
            data-testid="memories-notice"
          >
            {notice.text}
          </div>
        ) : null}
      </div>

      {state === "loading" ? (
        <p className="pending" role="status" data-testid="memories-loading">
          Loading memories…
        </p>
      ) : null}

      {state === "error" ? (
        <div className="empty" data-testid="memories-error">
          <p>Could not load your memories.</p>
          <button type="button" onClick={() => void load()}>
            Try again
          </button>
        </div>
      ) : null}

      {state === "ready" && memories.length === 0 ? (
        /* Step 11: an empty list must say so explicitly, never render blank. */
        <div className="empty" data-testid="memories-empty">
          <p>No memories yet.</p>
          <p>
            Have a conversation on the <Link href="/">chat page</Link> and the assistant will
            start remembering things. Capture runs after the reply, so a new memory takes a
            moment to appear.
          </p>
        </div>
      ) : null}

      {state === "ready" && memories.length > 0 ? (
        <ul className="memories" data-testid="memory-list">
          {memories.map((memory) => {
            const isEditing = editingId === memory.id;
            const isBusy = busy.has(memory.id);

            return (
              <li key={memory.id} className="memory" data-testid="memory-row">
                {isEditing ? (
                  <div className="memory__edit">
                    <label htmlFor={`edit-${memory.id}`}>Memory content</label>
                    <textarea
                      id={`edit-${memory.id}`}
                      value={draft}
                      rows={3}
                      onChange={(event) => setDraft(event.target.value)}
                      onKeyDown={(event) => {
                        // Keyboard-operable, per step 14. Escape cancels;
                        // Ctrl/Cmd+Enter saves without reaching for the mouse.
                        if (event.key === "Escape") cancelEdit();
                        if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                          void handleSave(memory);
                        }
                      }}
                      autoFocus
                      data-testid="memory-edit-input"
                    />
                    <div className="memory__actions">
                      <button
                        type="button"
                        onClick={() => void handleSave(memory)}
                        data-testid="memory-save"
                      >
                        Save
                      </button>
                      <button type="button" onClick={cancelEdit} data-testid="memory-cancel">
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <>
                    <p className="memory__content" data-testid="memory-content">
                      {memory.content}
                    </p>
                    <dl className="memory__meta">
                      <div>
                        <dt>Source</dt>
                        <dd data-testid="memory-source">{memory.source ?? "—"}</dd>
                      </div>
                      <div>
                        <dt>Importance</dt>
                        <dd>{formatScore(memory.importance)}</dd>
                      </div>
                      <div>
                        <dt>Created</dt>
                        <dd>
                          <time dateTime={memory.created_at ?? undefined}>
                            {formatDate(memory.created_at)}
                          </time>
                        </dd>
                      </div>
                    </dl>
                    <div className="memory__actions">
                      <button
                        type="button"
                        onClick={() => startEdit(memory)}
                        disabled={isBusy}
                        data-testid="memory-edit"
                        aria-label={`Edit memory: ${memory.content.slice(0, 40)}`}
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        className="danger"
                        onClick={() => void handleDelete(memory)}
                        disabled={isBusy}
                        data-testid="memory-delete"
                        aria-label={`Delete memory: ${memory.content.slice(0, 40)}`}
                      >
                        {isBusy ? "Deleting…" : "Delete"}
                      </button>
                    </div>
                  </>
                )}
              </li>
            );
          })}
        </ul>
      ) : null}
    </main>
  );
}
