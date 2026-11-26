import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import engine
from app.main import app
from app.models.listing import Listing
from app.seed import seed
from app.services import board_auth, boards
from tests.listing_auth_helpers import login as authenticate

ALUMNI_EMAIL = "alumni.verified@my.uwi.edu"
ALUMNI_PASSWORD = "alumni-dev"
ADMIN_EMAIL = "admin@uwi.edu"
ADMIN_PASSWORD = "admin-dev"


@pytest.fixture(scope="module", autouse=True)
def seeded_database():
    seed(reset=True)


def _client(email, password):
    client = TestClient(app)
    authenticate(client, email, password)
    return client


def _alumni_id():
    return board_auth.login(ALUMNI_EMAIL, ALUMNI_PASSWORD)["id"]


def _admin_id():
    return board_auth.login(ADMIN_EMAIL, ADMIN_PASSWORD)["id"]


def _clear_listings():
    with Session(engine) as session:
        for row in session.exec(select(Listing)).all():
            session.delete(row)
        session.commit()


def _details(kind, details):
    data = dict(details)
    if kind == "job":
        data.setdefault("company_name", "Example Employer")
        data.setdefault("location", "Kingston")
        data.setdefault("application_instructions", "Apply by email")
        data.setdefault("faculty", "Science and Technology")
    return data


def _publish(kind, title, **details):
    result = boards.submit_listing(
        _alumni_id(),
        kind=kind,
        title=title,
        body="Description for " + title,
        **_details(kind, details),
    )
    assert result.get("ok"), result
    listing_id = result["listing"]["id"]
    assert boards.publish_listing(_admin_id(), listing_id).get("ok")
    return listing_id


def _submit(kind, title, **details):
    result = boards.submit_listing(
        _alumni_id(),
        kind=kind,
        title=title,
        body="Description for " + title,
        **_details(kind, details),
    )
    assert result.get("ok"), result
    return result["listing"]["id"]


def test_landing_page_loads_with_uwi_connect_branding():
    page = TestClient(app).get("/")

    assert page.status_code == 200
    assert "UWI Connect" in page.text
    assert "CampusWire" not in page.text
    assert "Connect with Opportunities." in page.text
    assert "Stay Connected to UWI." in page.text
    assert "Discover career opportunities, explore alumni events" in page.text
    assert "Not an official UWI service." in page.text
    assert "admin@uwi.edu" not in page.text


def test_landing_page_contains_expected_sections():
    page = TestClient(app).get("/")

    for text in (
        "Career Opportunities",
        "Alumni Events",
        "Community Connections",
        "How UWI Connect works",
        "Featured opportunities",
    ):
        assert text in page.text

    assert len(re.findall(r"<h1\b", page.text)) == 1
    assert len(re.findall(r"<h2\b", page.text)) >= 3


def test_anonymous_navigation_shows_public_browse_and_sign_in():
    page = TestClient(app).get("/")

    assert ">Sign in</a>" in page.text
    assert 'href="/api/boards?kind=job"' in page.text
    assert 'href="/api/boards?kind=event"' in page.text
    assert 'id="notificationMenu"' not in page.text
    assert re.search(r'href="[^"]*/app/dashboard"', page.text) is None
    assert re.search(r'action="[^"]*/logout"', page.text) is None


def test_alumni_navigation_shows_dashboard_bell_and_logout():
    page = _client(ALUMNI_EMAIL, ALUMNI_PASSWORD).get("/")

    assert page.status_code == 200
    assert re.search(r'href="[^"]*/app/dashboard"', page.text) is not None
    assert 'id="notificationMenu"' in page.text
    assert 'data-stream-url="/api/notifications/stream"' in page.text
    assert re.search(r'action="[^"]*/logout"', page.text) is not None
    assert 'name="csrf_token"' in page.text
    assert page.text.count("notifications.js") == 1
    assert ">Admin dashboard</a>" not in page.text


def test_admin_navigation_shows_admin_dashboard_link():
    page = _client(ADMIN_EMAIL, ADMIN_PASSWORD).get("/")

    assert page.status_code == 200
    assert re.search(r'href="[^"]*/admin"', page.text) is not None
    assert ">Admin dashboard</a>" in page.text


def test_featured_content_only_uses_eligible_published_listings():
    seed(reset=True)
    _clear_listings()

    job_id = _publish(
        "job",
        "Featured Backend Role",
        company_name="UWI Partner",
        location="Kingston",
        category="Engineering",
        application_deadline="2099-12-31",
    )
    _publish(
        "event",
        "Featured Alumni Meetup",
        event_date="2099-11-20",
        event_time="18:00",
        location="St. Augustine",
        category="Networking",
    )
    _submit(
        "job",
        "Hidden Pending Role",
        company_name="Hidden Co",
        location="Kingston",
        application_instructions="Apply by email",
        faculty="Science and Technology",
    )
    rejected_id = _submit(
        "job",
        "Hidden Rejected Role",
        company_name="Hidden Co",
        location="Kingston",
        application_instructions="Apply by email",
        faculty="Science and Technology",
    )
    assert boards.reject_listing(_admin_id(), rejected_id).get("ok")
    _publish(
        "job",
        "Hidden Expired Role",
        company_name="Hidden Co",
        location="Kingston",
        application_instructions="Apply by email",
        faculty="Science and Technology",
        application_deadline="2000-01-01",
    )
    _publish(
        "event",
        "Hidden Expired Event",
        event_date="2000-01-01",
        event_time="10:00",
        location="Kingston",
    )
    revision = boards.request_revision(
        _alumni_id(),
        job_id,
        {"kind": "job", "title": "Hidden Proposed Job Title", "body": "Proposed body"},
    )
    assert revision.get("ok")

    page = TestClient(app).get("/")

    assert "Featured Backend Role" in page.text
    assert "Featured Alumni Meetup" in page.text
    assert "UWI Partner" in page.text
    assert "2099-11-20" in page.text
    assert "Engineering" in page.text
    for hidden in (
        "Hidden Pending Role",
        "Hidden Rejected Role",
        "Hidden Expired Role",
        "Hidden Expired Event",
        "Hidden Proposed Job Title",
    ):
        assert hidden not in page.text


def test_featured_section_limits_number_of_listings():
    seed(reset=True)
    _clear_listings()

    for index in range(5):
        _publish(
            "job",
            f"Limited Job {index}",
            company_name="Example Employer",
            location="Kingston",
            application_instructions="Apply by email",
            faculty="Science and Technology",
        )

    page = TestClient(app).get("/")

    assert "Limited Job 4" in page.text
    assert "Limited Job 1" not in page.text
    assert page.text.count("Browse all jobs") == 3


def test_featured_empty_state_when_no_eligible_listings():
    seed(reset=True)
    _clear_listings()

    page = TestClient(app).get("/")

    assert page.status_code == 200
    assert "No featured job opportunities are available right now." in page.text
    assert "No featured events are available right now." in page.text


def test_existing_protected_routes_still_require_authentication():
    client = TestClient(app)

    for path in (
        "/app",
        "/app/dashboard",
        "/admin",
        "/admin/moderation",
        "/admin/revisions",
        "/admin/deletions",
        "/admin/campaigns",
    ):
        assert client.get(path).status_code == 401
