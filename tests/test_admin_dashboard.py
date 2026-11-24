import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import seed
from app.services import board_auth, boards, campaigns
from app.services.website_notifications import register_website_observer
from tests.campaign_test_helpers import isolate_publisher

ADMIN_EMAIL = "admin@uwi.edu"
ADMIN_PASSWORD = "admin-dev"
ALUMNI_EMAIL = "alumni.verified@my.uwi.edu"
ALUMNI_PASSWORD = "alumni-dev"


@pytest.fixture(scope="module", autouse=True)
def seeded_database():
    seed(reset=True)


def _login(email, password):
    client = TestClient(app)
    response = client.post(
        "/login",
        data={"username": email, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


@pytest.fixture
def admin_client():
    return _login(ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture
def alumni_client():
    return _login(ALUMNI_EMAIL, ALUMNI_PASSWORD)


def _alumni_id():
    return board_auth.login(ALUMNI_EMAIL, ALUMNI_PASSWORD)["id"]


def _admin_id():
    return board_auth.login(ADMIN_EMAIL, ADMIN_PASSWORD)["id"]


def _submit_listing(client, kind, title, **extra):
    data = {
        "kind": kind,
        "title": title,
        "body": "Detailed description for admin review.",
        "location": "St. Augustine",
    }
    if kind == "job":
        data.update(
            {
                "company_name": "Campus Employer",
                "application_instructions": "Apply through the portal",
                "faculty": "Science and Technology",
                "department": "Computing",
                "application_deadline": "2099-07-01",
                "category": "Engineering",
            }
        )
    else:
        data.update(
            {
                "event_date": "2099-06-15",
                "event_time": "18:00",
                "category": "Networking",
                "registration_information": "Register online",
            }
        )
    data.update(extra)
    response = client.post("/app/dashboard/submit", data=data, follow_redirects=False)
    assert response.status_code == 303
    rows = [row for row in boards.list_by_author(_alumni_id()) if row["title"] == title]
    return rows[-1]["id"]


def _request_deletion(listing_id):
    response = TestClient(app).post(
        f"/api/boards/listings/{listing_id}/deletion-request",
        headers={"X-User-Id": str(_alumni_id())},
    )
    assert response.status_code == 200


def _create_campaign(client, subject="Demo campaign", body="Alumni update"):
    response = client.post(
        "/admin/campaigns",
        data={"subject": subject, "body": body},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return next(
        row for row in campaigns.list_campaigns() if row["subject"] == subject
    )


def test_admin_dashboard_requires_admin_session(admin_client, alumni_client):
    assert TestClient(app).get("/admin").status_code == 401
    assert alumni_client.get("/admin").status_code == 401
    assert (
        alumni_client.post("/admin/listings/999999/publish", follow_redirects=False).status_code
        == 401
    )
    assert (
        alumni_client.post(
            "/admin/campaigns",
            data={"subject": "Blocked", "body": "Blocked"},
            follow_redirects=False,
        ).status_code
        == 401
    )

    page = admin_client.get("/admin")
    assert page.status_code == 200
    assert "Admin dashboard" in page.text


def test_empty_states_render(admin_client, monkeypatch):
    monkeypatch.setattr("app.routers.admin_home.boards.list_pending", list)
    monkeypatch.setattr(
        "app.routers.admin_home.boards.list_deletion_requests",
        lambda admin_id: {"ok": True, "listings": []},
    )
    monkeypatch.setattr("app.routers.admin_home.campaigns.list_campaigns", list)

    page = admin_client.get("/admin")

    assert "No job or event listings are awaiting review." in page.text
    assert "No deletion requests are awaiting review." in page.text
    assert "No campaigns have been created yet." in page.text


def test_pending_listings_show_expanded_fields(admin_client, alumni_client):
    _submit_listing(alumni_client, "job", "Graduate Developer Role")
    _submit_listing(alumni_client, "event", "Alumni Networking Night")

    page = admin_client.get("/admin")

    assert page.status_code == 200
    for text in (
        "Graduate Developer Role",
        "Campus Employer",
        "Science and Technology",
        "Computing",
        "2099-07-01",
        "Apply through the portal",
        "Alumni Networking Night",
        "2099-06-15",
        "18:00",
        "Register online",
        "Verified Alum",
        "name=\"rejection_reason\"",
        "Approve",
        "Reject",
    ):
        assert text in page.text


def test_admin_approves_pending_listing(admin_client, alumni_client):
    listing_id = _submit_listing(alumni_client, "job", "Approval Demo Role")

    response = admin_client.post(
        f"/admin/listings/{listing_id}/publish", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin")
    row = next(row for row in boards.list_by_author(_alumni_id()) if row["id"] == listing_id)
    assert row["status"] == "published"

    page = admin_client.get("/admin")
    assert f"Listing #{listing_id} approved and published." in page.text
    assert "Approval Demo Role" not in page.text


def test_admin_rejects_with_reason(admin_client, alumni_client):
    listing_id = _submit_listing(alumni_client, "event", "Rejection Demo Event")

    response = admin_client.post(
        f"/admin/listings/{listing_id}/reject",
        data={"rejection_reason": "  Please add a confirmed venue.  "},
        follow_redirects=False,
    )

    assert response.status_code == 303
    row = next(row for row in boards.list_by_author(_alumni_id()) if row["id"] == listing_id)
    assert row["status"] == "rejected"
    assert row["rejection_reason"] == "Please add a confirmed venue."
    assert f"Listing #{listing_id} rejected." in admin_client.get("/admin").text


def test_backend_error_is_displayed(admin_client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin_home.boards.publish_listing",
        lambda admin_id, listing_id: {"ok": False, "error": "listing is not pending"},
    )

    response = admin_client.post("/admin/listings/999999/publish", follow_redirects=False)

    assert response.status_code == 303
    page = admin_client.get("/admin")
    assert "listing is not pending" in page.text
    assert "alert-danger" in page.text


def test_deletion_requests_display_and_deny(admin_client, alumni_client):
    listing_id = _submit_listing(alumni_client, "job", "Deletion Demo Role")
    _request_deletion(listing_id)

    page = admin_client.get("/admin")
    assert "Deletion requests" in page.text
    assert "Deletion Demo Role" in page.text
    assert "Approve deletion" in page.text
    assert "Deny request" in page.text
    assert "Permanently delete this listing?" in page.text

    response = admin_client.post(
        f"/admin/listings/{listing_id}/deletion-request/deny", follow_redirects=False
    )

    assert response.status_code == 303
    pending_ids = [
        row["id"] for row in boards.list_deletion_requests(_admin_id())["listings"]
    ]
    assert listing_id not in pending_ids
    assert f"Deletion request for listing #{listing_id} denied." in admin_client.get(
        "/admin"
    ).text


def test_deletion_approval_removes_listing(admin_client, alumni_client):
    listing_id = _submit_listing(alumni_client, "event", "Permanent Deletion Demo")
    _request_deletion(listing_id)

    response = admin_client.post(
        f"/admin/listings/{listing_id}/deletion-request/approve",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert listing_id not in [row["id"] for row in boards.list_by_author(_alumni_id())]
    assert f"Listing #{listing_id} permanently deleted." in admin_client.get("/admin").text

    admin_client.post(
        f"/admin/listings/{listing_id}/deletion-request/approve", follow_redirects=False
    )
    assert "listing not found" in admin_client.get("/admin").text


def test_campaign_draft_created_and_listed(admin_client):
    response = admin_client.post(
        "/admin/campaigns",
        data={"subject": "Scholarship update", "body": "Applications are open."},
        follow_redirects=False,
    )

    assert response.status_code == 303
    page = admin_client.get("/admin")
    assert "Scholarship update" in page.text
    assert "Applications are open." in page.text
    assert "Draft" in page.text
    assert "Send now" in page.text
    assert "Schedule" in page.text


def test_campaign_form_requires_title_and_message(admin_client):
    before = len(campaigns.list_campaigns())

    response = admin_client.post(
        "/admin/campaigns",
        data={"subject": " ", "body": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert len(campaigns.list_campaigns()) == before
    assert "Campaign title and message are required." in admin_client.get("/admin").text


def test_admin_sends_campaign(admin_client):
    campaign = _create_campaign(admin_client, "Send demo", "Sent body")

    with isolate_publisher():
        register_website_observer()
        response = admin_client.post(
            f"/admin/campaigns/{campaign['id']}/send", follow_redirects=False
        )

    assert response.status_code == 303
    row = next(
        row for row in campaigns.list_campaigns() if row["id"] == campaign["id"]
    )
    assert row["status"] == "sent"
    page = admin_client.get("/admin")
    assert "no email is delivered" in page.text
    assert "Sent" in page.text


def test_admin_schedules_campaign_with_utc_timestamp(admin_client):
    campaign = _create_campaign(admin_client, "Schedule demo", "Scheduled body")

    response = admin_client.post(
        f"/admin/campaigns/{campaign['id']}/schedule",
        data={"scheduled_at": "2099-06-15T10:00:00Z"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    row = next(
        row for row in campaigns.list_campaigns() if row["id"] == campaign["id"]
    )
    assert row["status"] == "scheduled"
    assert row["scheduled_at"] == "2099-06-15T10:00:00+00:00"
    page = admin_client.get("/admin")
    assert "Scheduled" in page.text
    assert "2099-06-15T10:00:00+00:00" in page.text


def test_schedule_rejects_timestamp_without_timezone(admin_client):
    campaign = _create_campaign(admin_client, "Bad schedule", "Invalid timestamp")

    response = admin_client.post(
        f"/admin/campaigns/{campaign['id']}/schedule",
        data={"scheduled_at": "2099-06-15T10:00:00"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    row = next(
        row for row in campaigns.list_campaigns() if row["id"] == campaign["id"]
    )
    assert row["status"] == "draft"
    assert "scheduled_at must include a UTC offset or Z" in admin_client.get("/admin").text


def test_alumni_dashboard_remains_available(alumni_client):
    page = alumni_client.get("/app/dashboard")

    assert page.status_code == 200
    assert "My submissions" in page.text
