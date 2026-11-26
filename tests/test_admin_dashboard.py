import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import seed
from app.services import board_auth, boards, campaigns
from app.services.website_notifications import register_website_observer
from tests.campaign_test_helpers import isolate_publisher
from tests.csrf_test_helpers import csrf_token_from_html

ADMIN_EMAIL = "admin@uwi.edu"
ADMIN_PASSWORD = "admin-dev"
ALUMNI_EMAIL = "alumni.verified@my.uwi.edu"
ALUMNI_PASSWORD = "alumni-dev"

ADMIN_PAGES = (
    "/admin",
    "/admin/moderation",
    "/admin/revisions",
    "/admin/deletions",
    "/admin/campaigns",
)
ADMIN_SUBPAGES = ADMIN_PAGES[1:]


@pytest.fixture(scope="module", autouse=True)
def seeded_database():
    seed(reset=True)


def _login(email, password):
    client = TestClient(app)
    csrf_token = csrf_token_from_html(client.get("/login").text)
    response = client.post(
        "/login",
        data={
            "username": email,
            "password": password,
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    csrf_token = client.get("/api/auth/csrf").json()["csrf_token"]
    client.headers.update({"X-CSRF-Token": csrf_token})
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


def _request_deletion(client, listing_id):
    response = client.post(f"/api/boards/listings/{listing_id}/deletion-request")
    assert response.status_code == 200


def _create_campaign(client, subject="Demo campaign", body="Alumni update"):
    response = client.post(
        "/admin/campaigns",
        data={"subject": subject, "body": body},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return next(row for row in campaigns.list_campaigns() if row["subject"] == subject)


def _sample_listing(listing_id, **overrides):
    row = {
        "id": listing_id,
        "author_id": 9988,
        "kind": "job",
        "title": f"Sample listing {listing_id}",
        "body": "Sample description",
        "company_name": "Example & Co",
        "location": "Kingston",
        "category": None,
        "faculty": "Science and Technology",
        "department": "Computing",
        "application_instructions": "Apply online",
        "application_deadline": "2099-07-01",
        "event_date": None,
        "event_time": None,
        "registration_information": None,
        "status": "pending",
        "deletion_requested": False,
        "rejection_reason": None,
        "created_at": "2099-01-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def _sample_revision(listing_id):
    return {
        "listing_id": listing_id,
        "requesting_author_id": 9988,
        "status": "pending",
        "requested_at": "2099-05-01T00:00:00+00:00",
        "original": _sample_listing(
            listing_id, status="published", title="Original Role"
        ),
        "proposed": {
            "kind": "job",
            "title": "Updated Role",
            "body": "Updated description",
            "company_name": "Example & Co",
            "location": "Kingston",
            "faculty": "Science and Technology",
            "department": "Computing",
            "application_instructions": "Apply online",
            "application_deadline": "2099-07-01",
            "event_date": None,
            "event_time": None,
            "category": None,
            "registration_information": None,
        },
    }


def _sample_campaign(campaign_id, **overrides):
    row = {
        "id": campaign_id,
        "subject": f"Campaign {campaign_id}",
        "body": "Campaign body",
        "status": "draft",
        "scheduled_at": None,
        "sent_at": None,
    }
    row.update(overrides)
    return row


def _isolate_admin_context(
    monkeypatch,
    *,
    pending=None,
    deletions=None,
    revisions=None,
    campaign_rows=None,
    published=None,
    author_display_name="Test Author",
):
    monkeypatch.setattr(
        "app.routers.admin_home.boards.list_pending", lambda: pending or []
    )
    monkeypatch.setattr(
        "app.routers.admin_home.boards.list_deletion_requests",
        lambda admin_id: {"ok": True, "listings": deletions or []},
    )
    monkeypatch.setattr(
        "app.routers.admin_home.boards.list_revision_requests",
        lambda admin_id: {"ok": True, "revision_requests": revisions or []},
    )
    monkeypatch.setattr(
        "app.routers.admin_home.campaigns.list_campaigns", lambda: campaign_rows or []
    )
    monkeypatch.setattr(
        "app.routers.admin_home.boards.browse_published", lambda: published or []
    )
    monkeypatch.setattr(
        "app.routers.admin_home.get_user",
        lambda author_id: {"display_name": author_display_name},
    )


def test_admin_dashboard_requires_admin_session(admin_client, alumni_client):
    anonymous = TestClient(app)

    for path in ADMIN_PAGES:
        assert anonymous.get(path).status_code == 401
        assert alumni_client.get(path).status_code == 403

    assert (
        alumni_client.post(
            "/admin/listings/999999/publish", follow_redirects=False
        ).status_code
        == 403
    )
    assert (
        alumni_client.post(
            "/admin/campaigns",
            data={"subject": "Blocked", "body": "Blocked"},
            follow_redirects=False,
        ).status_code
        == 403
    )

    page = admin_client.get("/admin")
    assert page.status_code == 200
    assert "Admin dashboard" in page.text


def test_overview_links_resolve_to_authenticated_pages(admin_client):
    page = admin_client.get("/admin")

    for label, action, path in (
        ("Listing moderation", "Review listings", "/admin/moderation"),
        ("Revision requests", "Review revisions", "/admin/revisions"),
        ("Deletion requests", "Review deletions", "/admin/deletions"),
        ("Campaigns", "Manage campaigns", "/admin/campaigns"),
    ):
        assert label in page.text
        assert action in page.text
        assert f'href="http://testserver{path}"' in page.text
        assert admin_client.get(path).status_code == 200

    for description in (
        "Pending job and event submissions",
        "Revision requests awaiting review",
        "Deletion requests awaiting decision",
        "Campaigns sent",
        "Published listings",
    ):
        assert description in page.text
    assert "No job or event listings are awaiting review." not in page.text
    assert "User management" not in page.text
    for control in (
        'name="rejection_reason"',
        'data-confirm-delete="true"',
        "data-schedule-form",
        'name="subject"',
    ):
        assert control not in page.text


def test_overview_counts_match_page_specific_data(admin_client, monkeypatch):
    _isolate_admin_context(
        monkeypatch,
        pending=[_sample_listing(1), _sample_listing(2)],
        deletions=[
            _sample_listing(3, deletion_requested=True),
            _sample_listing(4, deletion_requested=True),
            _sample_listing(5, deletion_requested=True),
        ],
        revisions=[_sample_revision(6) for _ in range(4)],
        campaign_rows=[
            _sample_campaign(7),
            _sample_campaign(8, status="scheduled"),
            _sample_campaign(9, status="sent"),
        ],
        published=[{"id": value} for value in range(10, 15)],
    )

    html = admin_client.get("/admin").text

    for area, expected in (
        ("moderation", 2),
        ("revisions", 4),
        ("deletions", 3),
        ("campaigns", 1),
    ):
        match = re.search(
            rf"admin-overview-card--{area}.*?admin-card-stat[^>]*>\s*(\d+)\s*<",
            html,
            re.DOTALL,
        )
        assert match, area
        assert int(match.group(1)) == expected

    published = re.search(
        r"admin-secondary-stat.*?admin-secondary-stat-value[^>]*>\s*(\d+)\s*<",
        html,
        re.DOTALL,
    )
    assert published
    assert int(published.group(1)) == 5
    assert "1 draft · 1 scheduled" in html


def test_subpages_have_explicit_back_links_and_scoped_styles(admin_client):
    for path in ADMIN_SUBPAGES:
        html = admin_client.get(path).text

        assert "← Back to admin dashboard" in html
        assert 'href="http://testserver/admin"' in html
        assert html.index("← Back to admin dashboard") < html.index("<h1")
        assert 'href="http://testserver/static/css/app.css"' in html
        assert 'href="http://testserver/static/css/admin-dashboard.css"' in html
        assert 'class="admin-dashboard"' in html


def test_each_subpage_renders_only_relevant_management_content(
    admin_client, monkeypatch
):
    _isolate_admin_context(
        monkeypatch,
        pending=[_sample_listing(7001)],
        deletions=[_sample_listing(7002, deletion_requested=True, status="published")],
        revisions=[_sample_revision(7003)],
        campaign_rows=[_sample_campaign(7004)],
    )

    expected = {
        "/admin/moderation": 'name="rejection_reason"',
        "/admin/revisions": "Approve revision for #7003",
        "/admin/deletions": 'data-confirm-delete="true"',
        "/admin/campaigns": 'name="subject"',
    }
    for path, marker in expected.items():
        html = admin_client.get(path).text

        assert marker in html
        assert "← Back to admin dashboard" in html
        for other_path, other_marker in expected.items():
            if other_path != path:
                assert other_marker not in html

    deletion_html = admin_client.get("/admin/deletions").text
    assert "Permanently delete this listing?" in deletion_html
    assert (
        "Permanently delete this listing?"
        not in admin_client.get("/admin/moderation").text
    )

    campaigns_html = admin_client.get("/admin/campaigns").text
    assert "data-schedule-form" in campaigns_html
    assert "data-schedule-form" not in admin_client.get("/admin/revisions").text


def test_empty_states_render(admin_client, monkeypatch):
    _isolate_admin_context(monkeypatch)

    for path, expected in (
        ("/admin/moderation", "No job or event listings are awaiting review."),
        ("/admin/revisions", "No revision requests are awaiting review."),
        ("/admin/deletions", "No deletion requests are awaiting review."),
        ("/admin/campaigns", "No campaigns have been created yet."),
    ):
        assert expected in admin_client.get(path).text


def test_pending_listings_show_expanded_fields(admin_client, alumni_client):
    _submit_listing(alumni_client, "job", "Graduate Developer Role")
    _submit_listing(alumni_client, "event", "Alumni Networking Night")

    page = admin_client.get("/admin/moderation")

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
        'name="rejection_reason"',
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
    assert response.headers["location"].endswith("/admin/moderation")
    row = next(
        row for row in boards.list_by_author(_alumni_id()) if row["id"] == listing_id
    )
    assert row["status"] == "published"

    page = admin_client.get("/admin/moderation")
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
    assert response.headers["location"].endswith("/admin/moderation")
    row = next(
        row for row in boards.list_by_author(_alumni_id()) if row["id"] == listing_id
    )
    assert row["status"] == "rejected"
    assert row["rejection_reason"] == "Please add a confirmed venue."
    assert (
        f"Listing #{listing_id} rejected." in admin_client.get("/admin/moderation").text
    )


def test_backend_error_is_displayed(admin_client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin_home.boards.publish_listing",
        lambda admin_id, listing_id: {"ok": False, "error": "listing is not pending"},
    )

    response = admin_client.post(
        "/admin/listings/999999/publish", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin/moderation")
    page = admin_client.get("/admin/moderation")
    assert "listing is not pending" in page.text
    assert "alert-danger" in page.text


def test_deletion_requests_display_and_deny(admin_client, alumni_client):
    listing_id = _submit_listing(alumni_client, "job", "Deletion Demo Role")
    _request_deletion(alumni_client, listing_id)

    page = admin_client.get("/admin/deletions")
    assert "Deletion requests" in page.text
    assert "Deletion Demo Role" in page.text
    assert "Approve deletion" in page.text
    assert "Deny request" in page.text
    assert "Permanently delete this listing?" in page.text

    response = admin_client.post(
        f"/admin/listings/{listing_id}/deletion-request/deny", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin/deletions")
    pending_ids = [
        row["id"] for row in boards.list_deletion_requests(_admin_id())["listings"]
    ]
    assert listing_id not in pending_ids
    assert (
        f"Deletion request for listing #{listing_id} denied."
        in admin_client.get("/admin/deletions").text
    )


def test_deletion_approval_removes_listing(admin_client, alumni_client):
    listing_id = _submit_listing(alumni_client, "event", "Permanent Deletion Demo")
    _request_deletion(alumni_client, listing_id)

    response = admin_client.post(
        f"/admin/listings/{listing_id}/deletion-request/approve",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin/deletions")
    assert listing_id not in [row["id"] for row in boards.list_by_author(_alumni_id())]
    assert (
        f"Listing #{listing_id} permanently deleted."
        in admin_client.get("/admin/deletions").text
    )

    response = admin_client.post(
        f"/admin/listings/{listing_id}/deletion-request/approve", follow_redirects=False
    )
    assert response.headers["location"].endswith("/admin/deletions")
    assert "listing not found" in admin_client.get("/admin/deletions").text


def test_campaign_draft_created_and_listed(admin_client):
    response = admin_client.post(
        "/admin/campaigns",
        data={"subject": "Scholarship update", "body": "Applications are open."},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin/campaigns")
    page = admin_client.get("/admin/campaigns")
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
    assert response.headers["location"].endswith("/admin/campaigns")
    assert len(campaigns.list_campaigns()) == before
    assert (
        "Campaign title and message are required."
        in admin_client.get("/admin/campaigns").text
    )


def test_admin_sends_campaign(admin_client):
    campaign = _create_campaign(admin_client, "Send demo", "Sent body")

    with isolate_publisher():
        register_website_observer()
        response = admin_client.post(
            f"/admin/campaigns/{campaign['id']}/send", follow_redirects=False
        )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin/campaigns")
    row = next(row for row in campaigns.list_campaigns() if row["id"] == campaign["id"])
    assert row["status"] == "sent"
    page = admin_client.get("/admin/campaigns")
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
    assert response.headers["location"].endswith("/admin/campaigns")
    row = next(row for row in campaigns.list_campaigns() if row["id"] == campaign["id"])
    assert row["status"] == "scheduled"
    assert row["scheduled_at"] == "2099-06-15T10:00:00+00:00"
    page = admin_client.get("/admin/campaigns")
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
    assert response.headers["location"].endswith("/admin/campaigns")
    row = next(row for row in campaigns.list_campaigns() if row["id"] == campaign["id"])
    assert row["status"] == "draft"
    assert (
        "scheduled_at must include a UTC offset or Z"
        in admin_client.get("/admin/campaigns").text
    )


def test_admin_actions_require_csrf_token(admin_client):
    admin_client.headers.pop("X-CSRF-Token", None)

    actions = (
        ("/admin/listings/999999/publish", None),
        ("/admin/listings/999999/reject", {"rejection_reason": "Blocked"}),
        ("/admin/listings/999999/deletion-request/approve", None),
        ("/admin/listings/999999/deletion-request/deny", None),
        ("/admin/listings/999999/revision-request/approve", None),
        ("/admin/listings/999999/revision-request/deny", None),
        ("/admin/campaigns", {"subject": "Blocked", "body": "Blocked"}),
        ("/admin/campaigns/999999/send", None),
        ("/admin/campaigns/999999/schedule", {"scheduled_at": "2099-06-15T10:00:00Z"}),
    )
    for path, data in actions:
        response = admin_client.post(path, data=data, follow_redirects=False)
        assert response.status_code == 403, path


def test_pending_cards_escape_user_content_and_label_actions(admin_client, monkeypatch):
    _isolate_admin_context(
        monkeypatch,
        pending=[
            {
                "id": 4242,
                "author_id": 9988,
                "kind": "job",
                "title": "<script>alert('title')</script>",
                "body": "See https://example.invalid/a/very/long/path for details.",
                "company_name": "Example & Co",
                "location": "Kingston",
                "category": None,
                "faculty": None,
                "department": "Computing",
                "application_deadline": "2099-07-01",
                "application_instructions": "Apply online",
                "event_date": None,
                "event_time": None,
                "registration_information": None,
                "status": "pending",
                "rejection_reason": None,
                "created_at": "2099-01-01T00:00:00+00:00",
                "deletion_requested": False,
            }
        ],
        author_display_name="<img src=x onerror=alert(1)>",
    )

    html = admin_client.get("/admin/moderation").text

    assert "<script>alert('title')</script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "Example &amp; Co" in html
    assert "Listing #4242" in html
    assert "Approve listing #4242" in html
    assert "Reject listing #4242" in html
    assert 'name="rejection_reason"' in html
    assert "Optional. Tell the author what to change" in html
    assert "text-break" in html


def test_deletion_card_identifies_listing_author_and_warning(admin_client, monkeypatch):
    _isolate_admin_context(
        monkeypatch,
        deletions=[
            {
                "id": 6100,
                "author_id": 9988,
                "kind": "event",
                "title": "Deletion Candidate",
                "status": "published",
                "deletion_requested": True,
                "company_name": None,
                "location": "Kingston",
                "category": None,
                "faculty": None,
                "department": None,
                "application_instructions": None,
                "application_deadline": None,
                "event_date": "2099-06-15",
                "event_time": "18:00",
                "registration_information": None,
                "rejection_reason": None,
                "created_at": "2099-01-01T00:00:00+00:00",
            }
        ],
        author_display_name="<script>alert('author')</script>",
    )

    html = admin_client.get("/admin/deletions").text

    assert "<script>alert('author')</script>" not in html
    assert "&lt;script&gt;" in html
    assert "Listing #6100 · Deletion Candidate" in html
    assert "Requested by" in html
    assert "Approving deletion permanently removes listing #6100" in html
    assert "Approve deletion of #6100" in html
    assert "Deny request for #6100" in html
    assert 'data-confirm-delete="true"' in html


def test_revision_diff_shows_non_title_changes_and_removals(admin_client, monkeypatch):
    _isolate_admin_context(
        monkeypatch,
        revisions=[
            {
                "listing_id": 5151,
                "requesting_author_id": 7766,
                "status": "pending",
                "requested_at": "2099-05-01T00:00:00+00:00",
                "original": {
                    "id": 5151,
                    "author_id": 7766,
                    "kind": "job",
                    "title": "Original Role",
                    "body": "Original body",
                    "company_name": "Old Employer",
                    "location": "Kingston",
                    "application_instructions": "Apply by post",
                    "application_deadline": "2099-06-01",
                    "faculty": "Science and Technology",
                    "department": "Computing",
                    "event_date": None,
                    "event_time": None,
                    "category": "Engineering",
                    "registration_information": None,
                    "status": "published",
                    "deletion_requested": False,
                    "rejection_reason": None,
                    "created_at": "2099-01-01T00:00:00+00:00",
                },
                "proposed": {
                    "kind": "event",
                    "title": "Updated Role",
                    "body": "Updated body",
                    "company_name": "New Employer",
                    "location": "Montego Bay",
                    "application_instructions": "Apply online",
                    "application_deadline": None,
                    "faculty": "Engineering",
                    "department": None,
                    "event_date": "2099-10-20",
                    "event_time": "19:00",
                    "category": "Careers",
                    "registration_information": "Register online",
                },
            }
        ],
        author_display_name="Revision Author",
    )

    html = admin_client.get("/admin/revisions").text

    for text in (
        "Changed fields",
        "Current",
        "Proposed",
        "Listing type",
        "Title",
        "Original Role",
        "Updated Role",
        "Description",
        "Original body",
        "Updated body",
        "Company",
        "Old Employer",
        "New Employer",
        "Location",
        "Kingston",
        "Montego Bay",
        "Faculty",
        "Science and Technology",
        "Engineering",
        "Department",
        "Computing",
        "How to apply",
        "Apply by post",
        "Apply online",
        "Application deadline",
        "2099-06-01",
        "Event date",
        "2099-10-20",
        "Event time",
        "19:00",
        "Category",
        "Careers",
        "Registration information",
        "Register online",
        "Removed",
        "Added",
        "No value",
        "Approve revision for #5151",
        "Deny revision for #5151",
    ):
        assert text in html
    assert "No field values changed" not in html


def test_admin_pages_keep_notification_hooks(admin_client):
    for path in ADMIN_PAGES:
        page = admin_client.get(path)

        assert page.status_code == 200
        for element_id in (
            "notificationMenu",
            "notificationBell",
            "notificationPanel",
            "notificationList",
            "notificationStatus",
            "notificationBadge",
            "notificationCount",
        ):
            assert f'id="{element_id}"' in page.text
        assert 'data-notifications-url="/api/notifications"' in page.text
        assert 'data-stream-url="/api/notifications/stream"' in page.text
        assert page.text.count("notifications.js") == 1


def test_alumni_dashboard_remains_available(alumni_client):
    page = alumni_client.get("/app/dashboard")

    assert page.status_code == 200
    assert "My submissions" in page.text
