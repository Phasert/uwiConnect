import re

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
        "status": "pending",
        "created_at": "2099-03-01T12:00:00+00:00",
    }
    listing.update(overrides)
    return listing


def test_edit_button_only_for_pending_and_rejected(client, monkeypatch):
    def fake_list_by_author(author_id):
        return [
            _sample_listing(1, status="pending"),
            _sample_listing(2, status="rejected"),
            _sample_listing(3, status="published"),
        ]

    monkeypatch.setattr(
        "app.routers.user_home.boards.list_by_author", fake_list_by_author
    )
    page = _login(client).get("/app/dashboard")

    assert page.status_code == 200
    assert 'data-edit-id="1"' in page.text
    assert 'data-edit-id="2"' in page.text
    assert 'data-edit-id="3"' not in page.text
    assert 'data-delete-id="1"' in page.text
    assert 'data-delete-id="2"' in page.text
    assert 'data-delete-id="3"' in page.text


def test_edit_button_prepopulates_existing_listing_data(client, monkeypatch):
    def fake_list_by_author(author_id):
        return [
            _sample_listing(
                4,
                kind="event",
                status="rejected",
                title="Rejected Event",
                body="Event body text.",
            )
        ]

    monkeypatch.setattr(
        "app.routers.user_home.boards.list_by_author", fake_list_by_author
    )
    page = _login(client).get("/app/dashboard")

    assert 'data-edit-id="4"' in page.text
    assert 'data-kind="event"' in page.text
    assert 'data-title="Rejected Event"' in page.text
    assert 'data-body="Event body text."' in page.text


def test_deletion_requested_badge_replaces_action_buttons(client, monkeypatch):
    def fake_list_by_author(author_id):
        return [
            _sample_listing(5, status="published", deletion_requested=True)
        ]

    monkeypatch.setattr(
        "app.routers.user_home.boards.list_by_author", fake_list_by_author
    )
    page = _login(client).get("/app/dashboard")

    assert "Deletion requested — awaiting admin review" in page.text
    assert 'data-delete-id="5"' not in page.text
    assert 'data-edit-id="5"' not in page.text


def test_dashboard_contains_agreed_api_contract_and_feedback(client, monkeypatch):
    def fake_list_by_author(author_id):
        return [_sample_listing(6, status="rejected")]

    monkeypatch.setattr(
        "app.routers.user_home.boards.list_by_author", fake_list_by_author
    )
    page = _login(client).get("/app/dashboard")
    alum_id = board_auth.login(ALUMNI_EMAIL, PASSWORD)["id"]

    assert page.status_code == 200
    assert '/api/boards/listings/' in page.text
    assert "/deletion-request" in page.text
    assert "method: 'PUT'" in page.text
    assert "method: 'POST'" in page.text
    assert "Title and description are required." in page.text
    assert "Listing updated and sent for admin review." in page.text
    assert "The listing stays visible until an admin approves it." in page.text
    assert "The listing service may be unavailable." in page.text
    assert 'data-user-id="' + str(alum_id) + '"' in page.text


def test_successful_actions_refresh_and_preserve_feedback(client, monkeypatch):
    def fake_list_by_author(author_id):
        return [_sample_listing(7, status="rejected")]

    monkeypatch.setattr(
        "app.routers.user_home.boards.list_by_author", fake_list_by_author
    )
    page = _login(client).get("/app/dashboard")

    assert page.status_code == 200
    assert page.text.count("window.location.reload()") == 2
    assert page.text.count("sessionStorage.setItem('listingActionMessage'") == 2
    assert "sessionStorage.getItem('listingActionMessage')" in page.text
    assert "sessionStorage.removeItem('listingActionMessage')" in page.text
    assert "showMessage(pendingMessage, 'success')" in page.text
    assert (
        "sessionStorage.setItem('listingActionMessage', "
        "'Listing updated and sent for admin review.')"
    ) in page.text
    assert (
        "sessionStorage.setItem('listingActionMessage', "
        "'Deletion request sent. The listing stays visible until an admin approves it.')"
    ) in page.text
    for block in re.findall(r"\.catch\(function \(\) \{(.*?)\}\);", page.text, re.DOTALL):
        assert "reload" not in block
        assert "showMessage(" in block
