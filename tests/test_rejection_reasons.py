from csrf_test_helpers import csrf_token_from_html
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlmodel import create_engine

from app import database as database_module
from app.main import app
from app.seed import seed
from app.services import board_auth, boards
from tests.listing_test_helpers import complete_listing_payload


def setup_module() -> None:
    seed(reset=True)


client = TestClient(app)


def _login_api(email: str, password: str) -> dict:
    response = client.post(
        "/api/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200
    return response.json()["user"]


def _submit(user_id: int, kind: str, title: str) -> int:
    response = client.post(
        "/api/boards/listings",
        json=complete_listing_payload(
            {"kind": kind, "title": title, "body": "Test body"}
        ),
        headers={"X-User-Id": str(user_id)},
    )
    assert response.status_code == 200
    return response.json()["listing"]["id"]


def test_saved_rejection_reasons_are_returned_by_author():
    alum = _login_api("alumni.verified@my.uwi.edu", "alumni-dev")
    admin = _login_api("admin@uwi.edu", "admin-dev")
    expected = {
        "Backend Rejected Job": "Include the salary range.",
        "Backend Rejected Event": "Confirm the event date.",
    }

    pairs = (
        ("job", "Backend Rejected Job"),
        ("event", "Backend Rejected Event"),
    )
    for kind, title in pairs:
        listing_id = _submit(alum["id"], kind, title)
        response = client.post(
            f"/api/admin/listings/{listing_id}/reject",
            json={"rejection_reason": f"  {expected[title]}  "},
            headers={"X-User-Id": str(admin["id"])},
        )
        assert response.status_code == 200

    listings = {row["title"]: row for row in boards.list_by_author(alum["id"])}

    assert listings["Backend Rejected Job"]["rejection_reason"] == expected[
        "Backend Rejected Job"
    ]
    assert listings["Backend Rejected Event"]["rejection_reason"] == expected[
        "Backend Rejected Event"
    ]


def test_dashboard_displays_saved_rejection_reason():
    alum = board_auth.login("alumni.verified@my.uwi.edu", "alumni-dev")
    assert alum is not None
    listing_id = _submit(alum["id"], "job", "Dashboard Rejected Job")
    admin = _login_api("admin@uwi.edu", "admin-dev")
    response = client.post(
        f"/api/admin/listings/{listing_id}/reject",
        json={"rejection_reason": "Missing application instructions."},
        headers={"X-User-Id": str(admin["id"])},
    )
    assert response.status_code == 200
    csrf_token = csrf_token_from_html(client.get("/login").text)
    login = client.post(
        "/login",
        data={
            "username": "alumni.verified@my.uwi.edu",
            "password": "alumni-dev",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert login.status_code == 303

    page = client.get("/app/dashboard")

    assert page.status_code == 200
    assert "Dashboard Rejected Job" in page.text
    assert "Missing application instructions." in page.text


def test_existing_database_gains_rejection_reason_column(tmp_path, monkeypatch):
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
                "VALUES (1, 2, 'job', 'Legacy Job', 'Body', 'rejected', 'now')"
            )
        )
    monkeypatch.setattr(database_module, "engine", legacy_engine)

    database_module._ensure_listing_rejection_reason_column()
    database_module._ensure_listing_rejection_reason_column()

    columns = {
        column["name"] for column in inspect(legacy_engine).get_columns("listing")
    }
    with legacy_engine.connect() as connection:
        row = connection.execute(
            text("SELECT title, status, rejection_reason FROM listing WHERE id = 1")
        ).one()

    assert "rejection_reason" in columns
    assert tuple(row) == ("Legacy Job", "rejected", None)
