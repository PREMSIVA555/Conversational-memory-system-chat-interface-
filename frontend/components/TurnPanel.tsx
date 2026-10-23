"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type Session = { customer_id: string; csrf_token: string };
type Turn = { turn_id: string; client_turn_id: string; conversation_id: string; status: string; last_event_id: number; accepted_at: string; text?: string; user_text?: string; result: { text: string; next_question?: string } | null };
type Request = { client_turn_id: string; text: string; instructions?: { language?: string; format?: string } };
type Pending = { session: string; request: Request };
const inProgress = (status: string) => status === "accepted" || status === "running";
const questionLabel = (text: string) => text.replace(/; page size \d+; cursor [A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/, " (next page)");
const statusLabel = (status: string) => ({ accepted: "Accepted · pending", running: "Checking your question…", completed: "Answer ready", clarify: "More information needed", unsupported: "Unsupported request", unavailable: "Enquiry unavailable", expired: "Request expired", no_evidence: "No matching information available", invalid_plan: "Unable to process this question", session_expired: "Session expired" }[status] ?? "Request failed");

export default function TurnPanel({ session, conversationId, active, onExpired, isCurrentSession }: { session: Session; conversationId: string; active: boolean; onExpired: () => void; isCurrentSession: (session: Session) => boolean }) {
  const [text, setText] = useState("");
  const [language, setLanguage] = useState("");
  const [format, setFormat] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [pending, setPending] = useState<Request | null>(null);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");
  const [historyError, setHistoryError] = useState("");
  const epoch = useRef(0);
  const controller = useRef(new AbortController());
  const pendingRef = useRef<Request | null>(null);
  const busyRef = useRef(false);
  const turnsRef = useRef<Turn[]>([]);
  const streams = useRef(new Map<string, EventSource>());
  const cursors = useRef(new Map<string, number>());
  const storageKey = `bank-pending-turn:${conversationId}`;
  const base = `/bank-api/conversations/${encodeURIComponent(conversationId)}/turns`;
  const current = useCallback((version: number) => version === epoch.current && isCurrentSession(session), [isCurrentSession, session.customer_id, session.csrf_token]);

  const remember = useCallback((request: Request | null) => {
    pendingRef.current = request;
    setPending(request);
    try {
      if (request) sessionStorage.setItem(storageKey, JSON.stringify({ session: session.csrf_token, request }));
      else sessionStorage.removeItem(storageKey);
    } catch { /* The in-memory identity is retained when browser storage is unavailable. */ }
  }, [session.csrf_token, storageKey]);

  const merge = useCallback((turn: Turn) => {
    if (!isCurrentSession(session) || turn.conversation_id !== conversationId) return;
    // Status is authoritative; an older stream delivery cannot roll it backwards.
    const old = turnsRef.current.find((item) => item.turn_id === turn.turn_id);
    if (old && old.last_event_id > turn.last_event_id) return;
    const next = (old ? turnsRef.current.map((item) => item.turn_id === turn.turn_id ? turn : item) : [...turnsRef.current, turn])
      .sort((a, b) => a.accepted_at.localeCompare(b.accepted_at) || a.turn_id.localeCompare(b.turn_id));
    turnsRef.current = next;
    setTurns(next);
    setError("");
    if (pendingRef.current?.client_turn_id === turn.client_turn_id) { remember(null); setChecking(false); setText(""); setLanguage(""); setFormat(""); }
    if (!inProgress(turn.status)) { streams.current.get(turn.turn_id)?.close(); streams.current.delete(turn.turn_id); }
  }, [conversationId, remember, isCurrentSession, session.customer_id, session.csrf_token]);

  const read = useCallback(async (path: string, version: number): Promise<Turn | null> => {
    const response = await fetch(path, { cache: "no-store", signal: controller.current.signal });
    if (!current(version)) return null;
    if (response.status === 401) { onExpired(); return null; }
    if (response.status === 404) return null;
    if (!response.ok) throw new Error("Status could not be checked. Please try again.");
    const turn: Turn = await response.json();
    if (!current(version)) return null;
    merge(turn);
    return turn;
  }, [merge, onExpired, current]);

  const load = useCallback(async () => {
    const version = epoch.current;
    const knownAtStart = new Set(turnsRef.current.map((turn) => turn.turn_id));
    try {
      const response = await fetch(base, { cache: "no-store", signal: controller.current.signal });
      if (!current(version)) return;
      if (response.status === 401) { onExpired(); return; }
      if (!response.ok) throw new Error(response.status === 503 ? "Account enquiries are temporarily unavailable." : "Conversation history could not be checked.");
      const history: { items: Turn[]; limit: number } = await response.json();
      if (!current(version)) return;
      history.items.forEach(merge);
      const visible = new Set(history.items.map((turn) => turn.turn_id));
      turnsRef.current = turnsRef.current.filter((turn) => visible.has(turn.turn_id) || !knownAtStart.has(turn.turn_id));
      setTurns(turnsRef.current);
      for (const [id, stream] of streams.current) {
        if (!turnsRef.current.some((turn) => turn.turn_id === id)) { stream.close(); streams.current.delete(id); }
      }
      setHistoryError("");
    } catch (cause) {
      if (current(version) && cause instanceof Error && cause.name !== "AbortError") setHistoryError(cause.message);
    }
  }, [base, merge, onExpired, current]);

  useEffect(() => {
    epoch.current++;
    controller.current = new AbortController();
    try {
      const saved: Pending | null = JSON.parse(sessionStorage.getItem(storageKey) ?? "null");
      if (saved?.session === session.csrf_token && saved.request?.client_turn_id && saved.request.text) { remember(saved.request); setChecking(true); }
      else sessionStorage.removeItem(storageKey);
    } catch { sessionStorage.removeItem(storageKey); }
    return () => { epoch.current++; controller.current.abort(); streams.current.forEach((stream) => stream.close()); streams.current.clear(); };
  }, [remember, session.csrf_token, storageKey]);

  useEffect(() => {
    setLanguage(""); setFormat("");
    if (!pendingRef.current) setText("");
    if (active) void load();
  }, [active, load]);

  useEffect(() => {
    const version = epoch.current;
    const tick = async () => {
      try {
        const request = pendingRef.current;
        if (request && !busyRef.current) await read(`${base}/by-client/${request.client_turn_id}`, version);
        if (!current(version)) return;
        for (const turn of turnsRef.current.filter((item) => inProgress(item.status))) {
          await read(`${base}/${turn.turn_id}`, version);
          if (!current(version)) return;
        }
      } catch (cause) {
        if (current(version) && cause instanceof Error && cause.name !== "AbortError") setError(cause.message);
      }
    };
    void tick();
    const timer = setInterval(() => void tick(), 2000);
    return () => clearInterval(timer);
  }, [base, read, current]);

  useEffect(() => {
    const version = epoch.current;
    for (const turn of turns) {
      if (!inProgress(turn.status) || streams.current.has(turn.turn_id)) continue;
      const cursor = cursors.current.get(turn.turn_id) ?? 0;
      const stream = new EventSource(`${base}/${turn.turn_id}/events?after_event_id=${cursor}`);
      streams.current.set(turn.turn_id, stream);
      const receive = (event: MessageEvent<string>) => {
        if (!current(version)) return;
        const id = Number(event.lastEventId);
        if (!Number.isSafeInteger(id) || id <= (cursors.current.get(turn.turn_id) ?? 0)) return;
        try {
          const value: { turn_id: string } = JSON.parse(event.data);
          if (value.turn_id !== turn.turn_id) return;
          cursors.current.set(turn.turn_id, id);
          void read(`${base}/${turn.turn_id}`, version).catch(() => { /* Polling retries. */ });
        } catch { /* GET status remains the recovery authority. */ }
      };
      for (const name of ["turn.accepted", "turn.running", "answer.ready", "turn.failed"]) stream.addEventListener(name, receive as EventListener);
      stream.onerror = () => {
        stream.close(); streams.current.delete(turn.turn_id);
        if (current(version)) void read(`${base}/${turn.turn_id}`, version).catch(() => { /* Polling retries. */ });
      };
    }
  }, [turns, base, conversationId, merge, read, current]);

  async function submit(request: Request) {
    if (busyRef.current) return;
    const version = epoch.current;
    busyRef.current = true; setBusy(true); setError(""); remember(request);
    try {
      // Resolve an unknown acknowledgement using its original identity before retrying.
      if (checking) {
        const found = await read(`${base}/by-client/${request.client_turn_id}`, version);
        if (!current(version) || found) return;
      }
      const response = await fetch(base, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token }, body: JSON.stringify(request), signal: controller.current.signal });
      if (!current(version)) return;
      if (response.status === 401) { onExpired(); return; }
      if (response.status >= 500) throw new Error("Acceptance could not be confirmed. Check status or retry the same question.");
      if (!response.ok) {
        remember(null); setChecking(false);
        setError(response.status === 409 ? "This request conflicts with an earlier question. Check its status before sending another." : "The question was not accepted. Check your input and session, then try again.");
        return;
      }
      const turn: Turn = await response.json();
      if (!current(version)) return;
      merge(turn);
    } catch (cause) {
      if (current(version) && cause instanceof Error && cause.name !== "AbortError") { setChecking(true); setError("Acceptance could not be confirmed. Check status or retry the same question. Account enquiries may be temporarily unavailable."); }
    } finally { if (current(version)) { busyRef.current = false; setBusy(false); } }
  }

  return <section hidden={!active} aria-label="Conversation questions" style={{ marginTop: "1.5rem" }}>
    <h2>Account, card and loan enquiries</h2>
    <p>Ask about balances, card limits, existing loans, EMI schedules, actual payments or synthetic credit-card fee policies. Choose the full resource reference for account, card and loan enquiries. Policy rules do not establish personal waiver eligibility.</p>
    <button onClick={() => void load()}>Refresh conversation</button>
    {historyError && <p role="alert">{historyError}</p>}
    <ol aria-label="Conversation history" style={{ paddingLeft: "1.5rem" }}>{turns.map((turn) => <li key={turn.turn_id} style={{ marginBottom: "1rem" }}>
      {(turn.user_text || turn.text) && <p><strong>You:</strong> {questionLabel(turn.user_text || turn.text || "")}</p>}
      <p role="status">{statusLabel(turn.status)}</p>
      {!inProgress(turn.status) && turn.result?.text && <p style={{ whiteSpace: "pre-wrap" }}>{turn.result.text}</p>}
      {!inProgress(turn.status) && turn.result?.next_question && <button disabled={busy || !!pending} onClick={() => void submit({ client_turn_id: crypto.randomUUID(), text: turn.result!.next_question! })}>Load next page</button>}
    </li>)}</ol>
    {error && <p role="alert">{error}</p>}
    {checking && <p role="status">Checking whether your question was accepted. The original question is kept for recovery.</p>}
    {pending && <p>Pending question: {questionLabel(pending.text)}</p>}
    <form onSubmit={(event) => { event.preventDefault(); if (!text.trim() || pendingRef.current || busyRef.current) return; void submit({ client_turn_id: crypto.randomUUID(), text: text.trim(), ...(language || format ? { instructions: { ...(language ? { language } : {}), ...(format ? { format } : {}) } } : {}) }); }}>
      <label htmlFor={`question-${conversationId}`}>Your question</label>
      <textarea id={`question-${conversationId}`} maxLength={8000} rows={3} value={text} disabled={busy || !!pending} onChange={(event) => setText(event.target.value)} style={{ display: "block", width: "100%", margin: ".5rem 0" }} />
      <fieldset disabled={busy || !!pending} style={{ marginBottom: "1rem" }}><legend>This answer only</legend>
        <label>Answer language{" "}<select value={language} onChange={(event) => setLanguage(event.target.value)}><option value="">Use saved preference</option><option value="en">English</option><option value="hi">Hindi</option></select></label>{" "}
        <label>Answer format{" "}<select value={format} onChange={(event) => setFormat(event.target.value)}><option value="">Use saved preference</option><option value="concise">Concise</option><option value="detailed">Detailed</option></select></label>
      </fieldset>
      <button disabled={busy || !!pending || !text.trim()}>Send question</button>
    </form>
    {pending && <p><button disabled={busy} onClick={() => void submit(pending)}>Check status / retry same question</button></p>}
  </section>;
}
