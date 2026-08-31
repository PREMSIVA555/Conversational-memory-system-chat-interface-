"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";

import { ChatError, sendChat } from "@/lib/api";

interface Turn {
  id: string;
  role: "user" | "assistant";
  text: string;
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

export default function ChatPage() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [failure, setFailure] = useState<FailureView | null>(null);
  const [subjectId, setSubjectId] = useState<string | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);
  const transcriptEndRef = useRef<HTMLDivElement>(null);

  // A visible seconds counter while waiting. The completion provider is
  // rate-limited and the model thinks before emitting content, so a slow reply
  // is normal — the counter is how a human tells "slow" from "hung".
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
    setTurns((current) => [...current, { id: nextTurnId("user"), role: "user", text: message }]);
    setPending(true);

    try {
      const response = await sendChat({ message, subject_id: subjectId });
      // The backend mints a subject_id on the first turn; adopt it so every
      // later turn in this session lands under the same subject.
      setSubjectId(response.subject_id);
      setTurns((current) => [
        ...current,
        { id: nextTurnId("assistant"), role: "assistant", text: response.reply },
      ]);
    } catch (cause) {
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
          M2.5 thin checkpoint — a real turn against the live backend.
          {subjectId ? ` Subject ${subjectId.slice(0, 8)}…` : ""}
        </p>
      </header>

      {/*
        This container IS the live region, which is what makes the assistant's
        reply reach a screen reader: `role="log"` + `aria-live="polite"` means
        each <article> appended here is announced as it arrives, and
        `aria-relevant="additions"` stops it re-reading the whole transcript on
        every render. The user's own turn is announced too, which is normal for
        a chat log and confirms the message was sent.

        The waiting indicator and the failure alert are deliberately NOT in here
        — see the status region below.
      */}
      <div className="transcript" role="log" aria-live="polite" aria-relevant="additions">
        {turns.length === 0 && !pending && !failure ? (
          <p className="empty">Send a message to check the backend end to end.</p>
        ) : null}

        {turns.map((turn) => (
          <article
            key={turn.id}
            className={`turn turn--${turn.role}`}
            aria-label={turn.role === "user" ? "Your message" : "Assistant reply"}
          >
            <span className="turn__role">{turn.role === "user" ? "You" : "Assistant"}</span>
            <div className="turn__body">{turn.text}</div>
            {turn.role === "assistant" ? (
              <span className="turn__note">
                Reply delivered. Capture runs after the reply, so the memory row for this turn is
                probably not written yet.
              </span>
            ) : null}
          </article>
        ))}

        <div ref={transcriptEndRef} />
      </div>

      {/*
        Status region, kept out of the transcript log above. `role="status"` is
        present from first render (an empty live region is more reliable than one
        created on demand) and announces once when its text appears.

        The elapsed-seconds counter is `aria-hidden` and sits outside any live
        region on purpose: it changes every second, and inside a live region that
        would talk over the reply with an endless "1s", "2s", "3s".
      */}
      <div className="status">
        <p className="pending" role="status">
          {pending ? "Waiting for a reply." : ""}
        </p>

        {pending ? (
          <p className="pending pending--tick" aria-hidden="true">
            {elapsed}s elapsed — replies can take a while.
          </p>
        ) : null}

        {failure ? (
          <div className="error" role="alert">
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
          disabled={pending}
        />
        <button type="submit" disabled={pending || draft.trim() === ""}>
          {pending ? "Sending…" : "Send"}
        </button>
      </form>

      <p className="footnote">
        Capture is asynchronous by design: the backend answers first and writes the memory
        afterwards. The gap has been measured at anywhere from a few seconds to well over half a
        minute, so a memory you just created will not be readable back immediately.
      </p>
    </main>
  );
}
