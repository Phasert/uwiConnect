import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import seed
from app.services import board_auth, boards, campaigns
from tests.csrf_test_helpers import csrf_token_from_html
from tests.listing_auth_helpers import login as authenticate

ALUMNI_EMAIL = "alumni.verified@my.uwi.edu"
ALUMNI_PASSWORD = "alumni-dev"
ADMIN_EMAIL = "admin@uwi.edu"
ADMIN_PASSWORD = "admin-dev"


@pytest.fixture(scope="module", autouse=True)
def seeded_database():
    seed(reset=True)


def _client(email, password, *, keep_header=True) -> TestClient:
    client = TestClient(app)
    authenticate(client, email, password)
    if not keep_header:
        client.headers.pop("X-CSRF-Token", None)
    return client


def _alumni_id() -> int:
    return board_auth.login(ALUMNI_EMAIL, ALUMNI_PASSWORD)["id"]


def _admin_id() -> int:
    return board_auth.login(ADMIN_EMAIL, ADMIN_PASSWORD)["id"]


def _publish(author_id, admin_id, title, proposed_title):
    submitted = boards.submit_listing(
        author_id,
        kind="job",
        title=title,
        body="Original body.",
        company_name="CSRF Employer",
        location="Montego Bay",
        application_instructions="Apply online.",
        faculty="Science and Technology",
    )
    assert submitted.get("ok")
    listing_id = submitted["listing"]["id"]
    assert boards.publish_listing(admin_id, listing_id).get("ok")
    revision = boards.request_revision(
        author_id,
        listing_id,
        {"kind": "job", "title": proposed_title, "body": "Revised body."},
    )
    assert revision.get("ok")
    return listing_id


def test_every_dashboard_post_form_has_csrf_field():
    clients = [(_client(ALUMNI_EMAIL, ALUMNI_PASSWORD), "/app/dashboard")]
    clients.extend(
        (_client(ADMIN_EMAIL, ADMIN_PASSWORD), path)
        for path in (
            "/admin",
            "/admin/moderation",
            "/admin/revisions",
            "/admin/deletions",
            "/admin/campaigns",
        )
    )

    for client, path in clients:
        page = client.get(path)
        forms = re.findall(
            r'<form\b[^>]*method="post"[^>]*>(.*?)</form>', page.text, re.DOTALL
        )

        assert page.status_code == 200
        assert forms
        for form in forms:
            assert 'name="csrf_token"' in form


def test_alumni_submission_works_with_hidden_token_and_no_header():
    client = _client(ALUMNI_EMAIL, ALUMNI_PASSWORD, keep_header=False)
    page = client.get("/app/dashboard")
    token = csrf_token_from_html(page.text)

    response = client.post(
        "/app/dashboard/submit",
        data={
            "csrf_token": token,
            "kind": "job",
            "title": "Hidden Token Submission",
            "body": "Submitted with the rendered CSRF field.",
            "location": "Kingston",
            "company_name": "Token Employer",
            "application_instructions": "Apply by email.",
            "faculty": "Science and Technology",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    titles = [row["title"] for row in boards.list_by_author(_alumni_id())]
    assert "Hidden Token Submission" in titles


def test_admin_campaign_works_with_hidden_token_and_no_header():
    client = _client(ADMIN_EMAIL, ADMIN_PASSWORD, keep_header=False)
    page = client.get("/admin/campaigns")
    token = csrf_token_from_html(page.text)

    response = client.post(
        "/admin/campaigns",
        data={
            "csrf_token": token,
            "subject": "Hidden Token Campaign",
            "body": "Created with the rendered CSRF field.",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    subjects = [row["subject"] for row in campaigns.list_campaigns()]
    assert "Hidden Token Campaign" in subjects


def test_alumni_actions_use_session_cookie_and_csrf_header():
    page = _client(ALUMNI_EMAIL, ALUMNI_PASSWORD).get("/app/dashboard")

    assert "X-User-Id" not in page.text
    assert "data-user-id" not in page.text
    assert 'data-csrf-token="' in page.text
    assert "'X-CSRF-Token': csrfToken" in page.text
    assert "credentials: 'same-origin'" in page.text
    assert "/revision-request" in page.text
    assert "Your session has expired. Please sign in again." in page.text
    assert "Your security token expired. Reload the page and try again." in page.text
    assert "You do not have permission to do that." in page.text


def test_revision_requests_are_visible_and_reviewable_from_admin_dashboard():
    alum = _client(ALUMNI_EMAIL, ALUMNI_PASSWORD)
    admin = _client(ADMIN_EMAIL, ADMIN_PASSWORD)
    author_id = _alumni_id()
    admin_id = _admin_id()

    listing_id = _publish(
        author_id, admin_id, "Revision Demo Role", "Revised Demo Role"
    )

    alum_page = alum.get("/app/dashboard")
    assert "Revision requested — awaiting admin review" in alum_page.text
    assert f'data-revise-id="{listing_id}"' not in alum_page.text

    admin_page = admin.get("/admin/revisions")
    assert "Revision requests" in admin_page.text
    assert "Approve revision" in admin_page.text
    assert "Deny revision" in admin_page.text
    assert "Revised Demo Role" in admin_page.text

    response = admin.post(
        f"/admin/listings/{listing_id}/revision-request/approve",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin/revisions")
    row = next(
        row for row in boards.list_by_author(author_id) if row["id"] == listing_id
    )
    assert row["title"] == "Revised Demo Role"
    assert (
        f"Listing #{listing_id} revision approved."
        in admin.get("/admin/revisions").text
    )


def test_admin_can_deny_a_revision_without_changing_the_listing():
    alum = _client(ALUMNI_EMAIL, ALUMNI_PASSWORD)
    admin = _client(ADMIN_EMAIL, ADMIN_PASSWORD)
    author_id = _alumni_id()
    admin_id = _admin_id()

    listing_id = _publish(author_id, admin_id, "Deny Demo Role", "Should Not Apply")

    assert "Approve revision" in admin.get("/admin/revisions").text
    response = admin.post(
        f"/admin/listings/{listing_id}/revision-request/deny",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin/revisions")
    row = next(
        row for row in boards.list_by_author(author_id) if row["id"] == listing_id
    )
    assert row["title"] == "Deny Demo Role"
    assert (
        f"Revision request for listing #{listing_id} denied."
        in admin.get("/admin/revisions").text
    )
    assert "Should Not Apply" not in alum.get("/app/dashboard").text


def test_missing_csrf_is_explained_for_html_and_api():
    admin = _client(ADMIN_EMAIL, ADMIN_PASSWORD, keep_header=False)
    html_response = admin.post(
        "/admin/campaigns",
        data={"subject": "No token", "body": "No token"},
        follow_redirects=False,
    )

    assert html_response.status_code == 403
    assert "security token expired" in html_response.text.lower()

    alumni = _client(ALUMNI_EMAIL, ALUMNI_PASSWORD, keep_header=False)
    api_response = alumni.post("/api/boards/listings/999999/deletion-request")

    assert api_response.status_code == 403
    assert api_response.json() == {"detail": "CSRF token missing or invalid"}
