# COMP 3613 UWI Connect (Phase 2 starter)

Flawed baseline **0.2.0** built on [uwidcit/faststart](https://github.com/uwidcit/faststart).

FastStart provides the SSR shell, JWT cookie auth, SQLModel, `manage.py`, and Render/Docker wiring. The uwi-connect scenario adds domain models/services and a JSON API under `/api` (see `/docs`).

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

Open http://127.0.0.1:5000 — SSR auth shell; scenario JSON under `/api` and OpenAPI at `/docs`.

## Seeded API accounts

| Email | Password | Role |
| --- | --- | --- |
| `admin@uwi.edu` | `admin-dev` | admin |
| `alumni.verified@my.uwi.edu` | `alumni-dev` | alumni (verified) |
| `alumni.pending@gmail.com` | `alumni-dev` | alumni (unverified) |
| `alumni.fakeuwi@example.com` | `alumni-dev` | alumni (unverified) |

JSON login: `POST /api/auth/login` with `{"email","password"}`. It sets the same HttpOnly access-token cookie used by protected API routes; client-supplied `X-User-Id` headers are not authentication.

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
| `SMTP_TIMEOUT` | Connection timeout in seconds, at most `60` (default `10`) |
| `NOTIFICATION_EMAIL_ENABLED` | `false` by default; set `true` to send listing/campaign notification emails |
| `APP_BASE_URL` | Public app URL used in verification links; set this for deployments |

The legacy `SMTP_FROM_ADDRESS`, `SMTP_USE_IMPLICIT_TLS`, and `SMTP_USE_STARTTLS`
variables are no longer read; use `SMTP_FROM_EMAIL` and `SMTP_USE_TLS`.

`NOTIFICATION_EMAIL_ENABLED` controls notification emails only. Verification
emails still use the same SMTP service whenever registration occurs. When
notification emails are enabled, application and campaign-processor startup
validate the SMTP settings without opening a connection; invalid settings are
reported at startup. Notification emails are best-effort with replay protection
for recorded successes; they are not durable or guaranteed delivery.

Enable 2-Step Verification in your Google Account, then create an App Password
for this application. Do not use your normal Google password or commit `.env`.
Send a manual test message with:

```text
python manage.py send-test-email --to recipient@example.com
```

Verification tokens are stored as hashes and expire after 30 minutes. The
verification table is added by the existing non-destructive schema initializer.
Unverified accounts cannot log in or use existing authenticated sessions.
The login page offers a verification-email resend; API clients can POST
`{"email": "user@uwi.edu"}` to `/api/auth/resend-verification` with the CSRF
token described below. Responses do not disclose whether an account exists.
Resend requests are limited to one per recipient per ten minutes across
instances sharing the database, and five per client address per ten minutes in
each process. A link issued in the last ten minutes is not replaced. Earlier
links are invalidated only after SMTP accepts a replacement; delivery failures
leave them usable. SMTP acceptance and database commit are not atomic, and mail
providers can still delay or reorder messages. The IP limit is not shared across
instances; use a deployment-edge rate limiter for distributed IP protection.
`GET /api/users` requires an authenticated administrator session.

Same-origin browser clients can obtain a separate CSRF token from `GET /api/auth/csrf`; send it in `X-CSRF-Token` on state-changing fetch requests. Native SSR forms submit the same token as a hidden `csrf_token` field. The CSRF endpoint never returns the HttpOnly JWT.

## Campaign delivery and scheduling

Campaigns publish website notifications to eligible verified alumni. If
`NOTIFICATION_EMAIL_ENABLED=true`, the shared SMTP observer also attempts email
delivery. Per-recipient email preferences are not currently configurable.
Campaign success depends on website notification delivery. Email is attempted
independently when enabled and is best-effort: an email failure alone does not
make the campaign unsuccessful or trigger a campaign retry, and a `sent`
status does not prove that a message reached an inbox. Email delivery records
successful sends, but a process failure between SMTP acceptance and recording
success can still result in a duplicate email on recovery.

Scheduled campaigns can still be created, but the Render Blueprint does not
include a scheduler, so they will not be processed automatically on Render.
For demonstrations, run `python manage.py process-campaigns` manually when
scheduled campaigns need processing. Run it in an environment configured to
use the application's database and settings. This command also processes
recovery work for campaigns left in progress. Automatic scheduled processing
would require a separately configured scheduler in the future.

The web service's Render Environment settings must contain
`NOTIFICATION_EMAIL_ENABLED` and all eight `SMTP_*` keys when using email
notifications. The Blueprint marks them as dashboard-managed (`sync: false`),
so on an existing Blueprint you must add any missing keys in the Render
Dashboard. Do not put SMTP credentials in this file. Set notification email
to `false` when disabled; to enable delivery, set it to `true` and provide
valid SMTP credentials and sender settings. Manually running the campaign
processor uses these settings for optional email delivery.

## Template escaping

The Jinja environment currently has autoescaping disabled. Moderation,
revision, and deletion review templates explicitly escape submitted listing
content, and the campaign table escapes campaign titles and bodies. Other
templates still render user-controlled listing/form values without consistent
escaping (including `app.html`, `landing.html`, and `alumni_dashboard.html`).
Enabling autoescaping globally would affect all shared templates and should be
coordinated with the dashboard, listing, and shared-layout owners before that
broader change is made.

## Student brief

See [`ASSIGNMENT.md`](ASSIGNMENT.md) and [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md).

## Tests

```bash
python -m pytest tests -q
```
