# YatraNest

Database foundation for the Isocode Labs Problem 4 tour operator platform.

## Requirements

- Python 3.11 or newer
- PostgreSQL 14 or newer

## Local setup

From the project root, create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Copy the example environment file and edit its database URL for your local PostgreSQL user and password:

```powershell
Copy-Item .env.example .env
```

Create a PostgreSQL database named `isocode_tours`. For example, in `psql`:

```sql
CREATE DATABASE isocode_tours;
```

The example URL in `.env.example` is:

```text
postgresql+psycopg://localhost/isocode_tours
```

This URL uses your local PostgreSQL authentication defaults. If your server requires a username, password, or non-default port, add those to the URL in `.env`. Do not commit `.env`.

## Run the application

```powershell
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/>. Health endpoints are available at `/health` and `/health/db`.

## Database migrations

The initial PostgreSQL schema is managed with Alembic. Apply migrations with:

```powershell
alembic upgrade head
```

When the models change, generate a migration and review its operations before applying it:

```powershell
alembic revision --autogenerate -m "describe schema change"
alembic upgrade head
```

The SQLAlchemy models are in `app/models/`. Seasonal prices use inclusive month ranges; when the start month is later than the end month, the season crosses into the next calendar year (October through March is stored as `10` to `3`). Optional absolute date limits can narrow a recurring season. Package versions store complete JSON snapshots including all prices, and the version helper appends new version numbers.

Seed only the two packages supplied in the assignment with:

```powershell
python -m app.seed_data
```

The seed script is safe to rerun and creates the initial version snapshot for each package.

## Run tests

Tests require a dedicated PostgreSQL database. They stop immediately if
`TEST_DATABASE_URL` is missing, is not PostgreSQL, or names the protected
development database `isocode_tours`. The test suite never falls back to
`DATABASE_URL` or the application's `.env` file.

Create a separate database once in PostgreSQL (do not use the development
database):

```sql
CREATE DATABASE isocode_tours_test;
```

Set the test connection URL in the current PowerShell session, using your own
local PostgreSQL credentials where required. Do not commit this value:

```powershell
$env:TEST_DATABASE_URL = "postgresql+psycopg://<username>:<password>@localhost:5432/isocode_tours_test"
```

Initialize the new test database schema once by pointing Alembic at that
database. This applies the existing migrations to the **test database only**:

```powershell
$env:DATABASE_URL = $env:TEST_DATABASE_URL
.\.venv\Scripts\alembic.exe upgrade head
Remove-Item Env:DATABASE_URL
```

Tests create their own supplied sample package rows inside rollback
transactions; do not seed or reset the test database manually. A separate
database is mandatory so tests cannot alter development data.

Before every database-backed test run, explicitly point the process at the
isolated test database and verify the database identity. This check prints only
the database name and fails closed on any other target:

```powershell
$env:DATABASE_URL = $env:TEST_DATABASE_URL
python -c "import os; from sqlalchemy import create_engine, text; from sqlalchemy.engine import make_url; url=make_url(os.environ['DATABASE_URL']); assert url.database == 'isocode_tours_test'; engine=create_engine(os.environ['DATABASE_URL']); conn=engine.connect(); name=conn.scalar(text('SELECT current_database()')); print(name); assert name == 'isocode_tours_test'; conn.close(); engine.dispose()"
```

Keep `DATABASE_URL` set from `TEST_DATABASE_URL` while running the tests. If
the preflight fails, do not run pytest.

Run focused tests:

```powershell
python -m pytest -q tests/test_public_routes.py
python -m pytest -q tests/test_admin_package_history.py
```

Run the complete suite:

```powershell
python -m pytest -q
```

Check the test database schema without applying migrations:

```powershell
$env:DATABASE_URL = $env:TEST_DATABASE_URL
.\.venv\Scripts\alembic.exe check
Remove-Item Env:DATABASE_URL
```

Clear the test URL from the current shell when finished:

```powershell
Remove-Item Env:TEST_DATABASE_URL
```

The database health endpoint executes `SELECT 1` against PostgreSQL.

## Public package enquiries

Published package pages accept enquiries through a server-rendered form. The
response notice records that the traveller agreed to contact about this
specific enquiry; WhatsApp consent is a separate optional choice. Submission
uses a short-lived, one-use form token and same-origin checks. The application
does not send email, SMS, or WhatsApp messages, and it does not yet implement
application-level rate limiting. A production deployment must apply request
rate limits at its reverse proxy or hosting edge and configure trusted proxy
headers so same-origin checks see the original HTTPS origin.

## Staff authentication foundation

After applying migrations, create staff accounts explicitly from a local terminal:

```powershell
python -m app.create_staff
```

The command prompts for the staff name, email, role, and password. Password input is hidden, passwords must be at least 12 characters, and the project does not create default accounts. Staff sign-in is at `/admin/login`. Sessions are stored in PostgreSQL, use opaque cookies whose token hashes are stored server-side, expire after 30 minutes idle or 8 hours absolute, and are revoked on logout. Set `APP_ENV=production` behind HTTPS so the session cookie's `Secure` flag is enabled.

## AI-assisted enquiry workflow

AI is optional. Public enquiries are saved and the browser confirmation is
returned independently of AI. When configured, the application uses Groq for
extraction and staff-initiated reply drafting. Keep provider credentials in the
deployment secret store or an untracked local `.env`; never put them in source,
tests, command history, or logs.

Configuration variables (values are deployment-specific):

- `AI_ENABLED` — `false` disables AI without disabling enquiries or manual staff workflows.
- `AI_PROVIDER` — select `groq` for the supported active provider. Gemini remains deferred.
- `GROQ_API_KEY` and `GROQ_MODEL` — required only when AI is enabled.
- `AI_TIMEOUT_SECONDS` — bounded provider timeout (1–60 seconds).
- `AI_MAX_RETRIES` — bounded retry count (0–2).
- `AI_FALLBACK_ENABLED` — retained for the shared provider orchestration; reply drafting requires Groq directly.
- `DATABASE_URL` — PostgreSQL application connection.
- `APP_ENV` — use `production` behind HTTPS to enable secure staff cookies.

If AI is disabled or Groq is unconfigured, staff can still review the original
enquiry, accept a manually corrected extraction, use deterministic package
recommendations where data permits, and write a reply manually. Groq output is
not accepted as extraction automatically, and generated reply text is an
editable draft only. The application never sends email, SMS, or WhatsApp.

The public enquiry route has a streaming 16 KiB body limit, nonce, origin,
honeypot, field-length, and payload validation protections but does not
implement application-level rate limiting.
Production hosting must enforce both request-rate limits and a request-body
limit at the reverse proxy or hosting edge. Configure trusted proxy headers
with an explicit trusted proxy address so HTTPS origin checks receive the
correct external scheme and host.

## Staff quotation builder

From an enquiry workspace, staff can create a quotation using a currently
published package and a `PackagePrice` that covers the full travel period, or
enter a staff-approved per-person rate. Package rates retain their stored
currency and price basis. Child and infant fares are never inferred; a complete
total and admin approval require explicit fares for every traveller category
present. GST is added only when staff supplies a rate; otherwise the displayed
amount is explicitly a pre-GST total and GST treatment remains unconfirmed.

Quotations are saved as drafts. Only an administrator can approve them. An
approved quotation can receive a random, 30-day bearer link for browser viewing
and PDF download. The link is not an authentication account; anyone possessing
it can access that approved quotation. Share it privately, and configure hosting
access logs to redact `/quotes/{quotation-id}/{token}` paths. Responses are
no-store/noindex. Approval and sharing do not reserve inventory, confirm a
booking, or send email, SMS, or WhatsApp messages. The PDF generator is a small
in-repository renderer and does not require a third-party PDF package.

## Travel blog CMS

Admins and editors manage server-rendered travel stories at `/admin/blog`.
They can save a post as a draft, preview it privately, then publish or
unpublish it. Public readers use `/blog` and `/blog/{slug}`. Blog content is
plain text and is escaped by Jinja; categories are labels stored on each post.
Editors can associate only currently published package records, which are
rechecked when saving. Public story pages hide associated packages that are no
longer published.

Cover uploads accept JPEG and PNG only, are capped at 5 MB with an additional
6 MB streamed request limit, and use generated filenames under
`app/static/uploads/blog`. File signatures/structure and pixel dimensions are
checked; SVG and arbitrary HTML uploads are not accepted. Keep the upload
directory writable by the application process and include it in the normal
backup policy. A production deployment should also limit upload requests at the
reverse proxy. Consultants do not have blog-management access.

Extraction uses FastAPI `BackgroundTasks`, which are process-local and
best-effort. They are not a durable queue: a process restart can leave work
pending or processing. Staff can retry failed, unavailable, and genuinely stale
work from the enquiry workspace. Deploy multiple application workers only with
the understanding that row locks and attempt tokens prevent duplicate or stale
extraction persistence; they do not provide durable task delivery.

Staff-triggered AI reply drafting currently makes a synchronous, bounded Groq
request. The database session remains open during that provider call so the
service can verify current recommendations and price rows before saving the
draft. Keep the configured provider timeout/retry limits conservative and size
the PostgreSQL connection pool for concurrent staff drafting. A durable worker
queue could reduce request-time connection use if drafting volume grows; it is
not part of this application.

## Production operations and data policy

For deployment, provision PostgreSQL and set `DATABASE_URL` through the host's
secret/configuration system. Apply reviewed Alembic migrations before starting
the web process:

```powershell
python -m alembic upgrade head
python -m app.create_staff
uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1
```

Replace `127.0.0.1` with the actual trusted proxy address for the deployment.
Do not use `--forwarded-allow-ips "*"` on an internet-facing deployment unless
the network topology guarantees only trusted proxies can reach the app. Put
TLS, request-size limits, rate limiting, and access logging with PII redaction
at the hosting edge. No live production deployment is included in this
repository or claimed by this project.

Back up PostgreSQL regularly using the hosting provider's managed backup
facility or `pg_dump` in custom format. Keep backups access-controlled and
encrypted, verify them with `pg_restore --list`, and practice restoring into a
separate database before relying on them. Prefer point-in-time recovery where
the provider supports it. Do not test restores by overwriting the live database.

No traveller-data retention period is encoded here; the operator must set and
document a production retention/deletion policy consistent with applicable
privacy requirements. The supplied package prices do not specify GST
treatment, so staff must confirm it before communicating a final quote. Stored
per-person prices do not establish child/infant fares, group totals, inventory,
or booking availability. These assumptions require business confirmation.

For a final staff/public workflow walkthrough, use the synthetic-data checklist
in [`docs/manual-qa-checklist.md`](docs/manual-qa-checklist.md). The checklist
is guidance only; its items are not marked complete by automated tests.
