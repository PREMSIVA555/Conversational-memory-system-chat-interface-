"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import PreferencePanel from "./PreferencePanel";
import TurnPanel from "./TurnPanel";

type Session = { customer_id: string; csrf_token: string; expires_at: number };
type Conversation = { id: string; created_at: string };

export default function BankingView() {
  const [session, setSession] = useState<Session | null>(null);
  const [items, setItems] = useState<Conversation[]>([]);
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  const [secure, setSecure] = useState(true);
  const [loading, setLoading] = useState(true);
  const generation = useRef(0);
  const active = useRef<Session | null>(null);
  const abort = useRef(new AbortController());
  const channel = useRef<BroadcastChannel | null>(null);
  const isCurrentSession = useCallback((candidate: { customer_id: string; csrf_token: string }) => active.current?.customer_id === candidate.customer_id && active.current?.csrf_token === candidate.csrf_token, []);

  const clear = useCallback(() => {
    generation.current++;
    abort.current.abort();
    abort.current = new AbortController();
    active.current = null;
    setSession(null);
    setItems([]);
    setSelected("");
    // The previous memory UI stored a client-selected subject. It is not authority.
    localStorage.clear();
    sessionStorage.clear();
  }, []);

  const refresh = useCallback(async () => {
    const version = generation.current;
    try {
      const response = await fetch("/bank-api/auth/session", { cache: "no-store", signal: abort.current.signal });
      if (version !== generation.current) return;
      if (response.status === 401) { clear(); setError(""); return; }
      if (!response.ok) throw new Error("Sign-in services are unavailable. Please try again.");
      const next: Session = await response.json();
      if (version !== generation.current) return;
      if (active.current && (active.current.customer_id !== next.customer_id || active.current.csrf_token !== next.csrf_token)) clear();
      active.current = next;
      setSession(next);
      setError("");
    } catch (cause) {
      if (version === generation.current && cause instanceof Error && cause.name !== "AbortError") { clear(); setError(cause.message); }
    } finally { if (version === generation.current || !active.current) setLoading(false); }
  }, [clear]);

  useEffect(() => {
    abort.current = new AbortController();
    setSecure(location.protocol === "https:");
    channel.current = new BroadcastChannel("bank-session");
    channel.current.onmessage = () => { clear(); void refresh(); };
    const timer = setInterval(() => void refresh(), 15000);
    window.addEventListener("focus", refresh);
    void refresh();
    return () => { clearInterval(timer); window.removeEventListener("focus", refresh); channel.current?.close(); abort.current.abort(); };
  }, [clear, refresh]);

  useEffect(() => {
    if (!session) return;
    const version = generation.current;
    void fetch("/bank-api/conversations", { cache: "no-store", signal: abort.current.signal }).then(async (response) => {
      if (version !== generation.current) return;
      if (response.status === 401) { clear(); return; }
      if (!response.ok) return;
      const conversations: Conversation[] = await response.json();
      if (version === generation.current) setItems((old) => [...conversations, ...old.filter((item) => !conversations.some((stored) => stored.id === item.id))]);
    }).catch(() => { /* Session refresh handles lost access. */ });
  }, [session?.customer_id, session?.csrf_token, clear]);

  async function action(kind: "create" | "logout") {
    const current = active.current;
    if (!current) return;
    const version = generation.current;
    try {
      const response = await fetch(kind === "logout" ? "/bank-api/auth/logout" : "/bank-api/conversations", {
        method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": current.csrf_token },
        body: "{}", signal: abort.current.signal,
      });
      if (version !== generation.current) return;
      if (response.status === 401) { clear(); return; }
      if (!response.ok) throw new Error(kind === "logout" ? "Logout could not be confirmed. Please retry." : "The request could not be completed.");
      if (kind === "logout") { clear(); channel.current?.postMessage("changed"); return; }
      const item: Conversation = await response.json();
      if (version === generation.current) { setItems((old) => [...old, item]); setSelected(item.id); }
    } catch (cause) {
      if (version === generation.current && cause instanceof Error && cause.name !== "AbortError") setError(cause.message);
    }
  }

  return <main style={{ maxWidth: 720, margin: "4rem auto", padding: "1.5rem" }}>
    <h1>Banking Assistant</h1>
    <p>Sign in to your private workspace.</p>
    {!secure && <p>Use the <a href="https://localhost:3443">secure local site</a> to sign in.</p>}
    {error && <p role="alert">{error}</p>}
    {loading ? <p role="status">Checking your session…</p> : session ? <>
      <p>Signed in</p>
      <button onClick={() => void action("logout")}>Sign out</button>{" "}
      <button onClick={() => void action("create")}>Create conversation</button>
      {!items.length && <p>Create a conversation to ask an account question.</p>}
      {!!items.length && <p><label>Conversation{" "}<select aria-label="Conversation" value={selected} onChange={(event) => setSelected(event.target.value)}>
        <option value="">Choose a conversation</option>
        {items.map((item, index) => <option key={item.id} value={item.id}>Conversation {index + 1}</option>)}
      </select></label></p>}
      {items.filter((item) => item.id === selected).map((item) => <TurnPanel key={`${session.customer_id}:${session.csrf_token}:${item.id}`} session={session} conversationId={item.id} active onExpired={clear} isCurrentSession={isCurrentSession} />)}
      <PreferencePanel key={`${session.customer_id}:${session.csrf_token}`} session={session} conversations={items} onExpired={clear} isCurrentSession={isCurrentSession} />
    </> : secure && <a href="/bank-api/auth/login">Sign in</a>}
  </main>;
}
