import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlmodel import create_engine

from app import database as database_module
from app.main import app
from app.seed import seed
from tests.listing_auth_helpers import auth_headers, browse_as_verified, login
from tests.listing_test_helpers import complete_listing_payload


def setup_module() -> None:
    seed(reset=True)


client = TestClient(app)


def _login(email: str, password: str = "alumni-dev") -> dict:
    return login(client, email, password)


def _submit(user_id: int, payload: dict):
    return client.post(
        "/api/boards/listings",
        json=payload,
        headers=auth_headers(client, user_id),
    )


def _publish(admin_id: int, listing_id: int) -> None:
    response = client.post(
        f"/api/admin/listings/{listing_id}/publish",
        headers=auth_headers(client, admin_id),
    )
    assert response.status_code == 200


def test_job_submission_saves_required_fields_and_optional_deadline():
    alum = _login("alumni.verified@my.uwi.edu")
    payload = {
        "kind": "job",
        "title": "Data Analyst",
        "body": "Analyse graduate outcomes.",
        "company_name": "UWI Analytics",
        "location": "Mona",
        "application_instructions": "Apply at https://example.com/jobs/1",
        "application_deadline": "2099-07-31",
        "faculty": "Social Sciences",
        "department": "Economics",
    }

    response = _submit(alum["id"], payload)

    assert response.status_code == 200
    listing = response.json()["listing"]
    for field, value in payload.items():
        assert listing[field] == value
    assert listing["status"] == "pending"

    without_deadline = payload.copy()
    without_deadline.pop("application_deadline")
    assert _submit(alum["id"], without_deadline).status_code == 200


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("company_name", "  "),
        ("location", None),
        ("application_instructions", ""),
        ("faculty", None),
    ],
)
def test_job_submission_rejects_missing_required_details(field, value):
    alum = _login("alumni.verified@my.uwi.edu")
    payload = complete_listing_payload(
        {"kind": "job", "title": "Incomplete Job", "body": "Body"}
    )
    payload[field] = value
    if field == "faculty":
        payload["department"] = None

    response = _submit(alum["id"], payload)

    assert response.status_code == 400


def test_job_submission_rejects_invalid_deadline():
    alum = _login("alumni.verified@my.uwi.edu")
    payload = complete_listing_payload(
        {"kind": "job", "title": "Bad Deadline", "body": "Body"}
    )
    payload["application_deadline"] = "31/07/2099"

    response = _submit(alum["id"], payload)

    assert response.status_code == 400
    assert "YYYY-MM-DD" in response.json()["detail"]


def test_event_submission_saves_fields_and_validates_date_and_time():
    alum = _login("alumni.verified@my.uwi.edu")
    payload = {
        "kind": "event",
        "title": "Alumni Workshop",
        "body": "A practical career workshop.",
        "event_date": "2099-09-10",
        "event_time": "13:30",
        "location": "UWI Teaching Complex",
        "category": "Workshop",
        "registration_information": "Register at https://example.com/events/1",
    }

    response = _submit(alum["id"], payload)

    assert response.status_code == 200
    listing = response.json()["listing"]
    for field, value in payload.items():
        assert listing[field] == value

    for field, invalid in (("event_date", "10-09-2099"), ("event_time", "evening")):
        invalid_payload = payload.copy()
        invalid_payload[field] = invalid
        assert _submit(alum["id"], invalid_payload).status_code == 400

    missing_location = payload.copy()
    missing_location["location"] = "  "
    assert _submit(alum["id"], missing_location).status_code == 400


def test_edit_updates_new_fields_and_preserves_omitted_values():
    alum = _login("alumni.verified@my.uwi.edu")
    payload = complete_listing_payload(
        {"kind": "job", "title": "Original Job", "body": "Original body"}
    )
    payload["application_deadline"] = "2099-11-30"
    listing_id = _submit(alum["id"], payload).json()["listing"]["id"]

    response = client.put(
        f"/api/boards/listings/{listing_id}",
        json={
            "kind": "job",
            "title": "Updated Job",
            "body": "Updated body",
            "company_name": "Updated Company",
        },
        headers=auth_headers(client, alum["id"]),
    )

    assert response.status_code == 200
    listing = response.json()["listing"]
    assert listing["company_name"] == "Updated Company"
    assert listing["location"] == payload["location"]
    assert listing["application_deadline"] == "2099-11-30"


def test_category_faculty_and_department_filters_remain_published_only():
    alum = _login("alumni.verified@my.uwi.edu")
    admin = _login("admin@uwi.edu", "admin-dev")
    job = complete_listing_payload(
        {"kind": "job", "title": "Engineering Graduate Role", "body": "Robotics"}
    )
    job["faculty"] = "Engineering"
    job["department"] = "Electrical Engineering"
    event = complete_listing_payload(
        {"kind": "event", "title": "Engineering Networking Night", "body": "Meet employers"}
    )
    event["category"] = "Networking"
    job_id = _submit(alum["id"], job).json()["listing"]["id"]
    event_id = _submit(alum["id"], event).json()["listing"]["id"]
    hidden_id = _submit(alum["id"], job).json()["listing"]["id"]
    _publish(admin["id"], job_id)
    _publish(admin["id"], event_id)

    by_job_category = browse_as_verified(
        client, "/api/boards", {"kind": "job", "category": "Engineering"}
    )
    combined = browse_as_verified(
        client,
        "/api/boards",
        {"q": "Engineering", "kind": "event", "category": "Networking"},
    )
    by_department = browse_as_verified(
        client, "/api/boards", {"department": "Electrical Engineering"}
    )

    assert [row["id"] for row in by_job_category] == [job_id]
    assert [row["id"] for row in combined] == [event_id]
    assert job_id in [row["id"] for row in by_department]
    assert hidden_id not in [row["id"] for row in by_department]


def test_legacy_database_gains_listing_fields_without_losing_rows(tmp_path, monkeypatch):
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
                "VALUES (4, 2, 'job', 'Legacy Job', 'Body', 'published', 'now')"
            )
        )
    monkeypatch.setattr(database_module, "engine", legacy_engine)

    database_module._ensure_listing_detail_columns()
    database_module._ensure_listing_detail_columns()

    columns = {column["name"] for column in inspect(legacy_engine).get_columns("listing")}
    with legacy_engine.connect() as connection:
        row = connection.execute(
            text("SELECT id, title, company_name, event_date FROM listing WHERE id = 4")
        ).one()

    assert {"company_name", "faculty", "event_date", "category"} <= columns
    assert tuple(row) == (4, "Legacy Job", None, None)
