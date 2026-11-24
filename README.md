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

JSON login: `POST /api/auth/login` with `{"email","password"}`. Other scenario endpoints take an `X-User-Id` header (demo auth).

Same-origin browser clients can obtain a separate CSRF token from `GET /api/auth/csrf`; send it in `X-CSRF-Token` on state-changing fetch requests. Native SSR forms submit the same token as a hidden `csrf_token` field. The CSRF endpoint never returns the HttpOnly JWT.

## Student brief

See [`ASSIGNMENT.md`](ASSIGNMENT.md) and [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md).

## Tests

```bash
python -m pytest tests -q
```
