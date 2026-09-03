"use client";

/**
 * The chat view (M6 steps 4, 5, 6, 14).
 *
 * WHAT CHANGED FROM M2.5, AND WHY IT IS THE POINT OF THIS MILESTONE
 * -----------------------------------------------------------------
 * M2.5 called `sendChat`, which awaits `response.text()` — the whole body — and
 * then appended one finished assistant turn. The reply appeared in a single
 * paint no matter how long the model took.
 *
 * This calls `streamChat`, which appends an EMPTY assistant turn first and then
 * grows it one chunk at a time. `test_streamed_tokens_render_incrementally`
 * samples that element's text length while the response is in flight and
 * asserts it increased across at least two samples, so a buffered
 * implementation fails the milestone even though it renders the same final
 * text.
 *
 * THE LIVE REGION
 * ---------------
 * The transcript container IS the live region, and the growing assistant text
 * lives INSIDE it. M2.5 failed its verification once for rendering the reply
 * next to the announced region rather than within it, so this is load-bearing
 * rather than decorative.
 *
 * `aria-relevant="additions"` keeps a screen reader from re-reading the whole
 * transcript on every render — which matters far more here than it did in
 * M2.5, because a streamed reply re-renders on every single chunk.
 */

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { ChatError, streamChat } from "@/lib/api";
import type { StreamMetadata } from "@/lib/stream";

interface Turn {
  id: string;
  role: "user" | "assistant";
  text: string;
  /** Assistant turns only: whether the answer was built without memory. */
  degraded?: boolean;
  degradedReason?: string;
  /** Assistant turns only: how many memories reached the prompt. */
  memoryCount?: number;
  /** True while this turn is still receiving chunks. */
  streaming?: boolean;
}

interface FailureView {
  message: string;
  detail?: string;
}

let turnCounter = 0;
function nextTurnId(role: Turn["role"]): string {
  turnCounter += 1;
  return `${role}-${turnCounter}`;
}

export default function ChatView() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [failure, setFailure] = useState<FailureView | null>(null);
  const [subjectId, setSubjectId] = useState<string | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);
  const transcriptEndRef = useRef<HTMLDivElement>(null);

  // A visible seconds counter while waiting. The completion provider is
  // rate-limited and the model reasons before emitting content, so a slow first
  // token is normal — the counter is how a human tells "slow" from "hung".
  useEffect(() => {
    if (!pending) {
      setElapsed(0);
      return;
    }
    const startedAt = Date.now();
    const handle = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(handle);
  }, [pending]);

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, pending, failure]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    const message = draft.trim();
    if (!message || pending) return;

    setFailure(null);
    setDraft("");
    setPending(true);

    const assistantId = nextTurnId("assistant");
    setTurns((current) => [
      ...current,
      { id: nextTurnId("user"), role: "user", text: message },
      // The empty assistant turn is added UP FRONT so there is an element to
      // grow. Adding it on the first chunk instead would work, but it would
      // also mean nothing is on screen during the wait for the first token,
      // which on this backend can be several seconds.
      { id: assistantId, role: "assistant", text: "", streaming: true },
    ]);

    const patch = (change: Partial<Turn>) =>
      setTurns((current) =>
        current.map((turn) => (turn.id === assistantId ? { ...turn, ...change } : turn)),
      );

    try {
      await streamChat(
        { message, subject_id: subjectId },
        {
          onMetadata: (metadata: StreamMetadata) => {
            // Fires once, before the first chunk. The backend mints a
            // subject_id on the first turn; adopt it so every later turn in
            // this session lands under the same subject.
            if (metadata.subjectId) setSubjectId(metadata.subjectId);
            patch({
              degraded: metadata.degraded,
              degradedReason: metadata.degradedReason,
              memoryCount: metadata.memoryCount,
            });
          },
          onChunk: (text: string) => {
            // Append, never replace. This is the incremental render.
            setTurns((current) =>
              current.map((turn) =>
                turn.id === assistantId ? { ...turn, text: turn.text + text } : turn,
              ),
            );
          },
        },
      );
      patch({ streaming: false });
    } catch (cause) {
      // Drop the empty assistant turn: leaving a blank bubble above an error
      // reads as "the assistant replied with nothing".
      setTurns((current) =>
        current.filter((turn) => !(turn.id === assistantId && turn.text === "")),
      );
      patch({ streaming: false });

      if (cause instanceof ChatError) {
        setFailure({ message: cause.message, detail: cause.detail });
      } else {
        setFailure({
          message: "Something went wrong sending that message.",
          detail: cause instanceof Error ? cause.message : String(cause),
        });
      }
    } finally {
      setPending(false);
      inputRef.current?.focus();
    }
  }

  return (
    <main className="shell">
      <header className="masthead">
        <h1>memory-system</h1>
        <p>
          Streamed replies with memory in context.
          {subjectId ? ` Subject ${subjectId.slice(0, 8)}…` : ""}
        </p>
        <nav className="nav" aria-label="Sections">
          <Link href="/memories">Manage memories →</Link>
        </nav>
      </header>

      {/*
        This container IS the live region. `role="log"` + `aria-live="polite"`
        announces each <article> as it arrives; `aria-relevant="additions"`
        stops the whole transcript being re-read on every chunk.

        The waiting indicator and the failure alert stay OUT of here — see the
        status region below.
      */}
      <div className="transcript" role="log" aria-live="polite" aria-relevant="additions">
        {turns.length === 0 && !pending && !failure ? (
          <p className="empty">Send a message to see the answer stream in.</p>
        ) : null}

        {turns.map((turn) => (
          <article
            key={turn.id}
            className={`turn turn--${turn.role}`}
            aria-label={turn.role === "user" ? "Your message" : "Assistant reply"}
            data-testid={turn.role === "assistant" ? "assistant-turn" : "user-turn"}
            data-streaming={turn.streaming ? "true" : "false"}
          >
            <span className="turn__role">{turn.role === "user" ? "You" : "Assistant"}</span>

            {/*
              The text node the e2e sampler measures. It must be the growing
              one, and it must be inside the live region above.
            */}
            <div className="turn__body" data-testid={turn.role === "assistant" ? "assistant-text" : undefined}>
              {turn.text}
              {turn.streaming ? <span className="caret" aria-hidden="true" /> : null}
            </div>

            {turn.role === "assistant" && turn.degraded ? (
              /*
                Step 6. This is NOT an error state and must not read like one:
                the answer is real, it was simply built without memory. It fires
                often in practice — the embedding provider is capped at 3
                requests a minute, so the retrieval path frequently times out and
                M5's breaker serves the turn anyway.
              */
              <p className="degraded" data-testid="degraded-indicator">
                <strong>Answering without memory.</strong>{" "}
                {turn.degradedReason
                  ? `The memory layer was skipped (${turn.degradedReason}).`
                  : "The memory layer was skipped for this turn."}
              </p>
            ) : null}

            {turn.role === "assistant" && !turn.degraded && turn.memoryCount !== undefined ? (
              <span className="turn__note" data-testid="memory-count">
                {turn.memoryCount === 0
                  ? "No stored memories matched this question."
                  : `${turn.memoryCount} ${turn.memoryCount === 1 ? "memory" : "memories"} used.`}
              </span>
            ) : null}
          </article>
        ))}

        <div ref={transcriptEndRef} />
      </div>

      {/*
        Status region, kept out of the transcript log. `role="status"` is present
        from first render — an empty live region is announced more reliably than
        one created on demand.

        The elapsed counter is aria-hidden and outside any live region: it
        changes every second and would otherwise talk over the streaming reply.
      */}
      <div className="status">
        <p className="pending" role="status">
          {pending ? "Waiting for a reply." : ""}
        </p>

        {pending ? (
          <p className="pending pending--tick" aria-hidden="true">
            {elapsed}s elapsed — the first token can take a while.
          </p>
        ) : null}

        {failure ? (
          <div className="error" role="alert" data-testid="chat-error">
            <strong>Could not get a reply.</strong>
            {failure.message}
            {failure.detail ? (
              <>
                {" "}
                <code>{failure.detail}</code>
              </>
            ) : null}
          </div>
        ) : null}
      </div>

      <form className="composer" onSubmit={handleSubmit}>
        <label htmlFor="message">Your message</label>
        <input
          id="message"
          name="message"
          ref={inputRef}
          type="text"
          autoComplete="off"
          placeholder="Ask something…"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          /* Step: the send control is disabled while a response is in flight. */
          disabled={pending}
          data-testid="chat-input"
        />
        <button type="submit" disabled={pending || draft.trim() === ""} data-testid="chat-send">
          {pending ? "Streaming…" : "Send"}
        </button>
      </form>

      <p className="footnote">
        Capture is asynchronous by design: the backend answers first and writes the memory
        afterwards, and only once the stream completes — so a half-received turn is never stored as
        if it were the whole reply. A memory you just created will not be readable back immediately.
      </p>
    </main>
  );
}
