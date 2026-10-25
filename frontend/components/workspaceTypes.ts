export type Session = { customer_id: string; csrf_token: string; expires_at: number };
export type Kind = "account" | "card" | "loan";
export type Money = { minor_units: string; currency: string; exponent: number };
export type Product = { id: string; display_name: string; kind?: string; last_four?: string; status?: string;
  current_balance?: Money; available_balance?: Money; daily_limit?: Money; credit_limit?: Money | null;
  available_credit?: Money | null; outstanding?: Money | null; principal?: Money; monthly_payment?: Money;
  apr_percent?: string; source: { version: string; as_of: string } };
export type Resource = { kind: Kind; id: string; version: string };
export type Action = { label: string; action?: string; resource_selection?: Resource | null; text?: string };
export type Block = { kind: string; title: string; data: Record<string, unknown> };
export type Result = { text: string; intro?: string; message_kind?: string; status: string; blocks?: Block[]; actions?: Action[]; next_question?: string; workspace?: { resource: (Resource & { label?: string }) | null } };
export type Turn = { turn_id: string; client_turn_id: string; accepted_at: string; status: string; user_text?: string; text?: string; result: Result | null };
export type ProductPage = { status: string; items: Product[]; complete: boolean; next_cursor: string | null; elapsed_ms?: number };

// Exact decimal formatting: financial values never pass through JS Number.
export function money(value: Money | null | undefined): string {
  if (!value) return "—";
  const negative = value.minor_units.startsWith("-");
  const digits = value.minor_units.replace(/^-/, "").padStart(value.exponent + 1, "0");
  const whole = (value.exponent ? digits.slice(0, -value.exponent) : digits).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${value.currency} ${negative ? "−" : ""}${whole}${value.exponent ? "." + digits.slice(-value.exponent) : ""}`;
}
export function resource(item: Product, kind: Kind): Resource { return { kind, id: item.id, version: item.source.version }; }
