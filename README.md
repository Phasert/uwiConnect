# COMP 3613 UWI Connect (Phase 2 starter)

Flawed baseline **0.2.0** built on [uwidcit/faststart](https://github.com/uwidcit/faststart).

FastStart provides the SSR shell, JWT cookie auth, SQLModel, `manage.py`, and Render/Docker wiring. The uwi-connect scenario adds domain models/services and a JSON API under `/api`.

## Quick start

```text
python -m venv .venv
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # Windows: Copy-Item .env.example .env
python manage.py init
python manage.py run
```

Open http://127.0.0.1:5000 — SSR auth shell; scenario JSON under `/api`.

## Seeded API accounts

| Email | Password | Role |
| --- | --- | --- |
| `admin@uwi.edu` | `admin-dev` | admin |
| `alumni.verified@my.uwi.edu` | `alumni-dev` | alumni (verified) |
| `alumni.pending@gmail.com` | `alumni-dev` | alumni (unverified) |
| `alumni.fakeuwi@example.com` | `alumni-dev` | alumni (unverified) |

JSON login: `POST /api/auth/login` with `{"email","password"}`. Other scenario endpoints take an `X-User-Id` header (demo auth).

## Gmail email delivery

The reusable SMTP service sends authenticated Gmail messages with STARTTLS.
Registration emails are queued as post-response tasks and contain a single-use,
30-minute verification link. SMTP failures are logged without credentials and
do not fail registration.

Set these values in the ignored `.env` file (also listed in `.env.example`):

| Variable | Value |
| --- | --- |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USERNAME` | Your Gmail address |
| `SMTP_PASSWORD` | Your Google-generated 16-character App Password |
| `SMTP_FROM_EMAIL` | Sender email address (usually the same Gmail address) |
| `SMTP_FROM_NAME` | Optional name shown to recipients |
| `SMTP_USE_TLS` | `true` (required; uses STARTTLS) |
| `SMTP_TIMEOUT` | Connection timeout in seconds (default `10`) |
| `APP_BASE_URL` | Public app URL used in verification links; set this for deployments |

Enable 2-Step Verification in your Google Account, then create an App Password
for this application. Do not use your normal Google password or commit `.env`.
Send a manual test message with:

```text
python manage.py send-test-email --to recipient@example.com
```

Verification tokens are stored as hashes and expire after 30 minutes. The
verification table is added by the existing non-destructive schema initializer.

Same-origin browser clients can obtain a separate CSRF token from `GET /api/auth/csrf`; send it in `X-CSRF-Token` on state-changing fetch requests. Native SSR forms submit the same token as a hidden `csrf_token` field. The CSRF endpoint never returns the HttpOnly JWT.

## Student brief

See [`ASSIGNMENT.md`](ASSIGNMENT.md) and [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md).

## Tests

```bash
python -m pytest tests -q
```
