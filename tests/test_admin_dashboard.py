import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import seed
from app.services import board_auth, boards, campaigns
from app.services.website_notifications import register_website_observer
from tests.campaign_test_helpers import add_user, isolate_publisher
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


def _submission_details(html):
    return re.findall(r"<details\b([^>]*)>(.*?)</details>", html, re.DOTALL)


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
        "app.routers.admin_home.boards.list_pending",
        lambda *args, **kwargs: pending or [],
    )
    monkeypatch.setattr(
        "app.routers.admin_home.boards.list_deletion_requests",
        lambda admin_id, *args, **kwargs: {"ok": True, "listings": deletions or []},
    )
    monkeypatch.setattr(
        "app.routers.admin_home.boards.list_revision_requests",
        lambda admin_id, *args, **kwargs: {"ok": True, "revision_requests": revisions or []},
    )
    monkeypatch.setattr(
        "app.routers.admin_home.campaigns.list_campaigns",
        lambda *args, **kwargs: campaign_rows or [],
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


def test_admin_uses_admin_dashboard_not_alumni_board(admin_client):
    assert admin_client.get("/app").status_code == 403
    page = admin_client.get("/admin")
    assert page.status_code == 200
    assert "Admin dashboard" in page.text
    assert re.search(r'href="[^"]*/admin">Admin</a>', page.text)
    assert ">Browse boards</a>" not in page.text
    assert "My submissions" not in page.text
    assert re.search(r'href="[^"]*/app/dashboard', page.text) is None


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


def test_pending_submissions_are_collapsed_by_default(admin_client, monkeypatch):
    _isolate_admin_context(
        monkeypatch,
        pending=[_sample_listing(8201), _sample_listing(8202)],
    )

    details = _submission_details(admin_client.get("/admin/moderation").text)

    assert len(details) == 2
    ids = []
    for attrs, content in details:
        assert 'class="admin-submission"' in attrs
        assert not re.search(r"\bopen\b", attrs)
        assert "<summary" in content
        match = re.search(r'\bid="(submissionDetails\d+)"', attrs)
        assert match
        ids.append(match.group(1))
    assert len(set(ids)) == 2


def test_pending_submission_summary_shows_key_information(admin_client, monkeypatch):
    _isolate_admin_context(
        monkeypatch,
        pending=[
            _sample_listing(
                8301,
                kind="job",
                title="Summary Job",
                created_at="2099-03-04T09:30:00+00:00",
            ),
            _sample_listing(
                8302,
                kind="event",
                title="Summary Event",
                created_at="2099-05-06T17:00:00+00:00",
            ),
        ],
    )

    details = _submission_details(admin_client.get("/admin/moderation").text)

    assert len(details) == 2
    expected = (
        (8301, "Summary Job", "Job", "2099-03-04T09:30:00+00:00"),
        (8302, "Summary Event", "Event", "2099-05-06T17:00:00+00:00"),
    )
    for (_, content), (listing_id, title, kind, created_at) in zip(details, expected):
        summary_match = re.search(r"<summary\b[^>]*>(.*?)</summary>", content, re.DOTALL)
        assert summary_match
        summary = summary_match.group(1)

        assert title in summary
        assert f"Listing #{listing_id}" in summary
        assert kind in summary
        assert "Test Author" in summary
        assert created_at in summary
        assert "Status:" in summary
        assert "Pending" in summary
        assert "Review details" in summary


def test_pending_submission_actions_stay_outside_summary_and_keep_csrf(
    admin_client, monkeypatch
):
    _isolate_admin_context(monkeypatch, pending=[_sample_listing(8401)])

    html = admin_client.get("/admin/moderation").text
    details = _submission_details(html)

    assert len(details) == 1
    _, content = details[0]
    summary_match = re.search(r"<summary\b[^>]*>(.*?)</summary>", content, re.DOTALL)
    assert summary_match
    summary = summary_match.group(1)

    assert "<form" not in summary
    assert 'type="submit"' not in summary
    assert content.count('<form method="post"') == 2
    assert "/admin/listings/8401/publish" in content
    assert "/admin/listings/8401/reject" in content
    assert content.count('name="csrf_token"') == 2
    assert 'name="rejection_reason"' in content
    assert "Optional. Tell the author what to change" in content
    assert "Approve listing #8401" in content
    assert "Reject listing #8401" in content
    assert content.count("data-admin-action") == 2
    assert "button.disabled = true;" in html


def test_admin_approves_pending_listing(admin_client, alumni_client):
    listing_id = _submit_listing(alumni_client, "job", "Approval Demo Role")

    response = admin_client.post(
        f"/admin/listings/{listing_id}/publish", follow_redirects=False
    )

    assert response.status_code == 303
    assert "/admin/moderation" in response.headers["location"]
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
    assert "/admin/moderation" in response.headers["location"]
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
    assert "/admin/moderation" in response.headers["location"]
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
    assert "/admin/deletions" in response.headers["location"]
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
    assert "/admin/deletions" in response.headers["location"]
    assert listing_id not in [row["id"] for row in boards.list_by_author(_alumni_id())]
    assert (
        f"Listing #{listing_id} permanently deleted."
        in admin_client.get("/admin/deletions").text
    )

    response = admin_client.post(
        f"/admin/listings/{listing_id}/deletion-request/approve", follow_redirects=False
    )
    assert "/admin/deletions" in response.headers["location"]
    assert "listing not found" in admin_client.get("/admin/deletions").text


def test_campaign_draft_created_and_listed(admin_client):
    response = admin_client.post(
        "/admin/campaigns",
        data={"subject": "Scholarship update", "body": "Applications are open."},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "/admin/campaigns" in response.headers["location"]
    page = admin_client.get("/admin/campaigns")
    assert "Scholarship update" in page.text
    assert "Applications are open." in page.text
    assert "Draft" in page.text
    assert "Send now" in page.text
    assert "Schedule" in page.text


def test_campaign_content_is_escaped_in_admin_list(admin_client):
    campaign = _create_campaign(
        admin_client,
        "<script>alert('campaign')</script>",
        "<img src=x onerror=alert(1)>",
    )

    page = admin_client.get("/admin/campaigns").text

    assert "&lt;script&gt;alert(&#39;campaign&#39;)&lt;/script&gt;" in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page
    assert f"/admin/campaigns/{campaign['id']}/send" in page


def test_campaign_form_requires_title_and_message(admin_client):
    before = len(campaigns.list_campaigns())

    response = admin_client.post(
        "/admin/campaigns",
        data={"subject": " ", "body": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "/admin/campaigns" in response.headers["location"]
    assert len(campaigns.list_campaigns()) == before
    assert (
        "Campaign title and message are required."
        in admin_client.get("/admin/campaigns").text
    )


def test_campaign_form_selects_and_displays_audience(admin_client, monkeypatch):
    monkeypatch.setattr(
        campaigns,
        "audience_options",
        lambda: {"faculty": ["Science & Technology"], "department": ["Computing"]},
    )
    page = admin_client.get("/admin/campaigns")
    assert 'name="audience_type"' in page.text
    assert 'value="Science &amp; Technology"' in page.text
    assert 'value="Computing"' in page.text

    response = admin_client.post(
        "/admin/campaigns",
        data={
            "subject": "Faculty audience",
            "body": "Message",
            "audience_type": "faculty",
            "faculty": "Science & Technology",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    campaign = next(
        row for row in campaigns.list_campaigns() if row["subject"] == "Faculty audience"
    )
    assert campaign["audience_type"] == "faculty"
    assert campaign["audience_value"] == "Science & Technology"
    assert "Faculty: Science &amp; Technology" in admin_client.get("/admin/campaigns").text


def test_campaign_audience_and_filters_work_together(admin_client, monkeypatch):
    monkeypatch.setattr(
        campaigns,
        "audience_options",
        lambda: {"faculty": ["Science"], "department": ["Computing"]},
    )
    query = "q=Target&status=draft&sort=subject_asc"
    page = admin_client.get(f"/admin/campaigns?{query}")
    assert 'value="Science"' in page.text
    assert 'value="Target"' in page.text
    response = admin_client.post(
        f"/admin/campaigns?{query}",
        data={"subject": "Target", "body": "Message", "audience_type": "all", "faculty": "Science"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/admin/campaigns?{query}")


@pytest.mark.parametrize(
    ("audience_type", "value"),
    [("unknown", ""), ("faculty", ""), ("department", "Unknown")],
)
def test_web_and_api_reject_invalid_audiences(
    admin_client, monkeypatch, audience_type, value
):
    monkeypatch.setattr(
        campaigns,
        "audience_options",
        lambda: {"faculty": ["Science"], "department": ["Computing"]},
    )
    before = len(campaigns.list_campaigns())
    web = admin_client.post(
        "/admin/campaigns",
        data={
            "subject": "Invalid web audience",
            "body": "Message",
            "audience_type": audience_type,
            "faculty": value,
            "department": value,
        },
        follow_redirects=False,
    )
    api = admin_client.post(
        "/api/admin/campaigns",
        json={
            "subject": "Invalid API audience",
            "body": "Message",
            "audience_type": audience_type,
            "audience_value": value,
        },
    )
    assert web.status_code == 303
    assert api.status_code == 400
    assert len(campaigns.list_campaigns()) == before


@pytest.mark.parametrize(
    "extra",
    [
        {"faculty": "Science"},
        {"department": "Computing"},
        {"audienceType": "faculty", "faculty": "Science"},
        {"audience_type": "faculty", "audience_value": "Science", "faculty": "Science"},
    ],
)
def test_api_rejects_unsupported_audience_fields(admin_client, extra):
    before = len(campaigns.list_campaigns())
    response = admin_client.post(
        "/api/admin/campaigns",
        json={"subject": "Invalid audience fields", "body": "Message", **extra},
    )

    assert response.status_code == 422
    assert any(error["type"] == "extra_forbidden" for error in response.json()["detail"])
    assert len(campaigns.list_campaigns()) == before


def test_api_defaults_to_all_alumni_when_audience_is_omitted(admin_client):
    response = admin_client.post(
        "/api/admin/campaigns", json={"subject": "All alumni", "body": "Message"}
    )

    assert response.status_code == 200
    assert response.json()["campaign"]["audience_type"] == "all"
    assert response.json()["campaign"]["audience_value"] is None


@pytest.mark.parametrize(
    ("audience_type", "value", "expected"),
    [("faculty", "science", "Science"), ("department", "computing", "Computing")],
)
def test_api_creates_targeted_campaign(
    admin_client, monkeypatch, audience_type, value, expected
):
    monkeypatch.setattr(
        campaigns,
        "audience_options",
        lambda: {"faculty": ["Science"], "department": ["Computing"]},
    )
    response = admin_client.post(
        "/api/admin/campaigns",
        json={
            "subject": "Department audience",
            "body": "Message",
            "audience_type": audience_type,
            "audience_value": value,
        },
    )
    assert response.status_code == 200
    assert response.json()["campaign"]["audience_type"] == audience_type
    assert response.json()["campaign"]["audience_value"] == expected


@pytest.mark.parametrize("field", ["faculty", "department"])
def test_web_rejects_all_alumni_with_a_target_value(admin_client, field):
    before = len(campaigns.list_campaigns())
    response = admin_client.post(
        "/admin/campaigns",
        data={
            "subject": "Conflicting audience",
            "body": "Message",
            "audience_type": "all",
            field: "Science",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert len(campaigns.list_campaigns()) == before
    assert "Choose only the value for the selected audience." in admin_client.get(
        "/admin/campaigns"
    ).text


def test_admin_sends_campaign(admin_client):
    campaign = _create_campaign(admin_client, "Send demo", "Sent body")

    with isolate_publisher():
        register_website_observer()
        response = admin_client.post(
            f"/admin/campaigns/{campaign['id']}/send", follow_redirects=False
        )

    assert response.status_code == 303
    assert "/admin/campaigns" in response.headers["location"]
    row = next(row for row in campaigns.list_campaigns() if row["id"] == campaign["id"])
    assert row["status"] == "sent"
    page = admin_client.get("/admin/campaigns")
    assert "best-effort and does not confirm inbox receipt" in page.text
    assert "Sent" in page.text


def test_admin_schedules_campaign_with_utc_timestamp(admin_client):
    campaign = _create_campaign(admin_client, "Schedule demo", "Scheduled body")

    response = admin_client.post(
        f"/admin/campaigns/{campaign['id']}/schedule",
        data={"scheduled_at": "2099-06-15T10:00:00Z"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "/admin/campaigns" in response.headers["location"]
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
    assert "/admin/campaigns" in response.headers["location"]
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

def test_moderation_filters_search_kind_sort_and_reset(admin_client, monkeypatch):
    rows = [
        _sample_listing(9101, title="Alpha Job", kind="job", created_at="2099-01-01T00:00:00+00:00"),
        _sample_listing(9102, title="Beta Event", kind="event", created_at="2099-01-02T00:00:00+00:00"),
        _sample_listing(9103, title="Gamma Job", kind="job", created_at="2099-01-03T00:00:00+00:00"),
    ]

    def fake_list_pending(*, q=None, kind=None, sort="created_desc"):
        result = list(rows)
        if kind in {"job", "event"}:
            result = [r for r in result if r["kind"] == kind]
        if q:
            ql = q.lower()
            result = [r for r in result if ql in r["title"].lower() or ql in r["body"].lower()]
        if sort == "created_asc":
            result.sort(key=lambda r: (r["created_at"], r["id"]))
        elif sort == "title_asc":
            result.sort(key=lambda r: (r["title"].lower(), r["id"]))
        elif sort == "title_desc":
            result.sort(key=lambda r: (r["title"].lower(), r["id"]), reverse=True)
        else:
            result.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return result

    monkeypatch.setattr("app.routers.admin_home.boards.list_pending", fake_list_pending)
    monkeypatch.setattr("app.routers.admin_home.get_user", lambda aid: {"display_name": "Test Author"})

    page = admin_client.get("/admin/moderation?q=job&kind=job&sort=title_desc")
    assert page.status_code == 200
    assert "Gamma Job" in page.text
    assert "Alpha Job" in page.text
    assert "Beta Event" not in page.text
    assert page.text.index("Gamma Job") < page.text.index("Alpha Job")
    assert 'name="q"' in page.text and 'value="job"' in page.text
    assert '<option value="job" selected' in page.text
    assert '<option value="title_desc" selected' in page.text
    assert "2 results matching current filters." in page.text
    assert 'href="http://testserver/admin/moderation">Reset</a>' in page.text

    reset = admin_client.get("/admin/moderation")
    assert "Gamma Job" in reset.text and "Beta Event" in reset.text and "Alpha Job" in reset.text
    assert '<option value="created_desc" selected' in reset.text


def test_moderation_invalid_filter_values_fall_back_safely(admin_client, monkeypatch):
    seen = {}

    def fake_list_pending(*, q=None, kind=None, sort="created_desc"):
        seen["args"] = {"q": q, "kind": kind, "sort": sort}
        return [_sample_listing(9201)]

    monkeypatch.setattr("app.routers.admin_home.boards.list_pending", fake_list_pending)
    monkeypatch.setattr("app.routers.admin_home.get_user", lambda aid: {"display_name": "Test Author"})

    page = admin_client.get("/admin/moderation?q=%20+bad+%20&kind=oops&sort=DROP%20TABLE")
    assert page.status_code == 200
    assert seen["args"] == {"q": "bad", "kind": "", "sort": "created_desc"}
    assert 'value="bad"' in page.text
    assert '<option value="" selected' in page.text
    assert '<option value="created_desc" selected' in page.text


def test_moderation_empty_filtered_results_show_reset(admin_client, monkeypatch):
    monkeypatch.setattr("app.routers.admin_home.boards.list_pending", lambda **kwargs: [])
    monkeypatch.setattr("app.routers.admin_home.get_user", lambda aid: {"display_name": "Test Author"})

    page = admin_client.get("/admin/moderation?q=missing&kind=job")
    assert page.status_code == 200
    assert "No matching results." in page.text
    assert 'href="http://testserver/admin/moderation">Reset filters</a>' in page.text
    assert "No job or event listings are awaiting review." not in page.text
    assert 'value="missing"' in page.text


def test_moderation_action_preserves_valid_filters(admin_client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin_home.boards.publish_listing",
        lambda admin_id, listing_id: {"ok": True},
    )
    response = admin_client.post(
        "/admin/listings/999/publish?q=Role&kind=job&sort=title_asc",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin/moderation?q=Role&kind=job&sort=title_asc")


def test_revision_filters_and_preserved_action(admin_client, monkeypatch):
    rows = [
        _sample_revision(9301) | {"requested_at": "2099-01-01T00:00:00+00:00"},
        _sample_revision(9302) | {"requested_at": "2099-01-02T00:00:00+00:00"},
    ]
    rows[0]["original"]["title"] = "Alpha Role"
    rows[0]["original"]["kind"] = "job"
    rows[0]["proposed"]["title"] = "Alpha Role"
    rows[0]["proposed"]["kind"] = "job"
    rows[1]["original"]["title"] = "Beta Event"
    rows[1]["original"]["kind"] = "event"
    rows[1]["proposed"]["title"] = "Beta Event"
    rows[1]["proposed"]["kind"] = "event"

    def fake_list_revision_requests(admin_id, *, q=None, kind=None, sort="requested_desc"):
        result = list(rows)
        if kind in {"job", "event"}:
            result = [r for r in result if r["original"]["kind"] == kind]
        if q:
            ql = q.lower()
            result = [r for r in result if ql in r["original"]["title"].lower()]
        if sort == "requested_asc":
            result.sort(key=lambda r: (r["requested_at"], r["listing_id"]))
        else:
            result.sort(key=lambda r: (r["requested_at"], r["listing_id"]), reverse=True)
        return {"ok": True, "revision_requests": result}

    monkeypatch.setattr("app.routers.admin_home.boards.list_revision_requests", fake_list_revision_requests)
    monkeypatch.setattr("app.routers.admin_home.get_user", lambda aid: {"display_name": "Test Author"})
    monkeypatch.setattr("app.routers.admin_home.boards.approve_revision", lambda admin_id, listing_id: {"ok": True})

    page = admin_client.get("/admin/revisions?kind=event&sort=requested_desc")
    assert page.status_code == 200
    assert "Beta Event" in page.text
    assert "Alpha Role" not in page.text
    assert '<option value="event" selected' in page.text

    response = admin_client.post(
        "/admin/listings/9302/revision-request/approve?kind=event&sort=requested_desc",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin/revisions?kind=event&sort=requested_desc")


def test_deletion_filters_and_preserved_action(admin_client, monkeypatch):
    rows = [
        _sample_listing(9401, title="Zulu Job", kind="job", deletion_requested=True, created_at="2099-01-01T00:00:00+00:00"),
        _sample_listing(9402, title="Alpha Event", kind="event", deletion_requested=True, created_at="2099-01-02T00:00:00+00:00"),
    ]

    def fake_list_deletion_requests(admin_id, *, q=None, kind=None, sort="created_desc"):
        result = list(rows)
        if kind in {"job", "event"}:
            result = [r for r in result if r["kind"] == kind]
        if sort == "title_asc":
            result.sort(key=lambda r: (r["title"].lower(), r["id"]))
        elif sort == "created_asc":
            result.sort(key=lambda r: (r["created_at"], r["id"]))
        else:
            result.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return {"ok": True, "listings": result}

    monkeypatch.setattr("app.routers.admin_home.boards.list_deletion_requests", fake_list_deletion_requests)
    monkeypatch.setattr("app.routers.admin_home.get_user", lambda aid: {"display_name": "Test Author"})
    monkeypatch.setattr("app.routers.admin_home.boards.deny_deletion", lambda admin_id, listing_id: {"ok": True})

    page = admin_client.get("/admin/deletions?sort=title_asc")
    assert page.status_code == 200
    assert page.text.index("Alpha Event") < page.text.index("Zulu Job")
    assert '<option value="title_asc" selected' in page.text

    response = admin_client.post(
        "/admin/listings/9402/deletion-request/deny?sort=title_asc",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin/deletions?sort=title_asc")


def test_campaign_filters_status_search_sort_and_reset(admin_client, monkeypatch):
    rows = [
        _sample_campaign(9501, subject="Beta Draft", body="hello", status="draft"),
        _sample_campaign(9502, subject="Alpha Sent", body="world", status="sent"),
        _sample_campaign(9503, subject="Gamma Scheduled", body="hello there", status="scheduled"),
    ]
    rows[0]["created_at"] = "2099-01-02T00:00:00+00:00"
    rows[1]["created_at"] = "2099-01-03T00:00:00+00:00"
    rows[2]["created_at"] = "2099-01-01T00:00:00+00:00"

    def fake_list_campaigns(*, q=None, status=None, sort="created_desc"):
        result = list(rows)
        if status in {"draft", "scheduled", "sending", "sent"}:
            result = [r for r in result if r["status"] == status]
        if q:
            ql = q.lower()
            result = [r for r in result if ql in r["subject"].lower() or ql in r["body"].lower()]
        if sort == "created_asc":
            result.sort(key=lambda r: (r.get("created_at", ""), r["id"]))
        elif sort == "subject_asc":
            result.sort(key=lambda r: (r["subject"].lower(), r["id"]))
        elif sort == "subject_desc":
            result.sort(key=lambda r: (r["subject"].lower(), r["id"]), reverse=True)
        else:
            result.sort(key=lambda r: (r.get("created_at", ""), r["id"]), reverse=True)
        return result

    monkeypatch.setattr("app.routers.admin_home.campaigns.list_campaigns", fake_list_campaigns)

    page = admin_client.get("/admin/campaigns?q=hello&status=draft&sort=subject_desc")
    assert page.status_code == 200
    assert "Beta Draft" in page.text
    assert "Alpha Sent" not in page.text
    assert "Gamma Scheduled" not in page.text
    assert '<option value="draft" selected' in page.text
    assert '<option value="subject_desc" selected' in page.text
    assert "1 result matching current filters." in page.text

    reset = admin_client.get("/admin/campaigns")
    assert "Alpha Sent" in reset.text
    assert '<option value="created_desc" selected' in reset.text


def test_campaign_action_preserves_filters(admin_client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin_home.campaigns.send_campaign",
        lambda admin_id, campaign_id: {"ok": True, "campaign": {"id": campaign_id}},
    )
    response = admin_client.post(
        "/admin/campaigns/77/send?q=Beta&status=draft&sort=subject_asc",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin/campaigns?q=Beta&status=draft&sort=subject_asc")

def test_listing_filters_service_level_search_kind_sort_and_ties(test_session, monkeypatch):
    from app.services import boards as boards_service
    from tests.listing_test_helpers import complete_listing_payload

    monkeypatch.setattr(boards_service, "engine", test_session.get_bind())
    monkeypatch.setattr("app.services.board_auth.engine", test_session.get_bind())
    admin = add_user(test_session, role="admin", email="admin-list@test.com")
    author = add_user(test_session, role="alumni", email="author-list@test.com")

    def add_listing(title, kind, created_at):
        from app.models.listing import Listing
        payload = complete_listing_payload({"kind": kind, "title": title, "body": f"{title} body"})
        row = Listing(author_id=author.id, **payload, status="pending", created_at=created_at)
        test_session.add(row)
        test_session.commit()
        test_session.refresh(row)
        return row

    l1 = add_listing("Beta Job", "job", "2099-01-01T00:00:00+00:00")
    l2 = add_listing("Alpha Job", "job", "2099-01-01T00:00:00+00:00")
    l3 = add_listing("Gamma Event", "event", "2099-01-02T00:00:00+00:00")

    jobs = boards_service.list_pending(q="job", kind="job", sort="title_asc")
    assert [r["title"] for r in jobs] == ["Alpha Job", "Beta Job"]
    assert all(r["kind"] == "job" for r in jobs)

    newest = boards_service.list_pending(sort="created_desc")
    same_day = [r for r in newest if r["created_at"] == "2099-01-01T00:00:00+00:00"]
    assert [r["id"] for r in same_day] == sorted([l1.id, l2.id], reverse=True)

    invalid = boards_service.list_pending(kind="bad", sort="bad")
    assert [r["id"] for r in invalid[:3]] == [l3.id, l2.id, l1.id]

    del_result = boards_service.list_deletion_requests(admin.id, q="Gamma", kind="event", sort="created_desc")
    assert del_result["ok"] is True
    assert del_result["listings"] == []
