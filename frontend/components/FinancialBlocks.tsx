"use client";
import { Block, Money, money } from "./workspaceTypes";
import { CheckedCase, type CurrentCase } from "./SupportCasesPanel";

export default function FinancialBlocks({ blocks, hi }: { blocks: Block[]; hi: boolean }) {
  return <>{blocks.map((block, index) => {
    const d = block.data;
    if (block.kind === "support_case") return <CheckedCase key={index} value={d as unknown as CurrentCase} hi={hi} />;
    const amount = (key: string) => money(d[key] as Money | null | undefined);
    const pairs: [string, string][] = block.kind === "account" ? [
      [hi ? "उपलब्ध बैलेंस" : "Available balance", amount("available_balance")], [hi ? "वर्तमान बैलेंस" : "Current balance", amount("current_balance")],
    ] : block.kind === "card" ? [
      [hi ? "स्थिति" : "Status", String(d.status)], [hi ? "दैनिक सीमा" : "Daily limit", amount("daily_limit")],
      [hi ? "क्रेडिट सीमा" : "Credit limit", d.credit_limit ? amount("credit_limit") : hi ? "डेबिट कार्ड पर लागू नहीं" : "Not applicable to debit cards"],
      [hi ? "उपलब्ध क्रेडिट" : "Available credit", d.available_credit ? amount("available_credit") : "—"], [hi ? "बकाया" : "Outstanding", d.outstanding ? amount("outstanding") : "—"],
    ] : block.kind === "loan" ? [
      [hi ? "स्थिति" : "Status", String(d.status)], [hi ? "मूलधन" : "Principal", amount("principal")], ["APR", String(d.apr_percent) + "%"],
      [hi ? "मासिक भुगतान" : "Monthly payment", amount("monthly_payment")],
    ] : [];
    const rows = d.items as Record<string, unknown>[] | undefined;
    return <div className="fact-card" key={index}>
      <h3>{block.title}</h3>
      {!!pairs.length && <dl className="fact-grid">{pairs.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>}
      {block.kind === "loan" && <p className="fact-note">{hi ? "मासिक राशि वास्तविक भुगतान का प्रमाण नहीं है।" : "The monthly amount is contractual, not evidence of payment."}</p>}
      {block.kind === "policy" && <p className="policy-text">{String(d.text)}</p>}
      {rows && <>
        {!rows.length && <p>{hi ? "इस अवधि में कोई रिकॉर्ड नहीं मिला।" : "No records were found for this period."}</p>}
        <ul className="record-list">{rows.map((item, rowIndex) => <li key={String(item.id ?? rowIndex)}>
          <span className="record-index">{rowIndex + 1}</span>
          <div><strong>{block.kind === "products" ? String(item.display_name) : money((item.amount ?? item.scheduled_amount) as Money)}</strong>
            <small>{String(item.status ?? (item.installment_number ? `${hi ? "किस्त" : "Installment"} ${item.installment_number}` : ""))}
              {item.last_four ? ` · ${item.kind} •••• ${item.last_four}` : ""}</small></div>
          <time>{String(item.booked_on ?? item.due_on ?? item.occurred_at ?? "").replace("T", " ").replace(/\+00:00$/, " UTC")}</time>
        </li>)}</ul>
        {!!d.start && <p className="fact-note">{String(d.start)} – {String(d.end)} · {d.complete ? hi ? "पूर्ण सूची" : "Complete list" : hi ? "अन्य रिकॉर्ड उपलब्ध हैं" : "More records available"}</p>}
      </>}
      {!!d.source && <small className="source-note">{hi ? "स्रोत की जानकारी" : "Source as of"}: {String((d.source as { as_of: string }).as_of).replace("T", " ")}</small>}
    </div>;
  })}</>;
}
