"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type Key = "preferred_response_language" | "preferred_response_format";
type Session = { customer_id: string; csrf_token: string };
type Conversation = { id: string };
type Choice = { key: Key; value: string | null; operation: string; scope_key: string };
type Proposal = { event_id: string; status: string; source_sequence: number; proposals: Choice[]; failure_code?: string };
type Setting = { value: string; scope: string; status: string };
type Snapshot = {
  effective: Record<Key, Setting>;
  projections: { key: Key; scope_key: string; value: string | null; tombstoned: boolean; version: number }[];
  proposals: Proposal[];
  rollout?: { new_writes_enabled: boolean };
};
type PendingRequest = { path: string; body: string; capture: boolean };

const label = (value: string | null) => ({ en: "English", hi: "Hindi", concise: "Concise", detailed: "Detailed" }[value ?? ""] ?? "Default");
const scopeLabel = (scope: string) => scope === "global" ? "All conversations" : scope.startsWith("conversation:") ? "This conversation" : scope.startsWith("response:") ? "This response only" : "Default";
const statusLabel = (status: string) => ({ pending: "Pending", persisted: "Saved", temporary: "This response only", ignored_stale: "Superseded", failed: "Failed", default: "Default" }[status] ?? status);

export default function PreferencePanel({ session, conversations, onExpired, isCurrentSession, activeConversation }: {
  session: Session; conversations: Conversation[]; onExpired: () => void; isCurrentSession?: (session: Session) => boolean; activeConversation?: string;
}) {
  const [conversation, setConversation] = useState(activeConversation ?? "");
  const [scope, setScope] = useState<"global" | "conversation">("global");
  const [language, setLanguage] = useState("en");
  const [format, setFormat] = useState("concise");
  const [text, setText] = useState("");
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState("");
  const [readError, setReadError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const epoch = useRef(0);
  const readSequence = useRef(0);
  const controller = useRef(new AbortController());
  const responsePreview = useRef<string | null>(null);
  const pending = useRef<PendingRequest | null>(null);
  const busyNow = useRef(false);
  const initialized = useRef("");
  const expectedVersions = useRef<Record<Key, number>>({ preferred_response_language: 0, preferred_response_format: 0 });
  const scopeKey = scope === "global" ? "global" : `conversation:${conversation}`;
  const current = useCallback((version: number) => version === epoch.current && (isCurrentSession?.(session) ?? true), [isCurrentSession, session.customer_id, session.csrf_token]);

  const refresh = useCallback(async () => {
    const version = epoch.current;
    const sequence = ++readSequence.current;
    const params = new URLSearchParams();
    if (conversation) params.set("conversation_id", conversation);
    if (responsePreview.current) params.set("response_event_id", responsePreview.current);
    try {
      const result = await fetch(`/bank-api/preferences?${params}`, { cache: "no-store", signal: controller.current.signal });
      if (!current(version) || sequence !== readSequence.current) return;
      if (result.status === 401) { onExpired(); return; }
      if (!result.ok) throw new Error("Settings could not be refreshed. Any displayed values are from the last successful check.");
      const next: Snapshot = await result.json();
      if (!current(version) || sequence !== readSequence.current) return;
      setSnapshot(next);
      setReadError("");
    } catch (cause) {
      if (current(version) && sequence === readSequence.current && cause instanceof Error && cause.name !== "AbortError") setReadError(cause.message);
    }
  }, [conversation, session.csrf_token, onExpired, current]);

  useEffect(() => {
    epoch.current++;
    controller.current = new AbortController();
    responsePreview.current = null;
    initialized.current = "";
    setSnapshot(null);
    setReadError("");
    void refresh();
    const timer = setInterval(() => { if (!busyNow.current) void refresh(); }, 1500);
    return () => { epoch.current++; controller.current.abort(); clearInterval(timer); };
  }, [refresh]);

  useEffect(() => {
    if (!snapshot || initialized.current === scopeKey) return;
    initialized.current = scopeKey;
    const stored = (key: Key, fallback: string) => snapshot.projections.find((p) => p.key === key && p.scope_key === scopeKey && !p.tombstoned)?.value ?? fallback;
    setLanguage(stored("preferred_response_language", "en"));
    setFormat(stored("preferred_response_format", "concise"));
    for (const key of ["preferred_response_language", "preferred_response_format"] as Key[]) {
      expectedVersions.current[key] = snapshot.projections.find((p) => p.key === key && p.scope_key === scopeKey)?.version ?? 0;
    }
  }, [snapshot, scopeKey]);

  async function submit(request: PendingRequest) {
    if (busyNow.current) return;
    if (snapshot?.rollout?.new_writes_enabled === false && pending.current !== request) {
      setError("Preference changes are temporarily unavailable. Your saved settings remain available.");
      return;
    }
    const version = epoch.current;
    busyNow.current = true;
    setBusy(true);
    pending.current = request;
    responsePreview.current = null;
    readSequence.current++;
    setSnapshot(null);
    setError("");
    setMessage("");
    try {
      const result = await fetch(request.path, {
        method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token },
        body: request.body, signal: controller.current.signal,
      });
      if (!current(version)) return;
      if (result.status === 401) { onExpired(); return; }
      if (result.status === 503) {
        const failure = await result.clone().json().catch(() => null);
        if (!current(version)) return;
        if (failure?.detail === "Preference writes unavailable") {
          pending.current = null;
          setChecking(false);
          setError("Preference changes are temporarily unavailable. Your saved settings remain available.");
          await refresh();
          return;
        }
      }
      if (result.status >= 500) throw new Error("The outcome is still being checked. Retry this same request to confirm it.");
      if (!result.ok) {
        pending.current = null;
        setChecking(false);
        initialized.current = "";
        setError(result.status === 409 ? "Settings changed before this edit was accepted. Review the refreshed values and try again." : "The preference request was rejected. Check the selection or instruction and try again.");
        await refresh();
        return;
      }
      const value: Proposal = await result.json();
      if (!current(version)) return;
      pending.current = null;
      setChecking(false);
      if (request.capture) responsePreview.current = value.event_id;
      else initialized.current = "";
      setMessage(value.status === "persisted" ? "Saved." : value.status === "temporary" ? "Applies to this response only." : value.status === "failed" ? "The instruction could not be applied. Your saved settings were kept." : value.status === "ignored_stale" ? "This older request was superseded. Current settings were kept." : "Request accepted. See its current processing status below.");
      await refresh();
    } catch (cause) {
      if (current(version) && cause instanceof Error && cause.name !== "AbortError") {
        setChecking(true);
        setError("The outcome is still being checked. Use Retry same request; a new request will not be created.");
        void refresh();
      }
    } finally {
      if (current(version)) { busyNow.current = false; setBusy(false); }
    }
  }

  function manual(key: Key, operation: "SET" | "DELETE") {
    if (disabled || pending.current) return;
    const expected = expectedVersions.current[key];
    void submit({ path: "/bank-api/preferences/manual", capture: false, body: JSON.stringify({
      client_event_id: crypto.randomUUID(), key, operation,
      value: operation === "DELETE" ? null : key === "preferred_response_language" ? language : format,
      scope, conversation_id: conversation || null, expected_version: expected,
    }) });
  }

  const disabled = busy || checking || !snapshot || !!readError || snapshot.rollout?.new_writes_enabled === false || (scope === "conversation" && !conversation);
  const effectiveLanguage = snapshot?.effective.preferred_response_language;
  const effectiveFormat = snapshot?.effective.preferred_response_format;
  return <section aria-labelledby="preference-heading" style={{ marginTop: "2rem", borderTop: "1px solid #bbb", paddingTop: "1rem" }}>
    <h2 id="preference-heading">Response preferences</h2>
    {snapshot?.rollout?.new_writes_enabled === false && <p role="status">Preference changes are temporarily unavailable. Your saved settings remain available.</p>}
    {!activeConversation && <label>Active conversation{" "}<select aria-label="Active conversation" value={conversation} disabled={busy || checking} onChange={(event) => {
      responsePreview.current = null;
      setSnapshot(null);
      setConversation(event.target.value);
      setMessage(""); setError("");
      if (!event.target.value) setScope("global");
    }}>
      <option value="">No conversation selected</option>
      {conversations.map((item, index) => <option key={item.id} value={item.id}>Conversation {index + 1}</option>)}
    </select></label>}
    {readError && <p role="alert">{readError}</p>}
    {error && <p role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    {checking && <button disabled={busy} onClick={() => { if (pending.current) void submit(pending.current); }}>Retry same request</button>}
    {snapshot ? <div aria-live="polite">
      <p><strong>Effective language:</strong> {label(effectiveLanguage?.value ?? null)} · {scopeLabel(effectiveLanguage?.scope ?? "default")} · {statusLabel(effectiveLanguage?.status ?? "default")}</p>
      <p><strong>Effective format:</strong> {label(effectiveFormat?.value ?? null)} · {scopeLabel(effectiveFormat?.scope ?? "default")} · {statusLabel(effectiveFormat?.status ?? "default")}</p>
      <p><strong>Settings preview</strong> (a sample, not a banking answer):</p>
      <p data-testid="preference-preview" lang={effectiveLanguage?.value === "hi" ? "hi" : "en"}>
        {effectiveLanguage?.value === "hi" ? (effectiveFormat?.value === "detailed" ? "नमस्ते। यह आपकी चुनी हुई हिंदी भाषा और विस्तृत प्रारूप का उदाहरण है।" : "नमस्ते। यह हिंदी में एक संक्षिप्त उदाहरण है।") : (effectiveFormat?.value === "detailed" ? "Hello. This sample uses your selected English language and detailed response format." : "Hello. This is a concise English sample.")}
      </p>
    </div> : !readError && <p role="status">Checking preferences…</p>}
    <fieldset disabled={disabled} style={{ margin: "1rem 0", padding: "1rem" }}>
      <legend>Saved settings</legend>
      <label>Apply to{" "}<select aria-label="Apply to" value={scope} onChange={(event) => { setScope(event.target.value as "global" | "conversation"); responsePreview.current = null; setSnapshot(null); void refresh(); }}>
        <option value="global">All conversations</option>
        <option value="conversation" disabled={!conversation}>This conversation</option>
      </select></label>
      <p><label>Language{" "}<select aria-label="Language" value={language} onChange={(event) => setLanguage(event.target.value)}><option value="en">English</option><option value="hi">Hindi</option></select></label>{" "}
        <button onClick={() => manual("preferred_response_language", "SET")}>Save language</button>{" "}<button onClick={() => manual("preferred_response_language", "DELETE")}>Reset language</button></p>
      <p><label>Format{" "}<select aria-label="Format" value={format} onChange={(event) => setFormat(event.target.value)}><option value="concise">Concise</option><option value="detailed">Detailed</option></select></label>{" "}
        <button onClick={() => manual("preferred_response_format", "SET")}>Save format</button>{" "}<button onClick={() => manual("preferred_response_format", "DELETE")}>Reset format</button></p>
      <p>Reset removes the setting in this scope. A conversation can then use the global setting; otherwise the defaults are English and concise.</p>
    </fieldset>
    <form onSubmit={(event) => {
      event.preventDefault();
      if (disabled || !text.trim() || pending.current) return;
      void submit({ path: "/bank-api/preferences/capture", capture: true, body: JSON.stringify({ client_event_id: crypto.randomUUID(), text, conversation_id: conversation || null }) });
    }}>
      <label htmlFor="preference-instruction">Preference instruction</label>
      <textarea id="preference-instruction" maxLength={1000} rows={2} value={text} onChange={(event) => setText(event.target.value)} disabled={disabled} style={{ display: "block", width: "100%", margin: ".5rem 0" }} />
      <button disabled={disabled || !text.trim()}>Apply instruction</button>
      <p>Supported examples: “Always reply in Hindi”, “Always be concise”, “Reply in English for this conversation”, “Reply in Hindi for this response”, and “Be detailed for this response”. Conversation and response instructions need an active conversation. Other wording may fail without changing your saved settings.</p>
    </form>
    {!!snapshot?.proposals.length && <div><h3>Recent preference requests</h3><ul>{snapshot.proposals.map((proposal) => <li key={proposal.event_id}>
      {proposal.proposals.length ? proposal.proposals.map((choice) => `${choice.operation === "DELETE" ? "Reset " : ""}${choice.key === "preferred_response_language" ? "Language" : "Format"}${choice.operation === "DELETE" ? "" : `: ${label(choice.value)}`} (${scopeLabel(choice.scope_key)})`).join(", ") : "Preference instruction"}
      {" — "}{statusLabel(proposal.status)}
      {proposal.status === "failed" && ". Saved settings were kept."}
    </li>)}</ul></div>}
  </section>;
}
