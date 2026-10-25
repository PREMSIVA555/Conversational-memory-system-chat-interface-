"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Kind, Product, ProductPage, Resource, Session } from "./workspaceTypes";

type Draft = { source_turn_id: string; topic: "general" | Kind; resource_id: string | null; summary: string };
type Preview = { id: string; version: number; payload_hash: string; payload: Draft; resource_label: string | null; expires_at: string; status: string };
type Selected = Resource & { label?: string };
const base = "/bank-api/support/proposals";
const productLabel = (p: Product) => p.display_name + (p.last_four ? ` · ${p.kind} •••• ${p.last_four}` : "");

export default function SupportPanel({ turnId, session, isCurrentSession, onExpired, selected, hi }: {
  turnId: string; session: Session; isCurrentSession: (s: Session) => boolean; onExpired: () => void; selected?: Selected | null; hi: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [summary, setSummary] = useState("");
  const [topic, setTopic] = useState<Draft["topic"]>(selected?.kind ?? "general");
  const [resource, setResource] = useState(selected?.id ?? "");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [products, setProducts] = useState<Product[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadingProducts, setLoadingProducts] = useState(false);
  const [productError, setProductError] = useState(false);
  const alive = useRef(true), working = useRef(false);
  const controller = useRef(new AbortController());
  const productController = useRef(new AbortController());
  const currentPreview = useRef<Preview | null>(null);
  const panel = useRef<HTMLDivElement>(null);
  const current = useCallback(() => alive.current && isCurrentSession(session), [isCurrentSession, session]);
  const say = useCallback((en: string, hindi: string) => hi ? hindi : en, [hi]);
  const remember = useCallback((value: Preview | null) => { currentPreview.current = value; setPreview(value); }, []);
  useEffect(() => {
    alive.current = true; controller.current = new AbortController();
    return () => { alive.current = false; controller.current.abort(); productController.current.abort(); };
  }, []);

  const read = useCallback(async (hydrate = true) => {
    if (working.current) return;
    working.current = true; if (hydrate) setBusy(true);
    try {
      const response = await fetch(`${base}/by-turn/${turnId}`, { cache: "no-store", signal: controller.current.signal });
      if (!current()) return;
      if (response.status === 401) { onExpired(); return; }
      if (response.status === 404) {
        if (currentPreview.current) {
          remember(null); setSummary(""); setUnavailable(true);
          setMessage(say("This preview is no longer available. Start from a new question.", "यह पूर्वावलोकन अब उपलब्ध नहीं है। नया प्रश्न पूछें।"));
        } else if (hydrate) setMessage(say("Describe the issue below to save a preview.", "पूर्वावलोकन सेव करने के लिए समस्या नीचे लिखें।"));
        if (hydrate) setUncertain(false);
        return;
      }
      if (!response.ok) throw new Error("read");
      const next: Preview = await response.json(); if (!current()) return;
      if (!hydrate && currentPreview.current && next.id !== currentPreview.current.id) {
        setUncertain(true); setMessage(say("The saved preview changed. Refresh it before editing.", "सेव किया गया पूर्वावलोकन बदल गया है। संपादन से पहले रीफ़्रेश करें।")); return;
      }
      remember(next); setUnavailable(false);
      if (hydrate) {
        setSummary(next.payload.summary); setTopic(next.payload.topic); setResource(next.payload.resource_id ?? "");
        setUncertain(false); setMessage("");
      }
    } catch (e) {
      if (current() && (e as Error).name !== "AbortError" && hydrate) setMessage(say("The saved state could not be checked. Try refreshing it.", "सेव की गई स्थिति नहीं जाँची जा सकी। फिर रीफ़्रेश करें।"));
    } finally { working.current = false; if (current() && hydrate) setBusy(false); }
  }, [current, onExpired, remember, say, turnId]);

  // Server time decides expiry. Background reads do not replace unsaved text
  // or silently adopt a concurrent editor's new version/hash binding.
  useEffect(() => {
    if (!open) return;
    panel.current?.scrollIntoView({ block: "nearest" });
    const timer = setInterval(() => void read(false), 5000);
    return () => clearInterval(timer);
  }, [open, read]);

  const loadProducts = useCallback(async (next = false) => {
    if (topic === "general") return;
    productController.current.abort(); const abort = new AbortController(); productController.current = abort;
    setLoadingProducts(true); setProductError(false);
    try {
      const response = await fetch(`/bank-api/products/${topic}${next && cursor ? "?cursor=" + encodeURIComponent(cursor) : ""}`, { cache: "no-store", signal: abort.signal });
      if (!current() || abort.signal.aborted) return;
      if (response.status === 401) { onExpired(); return; }
      if (!response.ok) throw new Error("products");
      const page: ProductPage = await response.json(); if (!current() || abort.signal.aborted) return;
      if (page.status === "unavailable" || page.status === "disabled") throw new Error("products");
      setProducts(old => next ? [...old, ...page.items.filter(p => !old.some(q => q.id === p.id))] : page.items);
      setCursor(page.next_cursor);
    } catch (e) { if (current() && !abort.signal.aborted && (e as Error).name !== "AbortError") setProductError(true); }
    finally { if (current() && !abort.signal.aborted) setLoadingProducts(false); }
  }, [current, cursor, onExpired, topic]);
  const productLoader = useRef(loadProducts); productLoader.current = loadProducts;
  useEffect(() => {
    productController.current.abort(); setProducts([]); setCursor(null); setProductError(false); setLoadingProducts(false);
    if (open && topic !== "general") void productLoader.current();
    return () => productController.current.abort();
  }, [open, topic]);

  async function mutate(kind: "save" | "cancel") {
    if (working.current) return;
    working.current = true; setBusy(true); setMessage("");
    try {
      const draft: Draft = { source_turn_id: turnId, topic, resource_id: topic === "general" ? null : resource, summary };
      const binding = preview ? { version: preview.version, payload_hash: preview.payload_hash } : {};
      const path = kind === "cancel" ? `${base}/${preview!.id}/cancel` : preview ? `${base}/${preview.id}/edit` : base;
      const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token },
        body: JSON.stringify(kind === "cancel" ? binding : preview ? { ...binding, draft } : draft), signal: controller.current.signal });
      if (!current()) return;
      if (response.status === 401) { onExpired(); return; }
      if (!response.ok) {
        if (response.status >= 500 || response.status === 409 || response.status === 404) setUncertain(true);
        setMessage(say("The preview could not be updated. Refresh its saved state before trying again.", "पूर्वावलोकन अपडेट नहीं हुआ। फिर कोशिश करने से पहले सेव की गई स्थिति रीफ़्रेश करें।")); return;
      }
      const next: Preview = await response.json(); if (!current()) return;
      remember(next); setSummary(next.payload.summary); setTopic(next.payload.topic); setResource(next.payload.resource_id ?? "");
      setUncertain(false); setUnavailable(false);
      setMessage(kind === "save" ? say("Preview saved. Review the exact text below.", "पूर्वावलोकन सेव हो गया। नीचे पूरा पाठ जाँचें।") : say("Preview cancelled.", "पूर्वावलोकन रद्द हो गया।"));
    } catch (e) {
      if (current() && (e as Error).name !== "AbortError") { setUncertain(true); setMessage(say("The result is uncertain. Refresh the saved preview before retrying.", "परिणाम स्पष्ट नहीं है। फिर कोशिश करने से पहले सेव किया गया पूर्वावलोकन रीफ़्रेश करें।")); }
    } finally { working.current = false; if (current()) setBusy(false); }
  }

  const canChoose = topic === "general" || (!loadingProducts && !productError && products.some(p => p.id === resource));
  const frozen = busy || uncertain || unavailable || (!!preview && preview.status !== "pending");
  const names = { general: say("General help", "सामान्य सहायता"), account: say("Account", "खाता"), card: say("Card", "कार्ड"), loan: say("Loan", "ऋण") };
  const status = preview ? ({ pending: say("Draft", "ड्राफ़्ट"), cancelled: say("Cancelled", "रद्द"), expired: say("Expired", "समय समाप्त") }[preview.status] ?? preview.status) : "";
  return <section className="support-preview" aria-label={say("Support preview", "सहायता पूर्वावलोकन")}>
    <button type="button" className="support-toggle" aria-expanded={open} onClick={() => { setOpen(!open); if (!open) void read(); }}>{say("Get help with this", "इस बारे में सहायता लें")}</button>
    {open && <div className="support-editor" ref={panel}>
      <h3>{say("Prepare a support request", "सहायता अनुरोध तैयार करें")}</h3>
      <p className="support-note">{say("Save and review your description. Case submission is not available yet.", "अपनी समस्या सेव करके जाँचें। अभी केस जमा करना उपलब्ध नहीं है।")}</p>
      {message && <p role="status" className="support-message">{message}</p>}
      <button type="button" disabled={busy} onClick={() => void read()}>{say("Refresh saved preview", "सेव किया गया पूर्वावलोकन रीफ़्रेश करें")}</button>
      <form onSubmit={e => { e.preventDefault(); if (!frozen && canChoose && summary.trim()) void mutate("save"); }}>
        <fieldset disabled={frozen}><legend>{say("Request details", "अनुरोध का विवरण")}</legend>
          <label>{say("What is this about?", "यह किस बारे में है?")}<select value={topic} onChange={e => { setTopic(e.target.value as Draft["topic"]); setResource(""); }}>
            {Object.entries(names).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
          </select></label>
          {topic !== "general" && <div className="support-product-choice"><label>{say("Choose your product", "अपना उत्पाद चुनें")}<select value={resource} onChange={e => setResource(e.target.value)} disabled={loadingProducts} required>
            <option value="">{loadingProducts ? say("Loading your products…", "आपके उत्पाद लोड हो रहे हैं…") : say("Choose a product", "उत्पाद चुनें")}</option>
            {resource && !products.some(p => p.id === resource) && <option value={resource} disabled>{preview?.resource_label || (selected?.id === resource ? selected.label : null) || say("Previously selected product", "पहले चुना गया उत्पाद")} — {say("load products to verify", "उत्पाद लोड करके जाँचें")}</option>}
            {products.map(p => <option key={p.id} value={p.id}>{productLabel(p)}</option>)}
          </select></label>
            {productError ? <p role="status">{say("Products are currently unavailable.", "अभी उत्पाद उपलब्ध नहीं हैं।")} <button type="button" onClick={() => void loadProducts()}>{say("Retry products", "उत्पाद फिर लोड करें")}</button></p> : !loadingProducts && !products.length && <p>{say("No products are available in this category.", "इस श्रेणी में कोई उत्पाद उपलब्ध नहीं है।")}</p>}
            {cursor && <button type="button" disabled={loadingProducts} onClick={() => void loadProducts(true)}>{say("Load more products", "और उत्पाद लोड करें")}</button>}
          </div>}
          <label>{say("Describe the issue", "समस्या बताएँ")}<textarea value={summary} onChange={e => setSummary(e.target.value)} maxLength={1000} rows={3} required /></label>
          <button type="submit" disabled={!summary.trim() || !canChoose}>{preview ? say("Save revised preview", "संशोधित पूर्वावलोकन सेव करें") : say("Save preview", "पूर्वावलोकन सेव करें")}</button>
        </fieldset>
      </form>
      {preview && <div className="support-saved" aria-label={say("Saved support preview", "सेव किया गया सहायता पूर्वावलोकन")}>
        <p><strong>{status}</strong> · {say("Version", "संस्करण")} {preview.version}</p>
        <p>{names[preview.payload.topic]}{preview.resource_label ? ` · ${preview.resource_label}` : ""}</p>
        <p className="support-summary">{preview.payload.summary}</p>
        <p className="support-note">{say("Expires", "समय सीमा")}: <time dateTime={preview.expires_at}>{new Date(preview.expires_at).toLocaleString(hi ? "hi-IN" : "en-IN")}</time></p>
        <button type="button" disabled={busy || uncertain || preview.status !== "pending"} onClick={() => void mutate("cancel")}>{say("Cancel preview", "पूर्वावलोकन रद्द करें")}</button>
      </div>}
    </div>}
  </section>;
}
