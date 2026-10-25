"use client";
import { useEffect, useRef, useState } from "react";
import { Session } from "./workspaceTypes";
import { CheckedCase, CurrentCase } from "./SupportCasesPanel";
type Request = { proposal_id: string; operation_key: string; version: number; payload_hash: string };
type Outcome = { outcome: string; accepted: boolean; request: Request | null; proposal_status: string; creation_receipt: { case_number: string; created_at: string } | null; current_case: CurrentCase | null };
export default function SupportSubmission({ id, turnId, matchesSaved, session, hi, isCurrentSession, onExpired, onFreeze, onConsumed }: {
  id: string; turnId: string; matchesSaved: boolean; session: Session; hi: boolean; isCurrentSession: (s: Session) => boolean; onExpired: () => void; onFreeze: (v: boolean) => void; onConsumed: () => void;
}) {
  const [outcome, setOutcome] = useState<Outcome | null>(null), [checking, setChecking] = useState(true), [submitting, setSubmitting] = useState(false), [attempted, setAttempted] = useState(false), [error, setError] = useState("");
  const [needsCheck, setNeedsCheck] = useState(false);
  const alive = useRef(true), abort = useRef(new AbortController()), request = useRef<Request | null>(null), attemptedRef = useRef(false), working = useRef(false), serial = useRef(0);
  const knownOutcome = useRef<Outcome | null>(null);
  const storage = `bank-support-confirm:${turnId}`;
  const props = useRef({ isCurrentSession, onExpired, onFreeze, onConsumed }); props.current = { isCurrentSession, onExpired, onFreeze, onConsumed };
  const current = () => alive.current && props.current.isCurrentSession(session);
  const say = (en: string, hindi: string) => hi ? hindi : en;
  function remember(value: Request) {
    request.current = value; attemptedRef.current = true; setAttempted(true); props.current.onFreeze(true);
    try { sessionStorage.setItem(storage, JSON.stringify({ session: session.csrf_token, value })); } catch { /* Server intent and stable prepared key survive refresh without browser storage. */ }
  }
  async function lookup() {
    const number = ++serial.current; setChecking(true); setError("");
    try {
      const known = request.current && attemptedRef.current ? request.current : null;
      const r = await fetch(`/bank-api/support/proposals/${id}/${known ? "reconcile" : "outcome"}`, { method: known ? "POST" : "GET", cache: "no-store", signal: abort.current.signal,
        ...(known ? { headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token }, body: JSON.stringify({ operation_key: known.operation_key, version: known.version, payload_hash: known.payload_hash }) } : {}) });
      if (!current() || number !== serial.current) return;
      if (r.status === 401) { props.current.onExpired(); return; }
      if (r.status === 409 && known) { request.current = null; attemptedRef.current = false; await lookup(); return; }
      if (!r.ok) throw new Error();
      const value: Outcome = await r.json(); if (!current() || number !== serial.current) return;
      knownOutcome.current = value; setOutcome(value); setNeedsCheck(false);
      if (value.request && (value.accepted || !attemptedRef.current)) request.current = value.request;
      if (value.accepted && value.request) remember(value.request);
      props.current.onFreeze(value.outcome !== "not_recorded" || value.accepted || attemptedRef.current);
      if (value.outcome === "committed") props.current.onConsumed();
    } catch (e) { if (current() && (e as Error).name !== "AbortError") { props.current.onFreeze(true); setNeedsCheck(true); setError(knownOutcome.current?.outcome === "committed" ? say("Your request is recorded, but its current status could not be refreshed.", "आपका अनुरोध दर्ज है, लेकिन वर्तमान स्थिति रीफ़्रेश नहीं हो सकी।") : say("The submission could not be checked. Its outcome is still unknown; check again.", "जमा की स्थिति नहीं जाँची जा सकी। परिणाम अभी अज्ञात है; फिर जाँचें।")); } }
    finally { if (current() && number === serial.current) setChecking(false); }
  }
  useEffect(() => {
    alive.current = true; abort.current = new AbortController(); props.current.onFreeze(true);
    try { const saved = JSON.parse(sessionStorage.getItem(storage) || "null"); if (saved?.session === session.csrf_token && saved.value?.proposal_id === id) remember(saved.value); } catch { /* Server lookup remains authoritative. */ }
    void lookup(); return () => { alive.current = false; serial.current++; abort.current.abort(); };
  }, [id, session.csrf_token]);
  async function submit() {
    if (working.current || checking || needsCheck || !request.current || (!attemptedRef.current && !matchesSaved) || outcome?.outcome !== "not_recorded") return;
    working.current = true; setSubmitting(true); setError(""); serial.current++;
    const original = request.current; remember(original);
    try {
      const r = await fetch(`/bank-api/support/proposals/${id}/confirm`, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token }, signal: abort.current.signal,
        body: JSON.stringify({ operation_key: original.operation_key, version: original.version, payload_hash: original.payload_hash }) });
      if (!current()) return;
      if (r.status === 401) { props.current.onExpired(); return; }
      if (r.ok) { const receipt = await r.json(); if (!current()) return; if (receipt.result === "case.created") {
        const known: Outcome = { outcome: "committed", accepted: true, request: original, proposal_status: "consumed", creation_receipt: receipt, current_case: null };
        knownOutcome.current = known; setOutcome(known); props.current.onConsumed(); window.dispatchEvent(new Event("bank-support-created"));
      } }
      await lookup();
    } catch (e) { if (current() && (e as Error).name !== "AbortError") { setNeedsCheck(true); setError(say("Submission outcome is unknown. Check the original submission before retrying.", "जमा का परिणाम अज्ञात है। फिर कोशिश करने से पहले मूल जमा की जाँच करें।")); } }
    finally { working.current = false; if (current()) setSubmitting(false); }
  }
  const busy = checking || submitting;
  return <div className="support-reconciliation" aria-label={say("Submission status", "जमा की स्थिति")}>
    {checking && <p role="status">{say("Checking submission…", "जमा की जाँच हो रही है…")}</p>}{submitting && <p role="status">{say("Submitting saved request…", "सेव किया गया अनुरोध जमा हो रहा है…")}</p>}{error && <p role="status">{error}</p>}
    {outcome?.outcome === "not_recorded" && <>{!needsCheck && <p>{attempted ? say("No committed submission is recorded yet. You can check again or explicitly retry the original request.", "अभी कोई जमा दर्ज नहीं है। फिर जाँचें या उसी मूल अनुरोध से दोबारा कोशिश करें।") : say("Review the saved description above before submitting.", "जमा करने से पहले ऊपर सेव किया गया विवरण जाँचें।")}</p>}
      {!matchesSaved && !attempted && <p>{say("Save your changes before submitting.", "जमा करने से पहले बदलाव सेव करें।")}</p>}
      <button disabled={busy || needsCheck || (!attempted && !matchesSaved)} onClick={() => void submit()}>{attempted ? say("Retry original submission", "मूल जमा की दोबारा कोशिश करें") : say("Submit support request", "सहायता अनुरोध जमा करें")}</button></>}
    {outcome?.outcome === "not_submitted" && <p>{say("This preview was not submitted and can no longer be used.", "यह पूर्वावलोकन जमा नहीं हुआ और अब उपयोग नहीं हो सकता।")}</p>}
    {outcome?.outcome === "committed" && <div className="support-receipt"><strong>{say("Support request recorded", "सहायता अनुरोध दर्ज है")}</strong>
      {outcome.creation_receipt ? <><p>{outcome.creation_receipt.case_number}</p><small>{say("Creation receipt", "निर्माण रसीद")}: {new Date(outcome.creation_receipt.created_at).toLocaleString(hi ? "hi-IN" : "en-IN")}</small></> : <p>{say("The creation receipt retention window has ended. The current case record is shown below.", "निर्माण रसीद की अवधि समाप्त हो गई है। वर्तमान केस रिकॉर्ड नीचे है।")}</p>}</div>}
    {outcome?.current_case && <CheckedCase value={outcome.current_case} hi={hi} />}
    <button disabled={busy} onClick={() => void lookup()}>{say("Check submission status", "जमा की स्थिति जाँचें")}</button>
  </div>;
}
