# UX01 independent behavioral checks

Read this document only, plus your own inputs and operator outputs. Do not read implementation, tests, fixtures, operator source, Genesis state, previous conversation or maker results. Fresh instructional isolation is not an OS sandbox. Do not call Astra or any paid application model.

## Contract

Customer/session identity and owned resources remain server-established (SPEC05/07/09, INV01/02/03/06/07/09/10/15/16/22/23/24/25/26/33). A greeting receives a useful greeting and actions, not generic missing-information text. Logged-in customers can discover owned accounts/cards/loans and select by friendly labels, with no typed UUID requirement. Multiple matches require choices; suffix 1111 can be ambiguous. Typed selections must be revalidated for ownership and version at acceptance/execution. Explicit product/category overrides old context. Follow-ups use only authorized prior result context, not prior financial amounts. The second transaction resolves against the latest result list and is re-read. Context is scoped by customer/session/conversation, up to 20 refs, expires after 15 minutes and is invalidated on source erasure. Another login of the same owner must not inherit it.

Auto-prepare creates one conversation per login despite concurrent calls, and returns it after refresh. Explicit New chat creates another. CSRF and Origin are required for mutations. Foreign resources/errors reveal no financial values. Language/format commands use the existing ordered typed preference grammar; Saved means committed. Ambiguous “Reply in Hindi” asks which scope. English and Hindi greetings/help are supported.

Amounts/currencies/statuses/dates remain checked typed blocks. Policy locale/scope/effective timestamp must match the explicit question. Schedule and pending/declined/reversed records never establish posted payment. No financial mutations, invented causes/waivers or case submission. Optional model wording selects from checked conversational framing and allowed follow-up actions, never arbitrary financial prose; malicious or failed wording falls back to valid facts. No live model quality claim from scripted cases. Existing persisted deadlines/counters are not renewed on retry.

## Operator

Working directory: `C:/Users/mprem/.codex/worktrees/banking-ux/banking-memory-agent`.
Python: `D:/Projects/Banking Assistant/banking-memory-agent/.venv/Scripts/python.exe`.
Execute `scripts/ux_probe.py` with one JSON stdin document `{"steps":[...]}`. Capture output yourself. Each invocation owns a newly created guarded PostgreSQL 16 DB and Redis 7 namespace. It runs actual HTTP, server-issued sessions, worker, SQL and persisted results; raw SDK replies are scripted. This operator does not exercise OIDC. Credentials remain private; never open config or browser-access files.

Default actor A. A2 is another login of A. B is another customer. All steps support `save:"name"`. `$name.field.0.field` references a saved response body (list indexes allowed).

- `prepare`: returns `{status,body:{conversation_id}}`.
- `products`: `kind:account|card|loan`, optional `params:{cursor:...}`; returns actual page with internal IDs, typed Money/source and source timestamps. Save e.g. `pa`. Select with `{kind:"account",id:"$pa.items.0.id",version:"$pa.items.0.source.version"}`.
- `turn`: `cid:"$chat.conversation_id",text:"...",fields?:{action:balance|transactions|card_details|loan_details|schedule|payments|products|help,resource_selection:{kind,id,version},instructions:{language:en|hi,format:concise|detailed}}`; waits for actual worker terminal result and returns `{body:<result>,elapsed_ms,raw_dispatches}`. Saved value is the result itself. Actions include validated selection objects. Normal no-ID questions and greetings use no scripted completion.
- `accept`: cid and explicit body `{client_turn_id:canonicalUUID,text,...}`; optional headers for boundary negatives. Returns immediate HTTP result instead of waiting.
- `request`: method/path/body/headers for static public paths, e.g. POST `/api/v1/workspace/prepare` with absent/bad `X-CSRF-Token` or foreign Origin. Default headers contain valid auth/CSRF/Origin; override with empty strings to remove authority.
- `new`: explicit new chat.
- `context`: cid; returns the active authorized context or null.
- `preferences`: optional cid; actual GET effective preferences. `preference_messages`: cid; actual GET durable preference receipt history, including recovery of pending consumer work. Both accept `$chat.conversation_id`.
- `script`: `outputs:[raw JSON objects/strings,...]`. Each SDK attempt consumes one output. Plans use `{tool,args}` with registered read tools: get_account(account_id), list_accounts, get_card(card_id), list_cards, list_card_transactions(card_id,start?,end?,status?,limit?,cursor?), get_loan(loan_id), list_loans, list_emi_schedule(loan_id,...), list_loan_payments(loan_id,...), explain_policy(product,topic,scope?,locale?,at?), clarify, unsupported. Plans are not authority to choose a hidden product. Optional wording response has `{intro,follow_up}`. Valid English intro example: `Here’s what I found.` Allowed follow_up includes transactions/schedule/payments only when offered by the current result. Financial claims in intro or additional keys must be rejected. Use explicit selected products and “Explain…” to test optional wording; each run starts with one clarify output that script replaces.
- `expire_context`: trusted clock fault in this guarded private DB only.
- `erase_context`: cid; erases active source through the authenticated erasure API.
- `change_product`: kind,id; trusted fixture revision change in private DB. This intentionally changes oracle hash; do not call it a preservation failure.
- `revoke`: actor; invalidates its Redis session.
- `bank_db_outage` / `restore_bank_db`: trusted private application DB-access fault (no container stop or data mutation). Login-session Redis stays available. Logout must revoke the accepting session even if optional SQL resource-context cleanup fails; after restore another login must not inherit its context. Do not call direct `context` for a revoked actor: the helper correctly raises401. Use actual HTTP session/products reads for its revoked boundary instead.
- `parallel`: steps array; parallel children cannot reference sibling outputs.
- `oracle`: matched PG16/Redis7 identities, resource preservation flag and raw dispatch count.

Fixture facts exposed by products: A has 2 accounts, 3 cards (2 ending1111), 3 loans; B has 1 of each. Savings current minor units `9007199254740993`, available `12000000`, INR exponent2. Credit-card history 2026-01-14..15 has four statuses/amounts -700declined,0pending,9007199254740993posted,-1reversed. Loan history contains partial posted4000/pending6000, declined0 and reversed record; monthly amount/schedule are not actual payment proof. Policy at2026-06-01 retail/en-IN annual fee100, waiver10000; de-DE/business absent. Compare exact typed minor units, not floating-point conversions.

Language verification examples (input grammar, not maker results): `Always reply in Hindi`, `Always be detailed`, `Reply in English for this conversation`, `Be concise for this conversation`, `Reply in Hindi for this response`. An entire command must match the existing typed grammar. `Reply in Hindi` alone should ask scope. The GET `/api/v1/conversations/{cid}/preference-messages` response exposes receipt status (`pending`, `persisted`, `temporary`, `ignored_stale`); GET `/api/v1/preferences?conversation_id={cid}` exposes effective preferences. Construct dynamic paths from your saved conversation through your own harness rather than expecting interpolation inside a string.

Valid policy planning syntax: `{ "tool":"explain_policy", "args":{"product":"credit-card","topic":"annual_fee","scope":"retail","locale":"en-IN","at":"2026-06-01T00:00:00Z"} }`. `fee_waiver` is another topic. These are synthetic approved policy fixtures, not customer waiver approval. Use explicit question constraints to test whether a raw plan attempts to substitute a different locale/scope/effective time. Wording may reject an unsupported phrase even after a valid policy read; verify the checked answer independently.

Write your independent cases and raw results under `.genesis/evidence/ux01-verifier-*`; report `.genesis/evidence/UX01-independent-verifier.json` with distinct counts, actual commands, failures/limitations and verdict. Do not edit source or approval state. Report defects promptly and independently retest after maker notification. No source audit, universal language/privacy certification or live timing SLA claim.
