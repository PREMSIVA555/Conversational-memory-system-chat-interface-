# P01 authenticated foundation

P01 implements signed OIDC login, opaque Redis sessions, browser mutation protection,
owned conversation roots, constrained roles, transaction-local RLS, append-only
audit and allowlisted operational telemetry. Conversation roots demonstrate owner
access; they do not accept chat turns or expose bank products. Durable events,
preferences and support behavior remain later phases.

## Local setup and launch

Use the existing Python 3.11 environment and Node installation:

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm --prefix frontend ci --no-audit --no-fund
docker pull quay.io/keycloak/keycloak:26.8.0
.venv\Scripts\python.exe scripts\p01_setup.py
.venv\Scripts\python.exe scripts\p01_run.py api
.venv\Scripts\python.exe scripts\p01_run.py frontend
```

Open `https://localhost:3443`. The local TLS certificate is self-signed and is
stored in `.genesis/local/p01/tls.crt`; the browser must accept that development
certificate for the app and the local issuer. The OIDC client verifies that exact
local certificate; it never disables TLS verification. No system trust store is
changed. Use publicly trusted HTTPS and your actual issuer/client configuration
for deployment; the development Keycloak realm is not a deployed identity service.

The setup creates fresh, uniquely named Docker PostgreSQL 16, Redis 7 and Keycloak
26.8.0 containers and two fictional customers. It never resets the existing memory
database or P00 test services. Random credentials, user sign-in details, data
locators, PID files, logs and the private TLS key remain in ignored
`.genesis/local/p01/`. Read `config.json` locally for the two synthetic sign-ins;
do not commit it or copy it into frontend code. The supplied `infra/.env` remains
unchanged. `runtime.env` supplies the banking profile; the existing model API keys
are not needed or called by these foundation routes.

The launcher refuses occupied ports and does not kill servers. For a restart,
inspect the PID file and the actual command/process before stopping it. Keep the
local config and container identities; do not reinitialize an existing directory.
A partial setup is a failure requiring inspection, not permission to overwrite
state. Container recreation/removal is an explicit maintenance action.

## Browser and API contract

Next rewrites `/bank-api/*` to backend `/api/v1/*`; cookies and browser Origin reach
the same auth boundary. Backend-only issuer/client secrets and DSNs never enter
browser bundles. Login uses authorization code flow, PKCE S256, state, nonce,
RS256 signature and issuer/audience/time validation via Authlib. A server-only
issuer/subject link resolves the internal customer. An unprovisioned identity is
denied.

`__Host-bank_sid` is opaque, Secure, HttpOnly, SameSite=Lax, Path=/, without Domain.
Redis keys hash cookie values. Login transactions are browser-bound, single-use
and expire after five minutes. Sessions default to an absolute 30-minute real-time
lifetime; explicit rotation replaces both cookie and CSRF secret and invalidates
the old session atomically. Logout revokes server state and clears browser working
state. The session watch ends on expiry/revocation. Queued access revalidates the
session before any resource read.

Available routes:

- `GET /api/v1/auth/login`, `GET /api/v1/auth/callback`.
- `GET /api/v1/auth/session`, `POST /api/v1/auth/rotate`, `POST /api/v1/auth/logout`.
- `GET /api/v1/auth/watch`: session-lifetime heartbeat, not the future run SSE API.
- `GET/POST /api/v1/conversations` and `GET /api/v1/conversations/{id}`: ownership roots.
- `GET /health/live`, `GET /health/ready`, `GET /metrics`.

POST bodies for the foundation mutations are exactly `{}`. Send the session's
`X-CSRF-Token` and the configured browser `Origin`. Mutation DTOs reject unknown
fields; identity headers/query fields are rejected. Auth failures return 401,
browser protection failures 403, foreign and missing resources the same 404,
invalid input 422 and required-store/config failures 503. Public errors contain
no original exceptions or SQL. Client-supplied trace IDs confer no authority.

`APP_PROFILE=banking` is the default. Legacy HTTP routes are not mounted there.
For the original local-only memory app, `APP_PROFILE=legacy-local` and
`API_HOST=127.0.0.1` must be explicit, and only `python -m api.main` is supported;
the import-string launch is rejected for that profile. This does not certify the
legacy identity mechanism as authentication.

## Roles, privacy and operational boundaries

The offline bootstrap creates distinct runtime, auth, worker, dispatcher and
migrator roles. None is a superuser, database owner or privileged-role member.
Only the migrator owns the banking schema. Auth reads identity links; runtime
cannot. Runtime accesses customer rows using a revalidated RequestContext and
transaction-local tenant context. Worker reads use a separate constrained pool.
Dispatcher has no customer-data grants before the P02 outbox is introduced.
Composite foreign keys preserve parent/child ownership. Audit insertion and
conversation creation share a transaction; runtime cannot edit/delete audit.

Readiness checks actual roles, dangerous grants, forced RLS and missing-context
denial, plus issuer and Redis connectivity. Role or auth-store failures close
serving access. Liveness remains available when dependencies fail.

Logs and explicit OTel spans export bounded metadata, never arbitrary exception
messages, customer IDs, prompts or bank facts. Incoming trace headers are ignored.
Worker correlation is a link across a separate process and never an identity
source. Optional trace export uses a bounded queue and failure counter; operational
metrics have bounded route/method/outcome dimensions. Access logging is disabled
to keep callback authorization codes out of application logs. Configure external
reverse-proxy/hosting logs separately; those are outside this local sink proof.
The pre-model minimizer is tested, while banking routes do not call models yet.

The in-memory access queue is a P01 invalid-session/overflow/alert seam, not a
durable worker. Prometheus rules cover overflow, rejected work and optional
telemetry drops. `promtool` tests rule firing on synthetic counters. Applying the
updated Compose configuration mounts those rules; rule tests alone do not prove
that an operator received a live alert.

Threat boundary: untrusted browsers, request identity fields, model output and
trace carriers cannot select a customer. Trusted dependencies are the configured
issuer, provisioned identity links and server-held credentials. This does not
promise isolation after complete application/credential/DB-admin compromise.

## Verification

```powershell
.venv\Scripts\python.exe scripts\p01_check.py evidence\security\YOUR_NEW_RUN --task P01
npm --prefix frontend run typecheck
```

Use a fresh evidence directory; output is not overwritten. The runner records
actual test counts, sanitized outcomes, source/artifact hashes and service image
identities. `jobs.banking_probe` is the isolated subprocess test seam, not a
public API or a durable consumer. The suite makes real local Keycloak, Redis and
PostgreSQL calls; it does not invoke paid completion/embedding providers. Missing
setup is a failure, never a passing mock substitute. Genesis human review gates
remain separate from technical test results.
