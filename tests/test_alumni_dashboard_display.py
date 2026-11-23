import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import seed
from app.services import board_auth

ALUMNI_EMAIL = "alumni.verified@my.uwi.edu"
PASSWORD = "alumni-dev"


@pytest.fixture
def client():
    seed(reset=True)
    return TestClient(app)


def _login(client: TestClient) -> TestClient:
    response = client.post(
        "/login",
        data={"username": ALUMNI_EMAIL, "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def _sample_listing(listing_id: int, **overrides):
    listing = {
        "id": listing_id,
        "author_id": 2,
        "kind": "job",
        "title": f"Sample Listing {listing_id}",
        "body": "Sample body.",
        "status": "rejected",
        "created_at": "2099-03-01T12:00:00+00:00",
    }
    listing.update(overrides)
    return listing


def test_rejected_job_displays_rejection_reason(client, monkeypatch):
    seen = []

    def fake_list_by_author(author_id):
        seen.append(author_id)
        return [
            _sample_listing(
                1,
                kind="job",
                status="rejected",
                rejection_reason="Missing salary range.",
            )
        ]

    monkeypatch.setattr(
        "app.routers.user_home.boards.list_by_author", fake_list_by_author
    )
    page = _login(client).get("/app/dashboard")

    assert page.status_code == 200
    assert "Missing salary range." in page.text
    assert "Rejected" in page.text
    assert seen == [board_auth.login(ALUMNI_EMAIL, PASSWORD)["id"]]


def test_rejected_event_displays_rejection_reason(client, monkeypatch):
    def fake_list_by_author(author_id):
        return [
            _sample_listing(
                2,
                kind="event",
                status="rejected",
                rejection_reason="Event date was not confirmed.",
            )
        ]

    monkeypatch.setattr(
        "app.routers.user_home.boards.list_by_author", fake_list_by_author
    )
    page = _login(client).get("/app/dashboard")

    assert page.status_code == 200
    assert "Event date was not confirmed." in page.text
    assert "Event" in page.text


def test_rejected_listings_without_reason_show_fallback(client, monkeypatch):
    def fake_list_by_author(author_id):
        return [
            _sample_listing(3, status="rejected", rejection_reason=None),
            _sample_listing(4, status="rejected"),
        ]

    monkeypatch.setattr(
        "app.routers.user_home.boards.list_by_author", fake_list_by_author
    )
    page = _login(client).get("/app/dashboard")

    assert page.status_code == 200
    assert page.text.count("No reason provided.") == 2


def test_pending_and_published_listings_hide_rejection_reasons(client, monkeypatch):
    def fake_list_by_author(author_id):
        return [
            _sample_listing(
                5,
                title="Pending Sample",
                status="pending",
                rejection_reason="Hidden pending reason.",
            ),
            _sample_listing(
                6,
                title="Published Sample",
                kind="event",
                status="published",
                rejection_reason="Hidden published reason.",
            ),
        ]

    monkeypatch.setattr(
        "app.routers.user_home.boards.list_by_author", fake_list_by_author
    )
    page = _login(client).get("/app/dashboard")

    assert page.status_code == 200
    assert "Hidden pending reason." not in page.text
    assert "Hidden published reason." not in page.text
    assert "No reason provided." not in page.text
    assert "Pending" in page.text
    assert "Published" in page.text


def test_dashboard_uses_logged_in_alumnus_id(client, monkeypatch):
    seen = []

    def fake_list_by_author(author_id):
        seen.append(author_id)
        return []

    monkeypatch.setattr(
        "app.routers.user_home.boards.list_by_author", fake_list_by_author
    )
    page = _login(client).get("/app/dashboard?author_id=999999")

    assert page.status_code == 200
    assert seen == [board_auth.login(ALUMNI_EMAIL, PASSWORD)["id"]]
    assert "You have not submitted any listings yet." in page.text
