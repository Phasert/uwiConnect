from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.database import engine
from app.main import app
from app.models.notification import Notification
from app.seed import seed
from app.services import board_auth
from tests.listing_auth_helpers import login as authenticate

ALUMNI_EMAIL = "alumni.verified@my.uwi.edu"
ALUMNI_PASSWORD = "alumni-dev"
ADMIN_EMAIL = "admin@uwi.edu"
ADMIN_PASSWORD = "admin-dev"

JS_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "notifications.js"
)


@pytest.fixture(scope="module", autouse=True)
def seeded_database():
    seed(reset=True)


@pytest.fixture
def alumni_client():
    client = TestClient(app)
    authenticate(client, ALUMNI_EMAIL, ALUMNI_PASSWORD)
    return client


def test_bell_is_rendered_on_authenticated_alumni_pages(alumni_client):
    for path in ("/app", "/app/dashboard"):
        page = alumni_client.get(path)

        assert page.status_code == 200
        assert 'id="notificationMenu"' in page.text
        assert 'id="notificationBell"' in page.text
        assert 'aria-label="Notifications"' in page.text
        assert 'data-bs-toggle="dropdown"' in page.text
        assert 'id="notificationPanel"' in page.text


def test_bell_does_not_break_admin_dashboard_layout():
    client = TestClient(app)
    authenticate(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    page = client.get("/admin")

    assert page.status_code == 200
    assert "Admin dashboard" in page.text
    assert 'id="notificationMenu"' in page.text
    assert 'id="moderation"' in page.text


def test_anonymous_user_has_no_bell_and_cannot_open_protected_pages():
    client = TestClient(app)

    login_page = client.get("/login")

    assert login_page.status_code == 200
    assert 'id="notificationMenu"' not in login_page.text
    assert client.get("/app").status_code == 401
    assert client.get("/app/dashboard").status_code == 401


def test_panel_contains_loading_empty_and_indicator_regions(alumni_client):
    page = alumni_client.get("/app")

    assert "Loading notifications…" in page.text
    assert 'id="notificationStatus"' in page.text
    assert 'id="notificationList"' in page.text
    assert 'data-stream-url="/api/notifications/stream"' in page.text
    assert 'aria-live="polite"' in page.text
    assert "app-notification-badge d-none" in page.text


def test_shared_script_is_included_once(alumni_client):
    page = alumni_client.get("/app")

    assert page.text.count("notifications.js") == 1


def test_script_uses_cookie_auth_sse_and_id_dedupe():
    source = JS_PATH.read_text(encoding="utf-8")

    assert "fetch(url" in source
    assert "credentials: 'same-origin'" in source
    assert "payload.notifications" in source
    assert "new EventSource(streamUrl)" in source
    assert "addEventListener('notification'" in source
    assert "event.lastEventId" in source
    assert "items.clear()" in source
    assert "items.has(item.id)" in source
    assert "items.set(item.id, item)" in source
    assert "seen.has(key)" in source
    assert "clear(list)" in source
    assert "response.status === 401 || response.status === 403" in source
    assert "closeStream()" in source
    assert "scheduleReconnect()" in source
    assert "controller.abort()" in source
    assert "X-User-Id" not in source
    assert "WebSocket" not in source
    assert "document.cookie" not in source
    assert "localStorage" not in source
    assert "unread" not in source.lower()


def test_merged_inbox_shape_still_has_no_read_status(alumni_client):
    user_id = board_auth.login(ALUMNI_EMAIL, ALUMNI_PASSWORD)["id"]
    with Session(engine) as session:
        session.add(
            Notification(
                user_id=user_id,
                event_type="listing.published",
                message="Contract check notification",
                created_at="2099-01-01T00:00:00+00:00",
                event_id="bell-contract-check",
            )
        )
        session.commit()

    response = alumni_client.get("/api/notifications")

    assert response.status_code == 200
    items = response.json()["notifications"]
    matching = [
        item for item in items if item["message"] == "Contract check notification"
    ]
    assert len(matching) == 1
    assert set(matching[0]) == {"id", "user_id", "event_type", "message", "created_at"}
    assert "unread" not in matching[0]
    assert "is_read" not in matching[0]


def test_cookie_authenticated_inbox_is_available(alumni_client):
    response = alumni_client.get("/api/notifications")

    assert response.status_code == 200
    assert set(response.json()) == {"notifications"}
    assert isinstance(response.json()["notifications"], list)


def test_header_only_inbox_request_is_rejected():
    response = TestClient(app).get("/api/notifications", headers={"X-User-Id": "1"})

    assert response.status_code == 401
