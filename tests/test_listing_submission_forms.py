import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import seed
from app.services import board_auth, boards
from tests.listing_auth_helpers import login

ALUMNI_EMAIL = "alumni.verified@my.uwi.edu"
UNVERIFIED_EMAIL = "alumni.pending@gmail.com"
PASSWORD = "alumni-dev"


@pytest.fixture
def client():
    seed(reset=True)
    return TestClient(app)


def _login(client: TestClient, email: str = ALUMNI_EMAIL) -> TestClient:
    login(client, email, PASSWORD)
    return client


def _alumni_id() -> int:
    return board_auth.login(ALUMNI_EMAIL, PASSWORD)["id"]


def _job_data(**overrides):
    data = {
        "kind": "job",
        "title": "Graduate Developer",
        "body": "Build and maintain graduate services.",
        "location": "Mona",
        "category": "Engineering",
        "company_name": "UWI Labs",
        "application_instructions": "Apply at https://example.com/apply",
        "application_deadline": "2099-07-31",
        "faculty": "Science and Technology",
        "department": "Computer Science",
    }
    data.update(overrides)
    return data


def _event_data(**overrides):
    data = {
        "kind": "event",
        "title": "Alumni Career Workshop",
        "body": "A practical workshop for final-year students.",
        "location": "Teaching Complex",
        "category": "Workshop",
        "event_date": "2099-09-10",
        "event_time": "13:30",
        "registration_information": "Register at https://example.com/events/1",
    }
    data.update(overrides)
    return data


def test_authenticated_alumnus_can_open_submission_forms(client):
    page = _login(client).get("/app/dashboard")

    assert page.status_code == 200
    assert "Submit Listing" in page.text
    assert 'id="listingSubmissionForm"' in page.text
    assert "action=" in page.text
    assert "/app/dashboard/submit" in page.text
    assert 'data-edit-id="' in page.text
    assert 'data-delete-id="' in page.text


def test_unauthenticated_user_cannot_submit(client):
    csrf_token = client.get("/api/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/app/dashboard/submit",
        data=_job_data(),
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 401


def test_unverified_alumnus_cannot_submit(client):
    _login(client, UNVERIFIED_EMAIL)
    response = client.post("/app/dashboard/submit", data=_job_data())

    assert response.status_code == 403


def test_job_fields_render_with_backend_field_names(client):
    page = _login(client).get("/app/dashboard")
    job_fields = re.search(
        r'<fieldset id="jobFields".*?</fieldset>', page.text, re.DOTALL
    ).group(0)

    for field in (
        "company_name",
        "application_instructions",
        "application_deadline",
        "faculty",
        "department",
    ):
        assert f'name="{field}"' in job_fields
    assert 'name="event_date"' not in job_fields
    assert 'name="event_time"' not in job_fields
    assert 'name="registration_information"' not in job_fields
    assert 'type="date"' in job_fields
    assert 'name="location"' in page.text
    assert 'name="category"' in page.text

    for field in ("title", "body", "location", "company_name", "application_instructions"):
        tag = re.search(rf'<[^>]*name="{field}"[^>]*>', page.text).group(0)
        assert "required" in tag
    faculty_tag = re.search(r'<[^>]*name="faculty"[^>]*>', job_fields).group(0)
    department_tag = re.search(r'<[^>]*name="department"[^>]*>', job_fields).group(0)
    assert "required" not in faculty_tag
    assert "required" not in department_tag


def test_event_fields_render_with_backend_field_names(client):
    page = _login(client).get("/app/dashboard")
    event_fields = re.search(
        r'<fieldset id="eventFields".*?</fieldset>', page.text, re.DOTALL
    ).group(0)

    for field in ("event_date", "event_time", "registration_information"):
        assert f'name="{field}"' in event_fields
    assert 'name="company_name"' not in event_fields
    assert 'name="application_instructions"' not in event_fields
    assert 'name="faculty"' not in event_fields
    assert 'type="date"' in event_fields
    assert 'type="time"' in event_fields

    for field in ("event_date", "event_time"):
        tag = re.search(rf'<[^>]*name="{field}"[^>]*>', event_fields).group(0)
        assert "required" in tag


def test_job_form_sends_exact_payload_without_user_controlled_fields(client, monkeypatch):
    seen = {}

    def fake_submit(author_id, **kwargs):
        seen["author_id"] = author_id
        seen["payload"] = kwargs
        return {"ok": True, "listing": {}}

    monkeypatch.setattr("app.routers.user_home.boards.submit_listing", fake_submit)
    response = _login(client).post(
        "/app/dashboard/submit",
        data=_job_data(author_id="999", status="published"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert seen["author_id"] == _alumni_id()
    assert seen["payload"] == {
        "kind": "job",
        "title": "Graduate Developer",
        "body": "Build and maintain graduate services.",
        "company_name": "UWI Labs",
        "location": "Mona",
        "application_instructions": "Apply at https://example.com/apply",
        "application_deadline": "2099-07-31",
        "faculty": "Science and Technology",
        "department": "Computer Science",
        "category": "Engineering",
    }
    assert "author_id" not in seen["payload"]
    assert "status" not in seen["payload"]


def test_event_form_sends_only_event_fields(client, monkeypatch):
    seen = {}

    def fake_submit(author_id, **kwargs):
        seen["author_id"] = author_id
        seen["payload"] = kwargs
        return {"ok": True, "listing": {}}

    monkeypatch.setattr("app.routers.user_home.boards.submit_listing", fake_submit)
    response = _login(client).post(
        "/app/dashboard/submit",
        data=_event_data(company_name="Should Be Ignored", faculty="Ignored"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert seen["author_id"] == _alumni_id()
    assert seen["payload"] == {
        "kind": "event",
        "title": "Alumni Career Workshop",
        "body": "A practical workshop for final-year students.",
        "event_date": "2099-09-10",
        "event_time": "13:30",
        "location": "Teaching Complex",
        "category": "Workshop",
        "registration_information": "Register at https://example.com/events/1",
    }


def test_successful_job_submission_returns_to_dashboard_as_pending(client):
    logged_in = _login(client)
    response = logged_in.post(
        "/app/dashboard/submit",
        data=_job_data(title="Unique Graduate Role"),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Listing submitted for review." in response.text
    assert "Unique Graduate Role" in response.text
    row = next(
        row
        for row in boards.list_by_author(_alumni_id())
        if row["title"] == "Unique Graduate Role"
    )
    assert row["status"] == "pending"
    public_ids = [row["id"] for row in logged_in.get("/api/boards").json()["listings"]]
    assert row["id"] not in public_ids


def test_successful_event_submission_returns_to_dashboard_as_pending(client):
    logged_in = _login(client)
    response = logged_in.post(
        "/app/dashboard/submit",
        data=_event_data(title="Unique Alumni Event"),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Listing submitted for review." in response.text
    assert "Unique Alumni Event" in response.text
    row = next(
        row
        for row in boards.list_by_author(_alumni_id())
        if row["title"] == "Unique Alumni Event"
    )
    assert row["status"] == "pending"
    public_ids = [row["id"] for row in logged_in.get("/api/boards").json()["listings"]]
    assert row["id"] not in public_ids


def test_optional_fields_can_be_omitted(client):
    logged_in = _login(client)
    job = _job_data(title="Job Without Extras")
    job.pop("application_deadline")
    job.pop("category")
    event = _event_data(title="Event Without Extras")
    event.pop("registration_information")
    event.pop("category")

    assert (
        logged_in.post(
            "/app/dashboard/submit", data=job, follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        logged_in.post(
            "/app/dashboard/submit", data=event, follow_redirects=False
        ).status_code
        == 303
    )


def test_invalid_dates_show_error_and_preserve_values(client):
    response = _login(client).post(
        "/app/dashboard/submit",
        data=_job_data(application_deadline="31/07/2099"),
    )

    assert response.status_code == 200
    assert "YYYY-MM-DD" in response.text
    assert 'value="31/07/2099"' in response.text
    assert "Listing submitted for review." not in response.text


def test_event_time_validation_shows_error(client):
    response = _login(client).post(
        "/app/dashboard/submit",
        data=_event_data(event_time="evening"),
    )

    assert response.status_code == 200
    assert "event_time must use HH:MM or HH:MM:SS" in response.text
    assert "Listing submitted for review." not in response.text


def test_whitespace_description_shows_error(client):
    response = _login(client).post(
        "/app/dashboard/submit",
        data=_job_data(body="   "),
    )

    assert response.status_code == 200
    assert "title and body are required" in response.text
    assert "Listing submitted for review." not in response.text


def test_backend_rejection_preserves_input_without_success(client, monkeypatch):
    def failed_submit(author_id, **kwargs):
        return {"ok": False, "error": "company_name is required for job listings"}

    monkeypatch.setattr("app.routers.user_home.boards.submit_listing", failed_submit)
    response = _login(client).post(
        "/app/dashboard/submit",
        data=_job_data(company_name="Acme"),
    )

    assert response.status_code == 200
    assert "company_name is required for job listings" in response.text
    assert 'value="Acme"' in response.text
    assert "Listing submitted for review." not in response.text


def test_submission_does_not_trigger_notifications(client, monkeypatch):
    notified = []
    monkeypatch.setattr(
        "app.services.boards.notification_publisher.notify",
        lambda event: notified.append(event),
    )

    response = _login(client).post(
        "/app/dashboard/submit",
        data=_job_data(title="No Notification Job"),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert notified == []
