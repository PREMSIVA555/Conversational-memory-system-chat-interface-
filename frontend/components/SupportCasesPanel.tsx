"use client";
import { useEffect, useRef, useState } from "react";
import { Session } from "./workspaceTypes";
export type CurrentCase = { id: string; case_number: string; status: string; created_at: string; status_updated_at: string; checked_at: string };
export const caseStatus = (value: string, hi: boolean) => ({ created: hi ? "प्राप्त" : "Received", in_review: hi ? "जाँच में" : "In review", resolved: hi ? "समाधान हो गया" : "Resolved" }[value] || (hi ? "स्थिति अज्ञात" : "Status unknown"));
export function CheckedCase({ value, hi }: { value: CurrentCase; hi: boolean }) {
  return <div className="support-current"><strong>{hi ? "वर्तमान स्थिति" : "Current status"}: {caseStatus(value.status, hi)}</strong><p>{value.case_number}</p><small>{hi ? "स्थिति बदली" : "Status updated"}: {new Date(value.status_updated_at).toLocaleString(hi ? "hi-IN" : "en-IN")}<br />{hi ? "जाँच का समय" : "Checked"}: {new Date(value.checked_at).toLocaleString(hi ? "hi-IN" : "en-IN")}</small></div>;
}
export default function SupportCasesPanel({ session, hi, isCurrentSession, onExpired }: { session: Session; hi: boolean; isCurrentSession: (s: Session) => boolean; onExpired: () => void }) {
  const [enabled, setEnabled] = useState(false), [items, setItems] = useState<CurrentCase[]>([]), [cursor, setCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<CurrentCase | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const alive = useRef(true), abort = useRef(new AbortController()), detailRequest = useRef(0), listRequest = useRef(0);
  const current = () => alive.current && isCurrentSession(session);
  const callbacks = useRef({ current, onExpired }); callbacks.current = { current, onExpired };
  async function load(next = false) {
    const serial = ++listRequest.current; setBusy(true); setError("");
    try {
      const r = await fetch("/bank-api/support/cases" + (next && cursor ? "?cursor=" + encodeURIComponent(cursor) : ""), { cache: "no-store", signal: abort.current.signal });
      if (!callbacks.current.current() || serial !== listRequest.current) return;
      if (r.status === 401) { callbacks.current.onExpired(); return; } if (!r.ok) throw new Error();
      const page = await r.json(); if (!callbacks.current.current() || serial !== listRequest.current) return;
      setItems(old => next ? [...old, ...page.items.filter((x: CurrentCase) => !old.some(y => y.id === x.id))] : page.items); setCursor(page.next_cursor);
      if (!next) { detailRequest.current++; setSelected(null); }
    } catch (e) { if (callbacks.current.current() && (e as Error).name !== "AbortError") setError(hi ? "सहायता अनुरोध अभी नहीं जाँचे जा सके। फिर रीफ़्रेश करें।" : "Support requests could not be checked. Refresh to try again."); }
    finally { if (callbacks.current.current() && serial === listRequest.current) setBusy(false); }
  }
  const loader = useRef(load); loader.current = load;
  useEffect(() => {
    alive.current = true; abort.current = new AbortController();
    void fetch("/bank-api/support/proposals/capabilities", { cache: "no-store", signal: abort.current.signal }).then(async r => {
      if (r.status === 401) { if (callbacks.current.current()) callbacks.current.onExpired(); return; }
      if (r.ok) { const c = await r.json(); if (callbacks.current.current() && c.continuity_enabled) { setEnabled(true); void loader.current(); } }
    }).catch(() => { /* Feature stays hidden until availability is established. */ });
    const changed = () => { if (callbacks.current.current()) void loader.current(); }; window.addEventListener("bank-support-created", changed);
    return () => { alive.current = false; abort.current.abort(); detailRequest.current++; listRequest.current++; window.removeEventListener("bank-support-created", changed); };
  }, [session.csrf_token]);
  async function detail(id: string) {
    const serial = ++detailRequest.current; setError("");
    try {
      const r = await fetch("/bank-api/support/cases/" + id, { cache: "no-store", signal: abort.current.signal });
      if (!callbacks.current.current() || serial !== detailRequest.current) return;
      if (r.status === 401) { callbacks.current.onExpired(); return; } if (!r.ok) throw new Error();
      const value = await r.json(); if (callbacks.current.current() && serial === detailRequest.current) setSelected(value);
    } catch (e) { if (callbacks.current.current() && (e as Error).name !== "AbortError") { setSelected(null); setError(hi ? "वर्तमान स्थिति उपलब्ध नहीं है। फिर जाँचें।" : "Current status is unavailable. Check again."); } }
  }
  if (!enabled) return null;
  return <section className="support-cases" aria-label={hi ? "मेरे सहायता अनुरोध" : "My support requests"}><h2>{hi ? "सहायता अनुरोध" : "Support requests"}</h2>
    <button disabled={busy} onClick={() => void load()}>{hi ? "सहायता अनुरोध रीफ़्रेश करें" : "Refresh support requests"}</button>
    {busy && <p role="status">{hi ? "अनुरोध जाँच रहे हैं…" : "Checking requests…"}</p>}{error && <p role="status">{error}</p>}
    {!busy && !error && !items.length && <p>{hi ? "अभी कोई सहायता अनुरोध नहीं है।" : "No support requests yet."}</p>}
    <ul>{items.map(item => <li key={item.id}><button onClick={() => void detail(item.id)}><span>{item.case_number}</span><small>{caseStatus(item.status, hi)} · {new Date(item.created_at).toLocaleDateString(hi ? "hi-IN" : "en-IN")}</small></button></li>)}</ul>
    {cursor && <button disabled={busy} onClick={() => void load(true)}>{hi ? "और अनुरोध" : "More requests"}</button>}
    {selected && <><CheckedCase value={selected} hi={hi} /><button onClick={() => void detail(selected.id)}>{hi ? "वर्तमान स्थिति फिर जाँचें" : "Check current status again"}</button></>}
  </section>;
}
