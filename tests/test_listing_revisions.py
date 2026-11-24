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


def _published_listing(author_id: int, admin_id: int, kind: str, title: str) -> dict:
    response = client.post(
        "/api/boards/listings",
        json=complete_listing_payload(
            {"kind": kind, "title": title, "body": "Original body"}
        ),
        headers={"X-User-Id": str(author_id)},
    )
    assert response.status_code == 200
    listing = response.json()["listing"]
    published = client.post(
        f"/api/admin/listings/{listing['id']}/publish",
        headers={"X-User-Id": str(admin_id)},
    )
    assert published.status_code == 200
    return published.json()["listing"]


def _revision(user_id: int, listing_id: int, payload: dict):
    return client.post(
        f"/api/boards/listings/{listing_id}/revision-request",
        json=payload,
        headers={"X-User-Id": str(user_id)},
    )


def _decision(admin_id: int, listing_id: int, decision: str):
    return client.post(
        f"/api/admin/listings/{listing_id}/revision-request/{decision}",
        headers={"X-User-Id": str(admin_id)},
    )


def test_author_requests_revision_without_changing_public_listing():
    author = _login("alumni.verified@my.uwi.edu")
    admin = _login("admin@uwi.edu", "admin-dev")
    listing = _published_listing(author["id"], admin["id"], "job", "Original Job")
    payload = {
        "kind": "job",
        "title": "Proposed Job",
        "body": "Proposed body",
        "company_name": "Proposed Company",
        "category": "Technology",
    }

    response = _revision(author["id"], listing["id"], payload)
    public = {row["id"]: row for row in client.get("/api/boards").json()["listings"]}
    assert client.post(
        "/login",
        data={
            "username": "alumni.verified@my.uwi.edu",
            "password": "alumni-dev",
        },
        follow_redirects=False,
    ).status_code == 303
    public_page = client.get("/app", params={"q": "Original Job"})
    own = {row["id"]: row for row in boards.list_by_author(author["id"])}
    requests = client.get(
        "/api/admin/listings/revision-requests",
        headers={"X-User-Id": str(admin["id"])},
    ).json()["revision_requests"]
    request = next(row for row in requests if row["listing_id"] == listing["id"])

    assert response.status_code == 200
    assert response.json()["revision"]["status"] == "pending"
    assert public[listing["id"]]["title"] == "Original Job"
    assert public[listing["id"]]["body"] == "Original body"
    assert "Original Job" in public_page.text
    assert "Proposed Job" not in public_page.text
    assert own[listing["id"]]["revision_requested"] is True
    assert request["original"]["title"] == "Original Job"
    assert request["proposed"]["title"] == "Proposed Job"
    assert request["proposed"]["category"] == "Technology"
    assert request["proposed"]["location"] == listing["location"]
    assert request["requesting_author_id"] == author["id"]


def test_admin_approval_applies_revision_and_keeps_listing_published():
    author = _login("alumni.verified@my.uwi.edu")
    admin = _login("admin@uwi.edu", "admin-dev")
    listing = _published_listing(author["id"], admin["id"], "event", "Old Event")
    payload = {
        "kind": "event",
        "title": "Approved Event",
        "body": "Approved body",
        "event_date": "2099-10-20",
        "event_time": "19:00",
        "location": "UWI SPEC",
        "category": "Careers",
        "registration_information": None,
    }
    assert _revision(author["id"], listing["id"], payload).status_code == 200

    response = _decision(admin["id"], listing["id"], "approve")
    public = {row["id"]: row for row in client.get("/api/boards").json()["listings"]}
    own = {row["id"]: row for row in boards.list_by_author(author["id"])}

    assert response.status_code == 200
    assert response.json()["listing"]["status"] == "published"
    assert response.json()["listing"]["registration_information"] is None
    assert public[listing["id"]]["title"] == "Approved Event"
    assert public[listing["id"]]["category"] == "Careers"
    assert own[listing["id"]]["revision_requested"] is False
    assert _decision(admin["id"], listing["id"], "approve").status_code == 400


def test_admin_denial_preserves_original_values():
    author = _login("alumni.verified@my.uwi.edu")
    admin = _login("admin@uwi.edu", "admin-dev")
    listing = _published_listing(author["id"], admin["id"], "job", "Kept Job")
    payload = {"kind": "job", "title": "Denied Job", "body": "Denied body"}
    assert _revision(author["id"], listing["id"], payload).status_code == 200

    response = _decision(admin["id"], listing["id"], "deny")
    public = {row["id"]: row for row in client.get("/api/boards").json()["listings"]}

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "listing_id": listing["id"],
        "status": "denied",
    }
    assert public[listing["id"]]["title"] == "Kept Job"
    assert _decision(admin["id"], listing["id"], "deny").status_code == 400


def test_revision_request_rejects_wrong_owner_status_invalid_and_duplicate():
    author = _login("alumni.verified@my.uwi.edu")
    registered = client.post(
        "/api/auth/register",
        json={
            "email": "other.revision@my.uwi.edu",
            "password": "other-dev",
            "display_name": "Other Alum",
        },
    ).json()["user"]
    assert client.post(
        "/api/auth/verify-email",
        headers={"X-User-Id": str(registered["id"])},
    ).status_code == 200
    other = _login("other.revision@my.uwi.edu", "other-dev")
    admin = _login("admin@uwi.edu", "admin-dev")
    published = _published_listing(author["id"], admin["id"], "job", "Guarded Job")
    payload = {"kind": "job", "title": "New Job", "body": "New body"}

    pending_response = client.post(
        "/api/boards/listings",
        json=complete_listing_payload(
            {"kind": "job", "title": "Pending Job", "body": "Body"}
        ),
        headers={"X-User-Id": str(author["id"])},
    )
    pending_id = pending_response.json()["listing"]["id"]
    rejected_id = client.post(
        "/api/boards/listings",
        json=complete_listing_payload(
            {"kind": "event", "title": "Rejected Event", "body": "Body"}
        ),
        headers={"X-User-Id": str(author["id"])},
    ).json()["listing"]["id"]
    assert client.post(
        f"/api/admin/listings/{rejected_id}/reject",
        headers={"X-User-Id": str(admin["id"])},
    ).status_code == 200

    assert _revision(other["id"], published["id"], payload).status_code == 400
    assert _revision(author["id"], pending_id, payload).status_code == 400
    assert _revision(author["id"], rejected_id, payload).status_code == 400
    assert _revision(
        author["id"], published["id"], {**payload, "title": "   "}
    ).status_code == 400
    assert _revision(
        author["id"], published["id"], {**payload, "status": "published"}
    ).status_code == 422
    assert _revision(author["id"], published["id"], payload).status_code == 200
    assert _revision(author["id"], published["id"], payload).status_code == 400
    assert _revision(author["id"], 999999, payload).status_code == 400


def test_revision_and_deletion_requests_cannot_overlap():
    author = _login("alumni.verified@my.uwi.edu")
    admin = _login("admin@uwi.edu", "admin-dev")
    revised = _published_listing(author["id"], admin["id"], "job", "Revision First")
    deleting = _published_listing(author["id"], admin["id"], "event", "Deletion First")
    job_payload = {"kind": "job", "title": "Changed", "body": "Changed body"}
    event_payload = {"kind": "event", "title": "Changed", "body": "Changed body"}

    assert _revision(author["id"], revised["id"], job_payload).status_code == 200
    assert client.post(
        f"/api/boards/listings/{revised['id']}/deletion-request",
        headers={"X-User-Id": str(author["id"])},
    ).status_code == 400
    assert client.post(
        f"/api/boards/listings/{deleting['id']}/deletion-request",
        headers={"X-User-Id": str(author["id"])},
    ).status_code == 200
    assert _revision(author["id"], deleting["id"], event_payload).status_code == 400


def test_only_admin_can_review_revisions():
    author = _login("alumni.verified@my.uwi.edu")
    admin = _login("admin@uwi.edu", "admin-dev")
    listing = _published_listing(author["id"], admin["id"], "job", "Admin Only")
    payload = {"kind": "job", "title": "Proposed", "body": "Proposed body"}
    assert _revision(author["id"], listing["id"], payload).status_code == 200

    assert client.get(
        "/api/admin/listings/revision-requests",
        headers={"X-User-Id": str(author["id"])},
    ).status_code == 403
    assert _decision(author["id"], listing["id"], "approve").status_code == 400
    assert _decision(author["id"], listing["id"], "deny").status_code == 400
    assert _decision(admin["id"], 999999, "approve").status_code == 400


def test_revision_actions_do_not_emit_notifications():
    author = _login("alumni.verified@my.uwi.edu")
    admin = _login("admin@uwi.edu", "admin-dev")
    approved = _published_listing(author["id"], admin["id"], "job", "Quiet Approval")
    denied = _published_listing(author["id"], admin["id"], "event", "Quiet Denial")
    observer = RecordingObserver()
    notification_publisher.subscribe(observer)
    try:
        assert _revision(
            author["id"],
            approved["id"],
            {"kind": "job", "title": "Quiet Edit", "body": "Quiet body"},
        ).status_code == 200
        assert _decision(admin["id"], approved["id"], "approve").status_code == 200
        assert _revision(
            author["id"],
            denied["id"],
            {"kind": "event", "title": "Quiet Edit", "body": "Quiet body"},
        ).status_code == 200
        assert _decision(admin["id"], denied["id"], "deny").status_code == 200
    finally:
        notification_publisher.unsubscribe(observer)

    assert observer.events == []


def test_existing_database_gains_revision_table_safely(tmp_path, monkeypatch):
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE listing ("
                "id INTEGER PRIMARY KEY, author_id INTEGER, kind VARCHAR, "
                "title VARCHAR, body VARCHAR, status VARCHAR, created_at VARCHAR)"
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

    database_module.create_db_and_tables()
    database_module.create_db_and_tables()

    tables = set(inspect(legacy_engine).get_table_names())
    with legacy_engine.connect() as connection:
        listing = connection.execute(
            text("SELECT id, title, status FROM listing WHERE id = 1")
        ).one()

    assert "listingrevision" in tables
    assert tuple(listing) == (1, "Legacy Job", "published")
