"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import PreferencePanel from "./PreferencePanel";
import WorkspaceChat from "./WorkspaceChat";
import SupportCasesPanel from "./SupportCasesPanel";
import "./workspace.css";
import { Action, Kind, ProductPage, Session, money, resource } from "./workspaceTypes";

const empty: ProductPage = { status: "loading", items: [], complete: false, next_cursor: null };
const kinds: Kind[] = ["account", "card", "loan"];
export default function BankingView() {
  const [session, setSession] = useState<Session | null>(null); const [cid, setCid] = useState("");
  const [hi, setHi] = useState(false); const [loading, setLoading] = useState(true); const [error, setError] = useState("");
  const [pages, setPages] = useState<Record<Kind, ProductPage>>({ account: empty, card: empty, loan: empty });
  const [productsOpen, setProductsOpen] = useState(false); const [action, setAction] = useState<(Action & { nonce: number }) | null>(null);
  const [chatBusy, setChatBusy] = useState(false); const [preparing, setPreparing] = useState(false); const [settingsKey, setSettingsKey] = useState(0);
  const active = useRef<Session | null>(null); const generation = useRef(0); const abort = useRef(new AbortController());
  const channel = useRef<BroadcastChannel | null>(null); const dialog = useRef<HTMLDialogElement>(null); const preparingRef = useRef(false);
  const isCurrentSession = useCallback((s: { customer_id: string; csrf_token: string }) => active.current?.customer_id === s.customer_id && active.current?.csrf_token === s.csrf_token, []);
  const clear = useCallback(() => {
    generation.current++; abort.current.abort(); abort.current = new AbortController(); active.current = null;
    setSession(null); setCid(""); setAction(null); setHi(false); setPages({ account: empty, card: empty, loan: empty }); dialog.current?.close();
    sessionStorage.clear(); localStorage.clear();
  }, []);
  const preferences = useCallback(async () => {
    const s = active.current; if (!s) return;
    try {
      const r = await fetch("/bank-api/preferences" + (cid ? "?conversation_id=" + cid : ""), { cache: "no-store", signal: abort.current.signal });
      if (!isCurrentSession(s)) return;
      if (r.status === 401) { clear(); return; }
      if (r.ok) { const p = await r.json(); if (isCurrentSession(s)) setHi(p.effective.preferred_response_language.value === "hi"); }
    } catch { /* Session refresh handles loss of authentication; keep the last language. */ }
  }, [cid, clear, isCurrentSession]);
  const loadProducts = useCallback(async (kind?: Kind, next = false) => {
    const s = active.current; if (!s) return;
    await Promise.all((kind ? [kind] : kinds).map(async k => {
      const cursor = next ? pages[k].next_cursor : null;
      try {
        const r = await fetch(`/bank-api/products/${k}${cursor ? "?cursor=" + encodeURIComponent(cursor) : ""}`, { cache: "no-store", signal: abort.current.signal });
        if (!isCurrentSession(s)) return;
        if (r.status === 401) { clear(); return; }
        if (!r.ok) throw new Error();
        const page: ProductPage = await r.json();
        if (isCurrentSession(s)) setPages(old => ({ ...old, [k]: { ...page, items: next ? [...old[k].items, ...page.items.filter(x => !old[k].items.some(y => y.id === x.id))] : page.items } }));
      } catch (e) { if (isCurrentSession(s) && (e as Error).name !== "AbortError") setPages(old => ({ ...old, [k]: { ...empty, status: "unavailable" } })); }
    }));
  }, [clear, isCurrentSession, pages]);
  const prepare = useCallback(async (fresh = false) => {
    const s = active.current; if (!s || preparingRef.current) return;
    preparingRef.current = true; setPreparing(true);
    try {
      const r = await fetch(fresh ? "/bank-api/workspace/new" : "/bank-api/workspace/prepare", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": s.csrf_token }, body: "{}", signal: abort.current.signal });
      if (!isCurrentSession(s)) return;
      if (r.status === 401) { clear(); return; }
      if (!r.ok) throw new Error();
      const value = await r.json(); if (isCurrentSession(s)) { setCid(value.conversation_id); setAction(null); setError(""); }
    } catch (e) { if (isCurrentSession(s) && (e as Error).name !== "AbortError") setError("Your workspace could not be prepared. Please try again."); }
    finally { preparingRef.current = false; if (isCurrentSession(s)) setPreparing(false); }
  }, [clear, isCurrentSession]);
  const refreshSession = useCallback(async () => {
    const version = generation.current;
    try {
      const r = await fetch("/bank-api/auth/session", { cache: "no-store", signal: abort.current.signal });
      if (version !== generation.current) return;
      if (r.status === 401) { clear(); return; }
      if (!r.ok) throw new Error();
      const s: Session = await r.json(); if (version !== generation.current) return;
      if (active.current && !isCurrentSession(s)) clear();
      active.current = s; setSession(old => old?.csrf_token === s.csrf_token && old.customer_id === s.customer_id ? old : s); setError("");
    } catch (e) { if (version === generation.current && (e as Error).name !== "AbortError") { clear(); setError("Sign-in services are unavailable. Please try again."); } }
    finally { setLoading(false); }
  }, [clear, isCurrentSession]);
  useEffect(() => {
    abort.current = new AbortController(); channel.current = new BroadcastChannel("bank-session"); channel.current.onmessage = () => { clear(); void refreshSession(); };
    void refreshSession(); const timer = setInterval(() => void refreshSession(), 15000);
    window.addEventListener("focus", refreshSession);
    return () => { clearInterval(timer); channel.current?.close(); abort.current.abort(); window.removeEventListener("focus", refreshSession); };
  }, [clear, refreshSession]);
  const firstLoad = useRef("");
  useEffect(() => { if (!session) { firstLoad.current = ""; return; } if (firstLoad.current === session.csrf_token) return; firstLoad.current = session.csrf_token; void loadProducts(); void prepare(); }, [session, loadProducts, prepare]);
  useEffect(() => { void preferences(); }, [preferences]);
  useEffect(() => { const focus = () => { if (active.current) { void loadProducts(); void preferences(); } }; window.addEventListener("focus", focus); return () => window.removeEventListener("focus", focus); }, [loadProducts, preferences]);
  async function logout() {
    const s = active.current; if (!s) return;
    try {
      const r = await fetch("/bank-api/auth/logout", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": s.csrf_token }, body: "{}" });
      if (!isCurrentSession(s)) return;
      if (!r.ok && r.status !== 401) { setError("Sign out could not be confirmed. Please try again."); return; }
      clear(); channel.current?.postMessage("changed");
    } catch { if (isCurrentSession(s)) setError("Sign out could not be confirmed. Please try again."); }
  }
  const names = hi ? { account: "खाते", card: "कार्ड", loan: "ऋण" } : { account: "Accounts", card: "Cards", loan: "Loans" };
  const trigger = (item: Action) => { setAction({ ...item, nonce: Date.now() }); setProductsOpen(false); };
  return <div className="bank-workspace" lang={hi ? "hi" : "en"}>
    <header className="bank-header"><a className="bank-brand" href="/" aria-label="Banking Assistant home"><span className="bank-logo" aria-hidden="true">b.</span><span>Banking Assistant<small>{hi ? "आपका निजी कार्यक्षेत्र" : "Your private workspace"}</small></span></a>
      {session && <nav aria-label="Workspace"><button className="header-button" disabled={chatBusy || preparing} onClick={() => void prepare(true)}>＋ {hi ? "नई बातचीत" : "New chat"}</button><button className="header-button" onClick={() => { setSettingsKey(x => x + 1); dialog.current?.showModal(); }}>{hi ? "सेटिंग्स" : "Settings"}</button><button className="header-button" onClick={() => void logout()}>{hi ? "साइन आउट" : "Sign out"}</button></nav>}
    </header>
    {error && <div role="alert" className="workspace-banner">{error}{session && !cid && <button onClick={() => void prepare()} disabled={preparing}>Try again</button>}</div>}
    {loading ? <main className="login-card"><span className="assistant-mark">✦</span><h1>Getting your workspace ready…</h1></main> : !session ? <main className="login-card"><span className="eyebrow">A CLEARER VIEW OF YOUR BANKING</span><h1>A little less searching.<br />A lot more clarity.</h1><p>Your accounts, cards and loans, with a helpful conversation to guide you.</p><a className="primary-link" href="/bank-api/auth/login">Sign in securely <span aria-hidden="true">↗</span></a><small>Synthetic demo banking · English and Hindi</small></main> : <main className="workspace-layout">
      <aside className={`products-panel ${productsOpen ? "products-open" : ""}`} aria-label={hi ? "मेरे उत्पाद" : "My products"}>
        <button className="products-mobile-toggle" aria-expanded={productsOpen} onClick={() => setProductsOpen(v => !v)}>{hi ? "मेरे उत्पाद" : "My products"}<span>{productsOpen ? "−" : "+"}</span></button>
        <div className="products-body"><div className="products-heading"><div><span className="eyebrow">{hi ? "आपकी जानकारी" : "AT A GLANCE"}</span><h2>{hi ? "मेरे उत्पाद" : "My products"}</h2></div><button className="refresh-products" aria-label={hi ? "उत्पाद रीफ़्रेश करें" : "Refresh products"} onClick={() => void loadProducts()}>↻</button></div>
          {kinds.map(kind => <section className="product-group" key={kind}><h3>{names[kind]} <span>{pages[kind].status === "ready" ? pages[kind].items.length : ""}</span></h3>
            {pages[kind].status === "loading" ? <p className="product-state">{hi ? "लोड हो रहा है…" : "Loading…"}</p> : pages[kind].status !== "ready" ? <p className="product-state">{hi ? "अभी उपलब्ध नहीं है।" : "Currently unavailable."}<button onClick={() => void loadProducts(kind)}>{hi ? "फिर कोशिश करें" : "Retry"}</button></p> : !pages[kind].items.length ? <p className="product-state">{hi ? "कोई उत्पाद नहीं मिला।" : "No products to show."}</p> : pages[kind].items.map(item => {
              const selected = resource(item, kind); const title = item.display_name; const code = kind === "account" ? "balance" : kind === "card" ? "card_details" : "loan_details";
              return <article className={`product-card product-${kind}`} key={item.id}>
                <div className="product-card-title"><span className="product-symbol" aria-hidden="true">{kind === "account" ? "▤" : kind === "card" ? "▰" : "⌂"}</span><h4>{title}</h4></div>
                {item.last_four && <p className="product-description">{item.kind} •••• {item.last_four} <span className="product-status">{item.status}</span></p>}
                <p className="product-value">{money(kind === "account" ? item.available_balance : kind === "card" ? item.available_credit ?? item.daily_limit : item.principal)}</p>
                <p className="product-value-label">{kind === "account" ? hi ? "उपलब्ध बैलेंस" : "Available balance" : kind === "card" ? item.available_credit ? hi ? "उपलब्ध क्रेडिट" : "Available credit" : hi ? "दैनिक सीमा" : "Daily limit" : hi ? "मूलधन" : "Principal"}</p>
                {kind === "loan" && <p className="product-description">{item.status} · {hi ? "मासिक" : "Monthly"} {money(item.monthly_payment)}</p>}
                <div className="product-actions"><button disabled={chatBusy || !cid} onClick={() => trigger({ label: kind === "account" ? hi ? "बैलेंस देखें" : "View balance" : kind === "card" ? hi ? "कार्ड की जानकारी" : "Limits & details" : hi ? "ऋण विवरण" : "Loan details", action: code, resource_selection: selected })}>{kind === "account" ? hi ? "बैलेंस" : "Balance" : hi ? "विवरण" : "Details"}</button>
                  <button disabled={chatBusy || !cid} onClick={() => trigger({ label: kind === "loan" ? hi ? "EMI अनुसूची" : "EMI schedule" : hi ? "लेनदेन" : "Transactions", action: kind === "loan" ? "schedule" : "transactions", resource_selection: selected })}>{kind === "loan" ? hi ? "EMI अनुसूची" : "EMI schedule" : hi ? "लेनदेन" : "Transactions"}</button>
                  {kind === "loan" && <button disabled={chatBusy || !cid} onClick={() => trigger({ label: hi ? "भुगतान रिकॉर्ड" : "Payment records", action: "payments", resource_selection: selected })}>{hi ? "भुगतान" : "Payments"}</button>}</div>
                <time className="product-asof">{hi ? "स्रोत" : "As of"} {new Date(item.source.as_of).toLocaleDateString(hi ? "hi-IN" : "en-IN")}</time>
              </article>;
            })}
            {pages[kind].next_cursor && <button className="more-products" onClick={() => void loadProducts(kind, true)}>{hi ? "और दिखाएँ" : "Show more"}</button>}
          </section>)}<p className="products-footnote">{hi ? "जानकारी केवल आपके अधिकृत उत्पादों की है।" : "Only products you’re authorized to view appear here."}</p>
          <SupportCasesPanel key={session.csrf_token} session={session} hi={hi} isCurrentSession={isCurrentSession} onExpired={clear} />
        </div>
      </aside>
      {cid ? <WorkspaceChat key={`${session.csrf_token}:${cid}`} session={session} conversationId={cid} hi={hi} isCurrentSession={isCurrentSession} onExpired={clear} onPreferences={preferences} externalAction={action} onBusyChange={setChatBusy} /> : <section className="workspace-chat workspace-preparing"><p>{hi ? "बातचीत तैयार हो रही है…" : "Preparing your conversation…"}</p></section>}
    </main>}
    <dialog className="settings-dialog" ref={dialog} onClose={() => void preferences()}><div className="settings-heading"><h2>{hi ? "आपकी पसंद" : "Your preferences"}</h2><button aria-label={hi ? "सेटिंग्स बंद करें" : "Close settings"} onClick={() => dialog.current?.close()}>×</button></div><p>{hi ? "भाषा और जवाब का विवरण अपने अनुसार रखें।" : "Choose your language and how much detail you prefer."}</p>{session && <PreferencePanel key={`${settingsKey}:${cid}`} session={session} activeConversation={cid} conversations={cid ? [{ id: cid }] : []} onExpired={clear} isCurrentSession={isCurrentSession} />}</dialog>
  </div>;
}
