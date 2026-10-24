"use client";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import FinancialBlocks from "./FinancialBlocks";
import { Action, Resource, Session, Turn } from "./workspaceTypes";

type MessageRequest = { client_turn_id: string; text: string; workspace: true; action?: string; resource_selection?: Resource };
type PreferenceMessage = { client_event_id: string; text: string; accepted_at: string; status: string; proposals: { value: string; scope_key: string }[] };
export default function WorkspaceChat({ session, conversationId, hi, isCurrentSession, onExpired, onPreferences, externalAction, onBusyChange }: {
  session: Session; conversationId: string; hi: boolean; isCurrentSession: (s: Session) => boolean;
  onExpired: () => void; onPreferences: () => void; externalAction: (Action & { nonce: number }) | null; onBusyChange: (busy: boolean) => void;
}) {
  const [turns, setTurns] = useState<Turn[]>([]); const [preferences, setPreferences] = useState<PreferenceMessage[]>([]);
  const [text, setText] = useState(""); const [pending, setPending] = useState<MessageRequest | null>(null);
  const [sending, setSending] = useState(false); const [error, setError] = useState("");
  const mounted = useRef(true); const abort = useRef(new AbortController()); const busy = useRef(false);
  const pendingRef = useRef<MessageRequest | null>(null); const lastAction = useRef(0); const transcript = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  const base = `/bank-api/conversations/${conversationId}`; const storage = `bank-workspace-pending:${conversationId}`;
  const current = useCallback(() => mounted.current && isCurrentSession(session), [isCurrentSession, session]);
  const remember = useCallback((value: MessageRequest | null) => {
    pendingRef.current = value; setPending(value);
    try { if (value) sessionStorage.setItem(storage, JSON.stringify({ session: session.csrf_token, value })); else sessionStorage.removeItem(storage); } catch { /* In-memory retry remains available. */ }
  }, [storage, session.csrf_token]);
  const load = useCallback(async () => {
    const [r, p] = await Promise.all([fetch(base + "/turns", { cache: "no-store", signal: abort.current.signal }), fetch(base + "/preference-messages", { cache: "no-store", signal: abort.current.signal })]);
    if (!current()) return;
    if (r.status === 401 || p.status === 401) { onExpired(); return; }
    if (!r.ok || !p.ok) throw new Error(hi ? "बातचीत लोड नहीं हो सकी। फिर कोशिश करें।" : "Your conversation could not be loaded. Please try again.");
    const history: { items: Turn[] } = await r.json(); const pref: { items: PreferenceMessage[] } = await p.json();
    if (!current()) return;
    setTurns(history.items); setPreferences(pref.items);
    const id = pendingRef.current?.client_turn_id;
    if (id && (history.items.some(t => t.client_turn_id === id) || pref.items.some(t => t.client_event_id === id))) { remember(null); setError(""); }
    if (pref.items.some(t => t.status === "persisted" || t.status === "temporary")) onPreferences();
  }, [base, current, hi, onExpired, onPreferences, remember]);
  useEffect(() => {
    mounted.current = true; abort.current = new AbortController();
    try { const saved = JSON.parse(sessionStorage.getItem(storage) ?? "null"); if (saved?.session === session.csrf_token) remember(saved.value); } catch { /* Invalid recovery data is ignored. */ }
    void load().catch(e => { if (current() && e.name !== "AbortError") setError(e.message); });
    const timer = setInterval(() => void load().catch(e => { if (current() && e.name !== "AbortError") setError(e.message); }), 1800);
    return () => { mounted.current = false; clearInterval(timer); abort.current.abort(); };
  }, [load, current, remember, storage, session.csrf_token]);
  const inFlight = turns.some(t => t.status === "accepted" || t.status === "running") || preferences.some(t => t.status === "pending");
  const disabled = sending || !!pending || inFlight;
  useEffect(() => { onBusyChange(disabled); return () => onBusyChange(false); }, [disabled, onBusyChange]);
  const send = useCallback(async (request: MessageRequest, retry = false) => {
    if (busy.current || (!retry && (pendingRef.current || inFlight))) return;
    followLatest.current = true;
    busy.current = true; setSending(true); setError(""); remember(request);
    try {
      // An unknown acknowledgement keeps its original ID; reload before replay.
      if (retry) { await load(); if (!current() || !pendingRef.current) return; }
      const r = await fetch(base + "/messages", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token }, body: JSON.stringify(request), signal: abort.current.signal });
      if (!current()) return;
      if (r.status === 401) { onExpired(); return; }
      if (r.status >= 500) throw new Error("unknown");
      if (!r.ok) { remember(null); setError(r.status === 409 ? (hi ? "उत्पाद बदल गया है। रीफ़्रेश करके फिर चुनें।" : "That product or request changed. Refresh your products, then choose again.") : (hi ? "प्रश्न स्वीकार नहीं हुआ। विवरण जाँचें।" : "Your question wasn’t accepted. Please check the details.")); return; }
      await r.json(); remember(null); setText(""); await load();
    } catch (e) { if (current() && (e as Error).name !== "AbortError") setError(hi ? "परिणाम स्पष्ट नहीं है। उसी प्रश्न की स्थिति जाँचें।" : "The result is uncertain. Check or retry the original question below."); }
    finally { busy.current = false; if (current()) setSending(false); }
  }, [base, current, hi, inFlight, load, onExpired, remember, session.csrf_token]);
  const choose = useCallback((item: Action) => {
    const label = item.text || item.label;
    void send({ client_turn_id: crypto.randomUUID(), text: label, workspace: true, ...(item.action ? { action: item.action } : {}), ...(item.resource_selection ? { resource_selection: item.resource_selection } : {}) });
  }, [send]);
  useEffect(() => { if (externalAction && externalAction.nonce !== lastAction.current && !disabled) { lastAction.current = externalAction.nonce; choose(externalAction); } }, [externalAction, choose, disabled]);
  // Polling replaces the arrays even when their contents are unchanged. Follow
  // changed content only while the reader is already at the latest messages.
  const transcriptVersion = JSON.stringify([turns, preferences, sending]);
  useLayoutEffect(() => {
    if (followLatest.current && transcript.current) {
      transcript.current.scrollTo({ top: transcript.current.scrollHeight, behavior: "auto" });
    }
  }, [transcriptVersion]);
  const rows = [...turns.map(t => ({ time: t.accepted_at, turn: t, pref: null as PreferenceMessage | null })), ...preferences.map(p => ({ time: p.accepted_at, turn: null as Turn | null, pref: p }))].sort((a, b) => a.time.localeCompare(b.time));
  const quick: Action[] = [{ label: hi ? "मेरा बैलेंस" : "My balance", action: "balance" }, { label: hi ? "मेरे कार्ड" : "My cards", text: hi ? "मेरे कार्ड" : "List my cards", action: "products" }, { label: hi ? "ऋण भुगतान" : "Loan payments", action: "payments" }];
  return <section className="workspace-chat" aria-label={hi ? "बैंकिंग बातचीत" : "Banking conversation"}>
    <div className="chat-scroll" ref={transcript} role="log" aria-live="polite" aria-relevant="additions" onScroll={e => {
      const element = e.currentTarget;
      followLatest.current = element.scrollHeight - element.clientHeight - element.scrollTop <= 64;
    }}>
      <div className="welcome-message"><span className="assistant-mark" aria-hidden="true">✦</span><h1>{hi ? "आपकी बैंकिंग, आसान बातचीत में।" : "Your banking, in a simpler conversation."}</h1><p>{hi ? "नमस्ते! खाते, कार्ड या ऋण की जानकारी पूछें। उत्पाद चुनने के लिए उसके कार्ड पर टैप करें।" : "Hi! Ask about your accounts, cards or loans. You can also choose a product to get started."}</p><div className="quick-actions">{quick.map(a => <button key={a.label} disabled={disabled} onClick={() => choose(a)}>{a.label}<span aria-hidden="true"> ↗</span></button>)}</div></div>
      {rows.map(({ turn, pref }) => <div className="message-pair" key={turn?.turn_id ?? pref!.client_event_id}>
        <div className="user-bubble">{(turn?.user_text || turn?.text || pref?.text || "").includes("; cursor ") ? (hi ? "अगला पेज दिखाएँ" : "Show the next page") : (turn?.user_text || turn?.text || pref?.text || "").replace(/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/g, hi ? "चुना गया उत्पाद" : "selected product")}</div>
        <div className="assistant-message"><span className="assistant-avatar" aria-hidden="true">✦</span><div className="assistant-content">
          {pref ? <p role="status">{pref.status === "persisted" ? (hi ? "पसंद सेव हो गई है।" : "Your preference is saved.") : pref.status === "temporary" ? (hi ? "यह निर्देश केवल इस जवाब के लिए है।" : "This instruction applies to this response only.") : pref.status === "pending" ? (hi ? "पसंद सेव हो रही है…" : "Saving your preference…") : pref.status === "ignored_stale" ? (hi ? "नए निर्देश की पसंद लागू है।" : "A newer preference takes priority.") : (hi ? "पसंद सेव नहीं हो सकी।" : "Your preference could not be saved.")}</p> : turn?.result ? <>
            {turn.result.intro && <p>{turn.result.intro}</p>}
            {turn.result.blocks?.[0]?.kind !== "policy" && <p>{turn.result.text.replace(/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/g, hi ? "चुना गया उत्पाद" : "selected product")}</p>}
            <FinancialBlocks blocks={turn.result.blocks ?? []} hi={hi} />
            <div className="follow-up-actions">{turn.result.actions?.map((a, i) => <button key={i} disabled={disabled} onClick={() => choose(a)}>{a.label}</button>)}
            {turn.result.next_question && <button disabled={disabled} onClick={() => choose({ label: hi ? "अगला पेज" : "Load next page", text: turn.result!.next_question })}>{hi ? "अगला पेज" : "Load next page"}</button>}</div>
          </> : <p className="thinking" role="status">{turn?.status === "running" ? (hi ? "जानकारी देख रहे हैं…" : "Checking your information…") : (hi ? "प्रश्न मिल गया है…" : "Your question is queued…")}</p>}
        </div></div>
      </div>)}
    </div>
    <div className="composer-area">
      {error && <p className="workspace-error" role="alert">{error} <button onClick={() => void load().catch(() => {})}>{hi ? "स्थिति जाँचें" : "Check status"}</button></p>}
      {pending && <button className="retry-button" disabled={sending} onClick={() => void send(pending, true)}>{hi ? "उसी प्रश्न की स्थिति जाँचें / फिर भेजें" : "Check / retry original question"}</button>}
      <form className="workspace-composer" onSubmit={e => { e.preventDefault(); if (text.trim() && !disabled) void send({ client_turn_id: crypto.randomUUID(), text: text.trim(), workspace: true }); }}>
        <textarea aria-label={hi ? "आपका संदेश" : "Your message"} placeholder={hi ? "बैंकिंग के बारे में पूछें…" : "Ask about your banking…"} rows={1} maxLength={8000} value={text} disabled={disabled}
          onChange={e => { setText(e.target.value); e.target.style.height = "auto"; e.target.style.height = Math.min(e.target.scrollHeight, 160) + "px"; }}
          onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) { e.preventDefault(); if (text.trim() && !disabled) e.currentTarget.form?.requestSubmit(); } }} />
        <button type="submit" aria-label={hi ? "संदेश भेजें" : "Send message"} disabled={disabled || !text.trim()}>↑</button>
      </form><p className="composer-hint">{hi ? "Enter से भेजें · Shift+Enter से नई पंक्ति · केवल सिंथेटिक डेमो डेटा" : "Enter to send · Shift+Enter for a new line · Synthetic demo data only"}</p>
    </div>
  </section>;
}
