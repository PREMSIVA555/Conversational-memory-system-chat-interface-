# frontend — M2.5 thin chat checkpoint

> **This is the M2.5 checkpoint, not the finished UI.** It exists so a human can
> open a browser, send a message, and see a real reply from the real backend —
> proving the stack works today rather than at M6, five backend milestones deep.
> **M6 extends this same app in place**: token streaming, the memory management
> panel, and the Playwright e2e suite all land here. Deliberately absent for now:
> streaming, memory browsing/editing, governance endpoints, e2e tests.

Next.js (App Router) + TypeScript.

## Running it

The backend must be up first. From the repository root:

```bash
python -m api.main          # NOT `uvicorn api.main:app` — see api/main.py for why
```

Then, in a second terminal:

```bash
cd frontend
npm install
cp .env.local.example .env.local
npm run dev
```

Open the URL the dev server prints (http://localhost:3000, or the next free port
if 3000 is taken) and send a message.

## Environment

| Variable | Required | Purpose |
| --- | --- | --- |
| `API_BASE_URL` | yes | Base URL of the FastAPI backend, e.g. `http://localhost:8000`. |

There is no hardcoded fallback: if the variable is missing or blank the chat
route answers 500 with a readable message rather than quietly guessing a host.

**No `NEXT_PUBLIC_` prefix, on purpose.** Next inlines `NEXT_PUBLIC_*` variables
into the browser bundle at every reference. This value is read only server-side,
and without the prefix a future client-side reference *cannot* leak the internal
backend URL into public JS — it would simply be `undefined` in the browser.

**Nothing secret belongs anywhere under `frontend/`.** This app is served to a
browser, so treat every file here as public. API keys, database URLs and
provider credentials stay in `infra/.env` on the backend side.

## Layout

| Path | What it is |
| --- | --- |
| `app/page.tsx` | The chat view — transcript, composer, loading and error states. |
| `app/api/chat/route.ts` | Server-side proxy to the FastAPI backend (see below). |
| `app/layout.tsx` | Root layout and metadata. |
| `app/globals.css` | Styles, light and dark. |
| `lib/api.ts` | Typed client: `sendChat`, request/response types, `ChatError`. |

## Why the browser does not call the backend directly

`api/main.py` mounts no CORS middleware, so a preflighted `POST` from the
browser to `http://localhost:8000/chat` is rejected — `OPTIONS /chat` answers
`405`. Instead the browser posts to this app's own `/api/chat` route and Node
forwards the call server-side. Same origin, no preflight, and the backend never
has to be reachable from the user's browser.

If CORS middleware is added to the backend later, `sendChat` can point straight
at `resolveApiBaseUrl()` and `app/api/chat/route.ts` can be deleted.

## Backend contract

Mirrors `api/chat.py` — keep `lib/api.ts` in step with it.

`POST /chat` with `{ message, subject_id?, actor_id?, stream, capture }`.

This milestone sends `stream: false`, which returns a single JSON body:

```json
{ "reply": "...", "subject_id": "...", "actor_id": "...", }
```

The backend mints a `subject_id` on the first turn; the UI adopts it from the
response and sends it on every later turn, so a session's memories accumulate
under one subject.

M6 will switch to `stream: true`, which returns a chunked `text/plain` body.
**The proxy route is already ready for that**: it forwards the client's `stream`
flag rather than overriding it, pipes the upstream body through without
buffering, and mirrors the upstream content type. The change M6 has to make is
in `sendChat`, which today calls `response.text()` and so waits for the whole
body; it becomes a reader over `response.body`. No change to the route.

## Two backend behaviours the UI is built around

**Capture is asynchronous.** The reply returns *before* the memory row is
written — a measured turn showed a ~9.6 second gap. The UI therefore never
implies a memory has been persisted; each assistant turn carries a note saying
capture happens afterwards. Do not add anything here that reads a memory back
immediately after writing it.

**Replies are slow and that is normal.** The completion provider is rate-limited
and gpt-oss models think before emitting content. There is deliberately **no
client-side timeout** — aborting early would report a healthy backend as broken.
The composer shows an elapsed-seconds counter so a human can tell "slow" from
"hung".

## Accessibility

The transcript container is the live region — `role="log"` with
`aria-live="polite"` and `aria-relevant="additions"` — so each turn appended to
it, the assistant's reply included, is announced as it arrives without re-reading
the whole conversation.

The waiting indicator and the error alert sit *outside* that log, in their own
status region. The elapsed-seconds counter is `aria-hidden` and outside any live
region on purpose: it changes every second, and inside a live region it would
talk over the reply with an endless "1s", "2s", "3s". If you add anything that
updates on a timer, keep it out of the log for the same reason.

## Checks

```bash
npm run build      # production build; must exit 0 with zero type errors
npm run typecheck  # tsc --noEmit
```

> **Do not run `npm run build` while `npm run dev` is serving.** Both write to
> the same `.next/` directory, and the build replaces artifacts the running dev
> server has already loaded — the page then fails with a `__webpack_modules__`
> 500. Stop the dev server first, or restart it after building. This applies in
> either order.
