from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlmodel import create_engine

from app import database as database_module
from app.main import app
from app.seed import seed
from app.services import boards
from app.services.observer import notification_publisher
from tests.listing_test_helpers import complete_listing_payload


def setup_module() -> None:
    seed(reset=True)


client = TestClient(app)


class RecordingObserver:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)


def _login(email: str, password: str = "alumni-dev") -> dict:
    response = client.post(
        "/api/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200
    return response.json()["user"]


def _submit(user_id: int, kind: str, title: str) -> int:
    response = client.post(
        "/api/boards/listings",
        json=complete_listing_payload(
            {"kind": kind, "title": title, "body": "Original body"}
        ),
        headers={"X-User-Id": str(user_id)},
    )
    assert response.status_code == 200
    return response.json()["listing"]["id"]


def _edit(user_id: int, listing_id: int, payload: dict):
    return client.put(
        f"/api/boards/listings/{listing_id}",
        json=payload,
        headers={"X-User-Id": str(user_id)},
    )


def _request_deletion(user_id: int, listing_id: int):
    return client.post(
        f"/api/boards/listings/{listing_id}/deletion-request",
        headers={"X-User-Id": str(user_id)},
    )


def test_owner_edits_pending_listing():
    alum = _login("alumni.verified@my.uwi.edu")
    listing_id = _submit(alum["id"], "job", "Pending Edit")

    response = _edit(
        alum["id"],
        listing_id,
        {
            "kind": "event",
            "title": " Updated title ",
            "body": " Updated body ",
            "event_date": "2099-08-20",
            "event_time": "17:30",
            "location": "UWI SPEC",
            "category": "Social",
        },
    )

    assert response.status_code == 200
    listing = response.json()["listing"]
    assert listing["kind"] == "event"
    assert listing["title"] == "Updated title"
    assert listing["body"] == "Updated body"
    assert listing["status"] == "pending"
    assert listing["deletion_requested"] is False


def test_editing_rejected_listing_resubmits_and_clears_reason():
    alum = _login("alumni.verified@my.uwi.edu")
    admin = _login("admin@uwi.edu", "admin-dev")
    listing_id = _submit(alum["id"], "event", "Rejected Edit")
    rejected = client.post(
        f"/api/admin/listings/{listing_id}/reject",
        json={"rejection_reason": "Needs a confirmed date."},
        headers={"X-User-Id": str(admin["id"])},
    )
    assert rejected.status_code == 200

    response = _edit(
        alum["id"],
        listing_id,
        {"kind": "event", "title": "Confirmed Event", "body": "Date confirmed"},
    )

    assert response.status_code == 200
    assert response.json()["listing"]["status"] == "pending"
    assert response.json()["listing"]["rejection_reason"] is None


def test_edit_rejects_published_other_user_invalid_and_protected_changes():
    alum = _login("alumni.verified@my.uwi.edu")
    admin = _login("admin@uwi.edu", "admin-dev")
    listing_id = _submit(alum["id"], "job", "Guarded Edit")
    payload = {"kind": "job", "title": "New title", "body": "New body"}

    assert _edit(admin["id"], listing_id, payload).status_code == 400
    assert _edit(alum["id"], listing_id, {**payload, "kind": "course"}).status_code == 400
    assert _edit(alum["id"], listing_id, {**payload, "title": "  "}).status_code == 400
    assert _edit(alum["id"], listing_id, {**payload, "status": "published"}).status_code == 422
    assert client.post(
        f"/api/admin/listings/{listing_id}/publish",
        headers={"X-User-Id": str(admin["id"])},
    ).status_code == 200
    assert _edit(alum["id"], listing_id, payload).status_code == 400
    assert _edit(alum["id"], 999999, payload).status_code == 400
    assert client.put(f"/api/boards/listings/{listing_id}", json=payload).status_code == 401


def test_published_deletion_request_stays_visible_and_cannot_duplicate():
    alum = _login("alumni.verified@my.uwi.edu")
    admin = _login("admin@uwi.edu", "admin-dev")
    listing_id = _submit(alum["id"], "job", "Visible Deletion Request")
    assert client.post(
        f"/api/admin/listings/{listing_id}/publish",
        headers={"X-User-Id": str(admin["id"])},
    ).status_code == 200

    response = _request_deletion(alum["id"], listing_id)

    assert response.status_code == 200
    assert response.json()["listing"]["deletion_requested"] is True
    public_ids = [row["id"] for row in client.get("/api/boards").json()["listings"]]
    assert listing_id in public_ids
    own = {row["id"]: row for row in boards.list_by_author(alum["id"])}
    assert own[listing_id]["deletion_requested"] is True
    assert _request_deletion(alum["id"], listing_id).status_code == 400
    assert _request_deletion(admin["id"], listing_id).status_code == 400
    login = client.post(
        "/login",
        data={
            "username": "alumni.verified@my.uwi.edu",
            "password": "alumni-dev",
        },
        follow_redirects=False,
    )
    assert login.status_code == 303
    dashboard = client.get("/app/dashboard")
    assert "Deletion requested — awaiting admin review" in dashboard.text


def test_admin_lists_and_denies_deletion_request():
    alum = _login("alumni.verified@my.uwi.edu")
    admin = _login("admin@uwi.edu", "admin-dev")
    listing_id = _submit(alum["id"], "event", "Denied Deletion")
    assert _request_deletion(alum["id"], listing_id).status_code == 200

    denied_list = client.get(
        "/api/admin/listings/deletion-requests",
        headers={"X-User-Id": str(alum["id"])},
    )
    denied_approval = client.post(
        f"/api/admin/listings/{listing_id}/deletion-request/approve",
        headers={"X-User-Id": str(alum["id"])},
    )
    denied_denial = client.post(
        f"/api/admin/listings/{listing_id}/deletion-request/deny",
        headers={"X-User-Id": str(alum["id"])},
    )
    requests = client.get(
        "/api/admin/listings/deletion-requests",
        headers={"X-User-Id": str(admin["id"])},
    )
    denial = client.post(
        f"/api/admin/listings/{listing_id}/deletion-request/deny",
        headers={"X-User-Id": str(admin["id"])},
    )

    assert denied_list.status_code == 403
    assert denied_approval.status_code == 400
    assert denied_denial.status_code == 400
    assert listing_id in [row["id"] for row in requests.json()["listings"]]
    assert denial.status_code == 200
    assert denial.json()["listing"]["deletion_requested"] is False
    assert client.post(
        f"/api/admin/listings/{listing_id}/deletion-request/deny",
        headers={"X-User-Id": str(admin["id"])},
    ).status_code == 400


def test_admin_approves_deletion_and_removes_listing():
    alum = _login("alumni.verified@my.uwi.edu")
    admin = _login("admin@uwi.edu", "admin-dev")
    listing_id = _submit(alum["id"], "job", "Approved Deletion")
    assert _request_deletion(alum["id"], listing_id).status_code == 200

    response = client.post(
        f"/api/admin/listings/{listing_id}/deletion-request/approve",
        headers={"X-User-Id": str(admin["id"])},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "listing_id": listing_id, "deleted": True}
    assert listing_id not in [row["id"] for row in boards.list_by_author(alum["id"])]
    assert client.post(
        f"/api/admin/listings/{listing_id}/deletion-request/approve",
        headers={"X-User-Id": str(admin["id"])},
    ).status_code == 400


def test_listing_actions_do_not_emit_notifications():
    alum = _login("alumni.verified@my.uwi.edu")
    admin = _login("admin@uwi.edu", "admin-dev")
    observer = RecordingObserver()
    notification_publisher.subscribe(observer)
    try:
        listing_id = _submit(alum["id"], "job", "Quiet Actions")
        assert _edit(
            alum["id"],
            listing_id,
            {"kind": "job", "title": "Quiet Edit", "body": "Quiet body"},
        ).status_code == 200
        assert _request_deletion(alum["id"], listing_id).status_code == 200
        assert client.post(
            f"/api/admin/listings/{listing_id}/deletion-request/deny",
            headers={"X-User-Id": str(admin["id"])},
        ).status_code == 200
        approved_id = _submit(alum["id"], "event", "Quiet Approval")
        assert _request_deletion(alum["id"], approved_id).status_code == 200
        assert client.post(
            f"/api/admin/listings/{approved_id}/deletion-request/approve",
            headers={"X-User-Id": str(admin["id"])},
        ).status_code == 200
    finally:
        notification_publisher.unsubscribe(observer)

    assert observer.events == []


def test_existing_database_gains_deletion_requested_column(tmp_path, monkeypatch):
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE listing ("
                "id INTEGER PRIMARY KEY, author_id INTEGER, kind VARCHAR, "
                "title VARCHAR, body VARCHAR, status VARCHAR, "
                "rejection_reason VARCHAR, created_at VARCHAR)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO listing "
                "(id, author_id, kind, title, body, status, created_at) "
                "VALUES (1, 2, 'job', 'Legacy Job', 'Body', 'published', 'now')"
            )
        )
    monkeypatch.setattr(database_module, "engine", legacy_engine)

    database_module._ensure_listing_deletion_requested_column()
    database_module._ensure_listing_deletion_requested_column()

    columns = {
        column["name"] for column in inspect(legacy_engine).get_columns("listing")
    }
    with legacy_engine.connect() as connection:
        row = connection.execute(
            text("SELECT title, status, deletion_requested FROM listing WHERE id = 1")
        ).one()

    assert "deletion_requested" in columns
    assert tuple(row) == ("Legacy Job", "published", 0)
