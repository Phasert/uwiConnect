import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import seed
from app.services.observer import (
    CAMPAIGN_SENT,
    LISTING_PUBLISHED,
    notification_publisher,
)
from app.services.website_notifications import (
    register_website_observer,
    website_notification_observer,
)
from tests.listing_test_helpers import complete_listing_payload


def setup_module() -> None:
    seed(reset=True)
    register_website_observer()


def teardown_module() -> None:
    notification_publisher.unsubscribe(website_notification_observer)


client = TestClient(app)


class RecordingObserver:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)


@pytest.fixture
def events():
    observer = RecordingObserver()
    notification_publisher.subscribe(observer)
    yield observer.events
    notification_publisher.unsubscribe(observer)


def _login(email: str, password: str) -> dict:
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200
    return r.json()["user"]


def _submit(user_id: int, payload: dict) -> dict:
    r = client.post(
        "/api/boards/listings",
        json=complete_listing_payload(payload),
        headers={"X-User-Id": str(user_id)},
    )
    assert r.status_code == 200
    return r.json()


def _publish(user_id: int, listing_id: int):
    return client.post(
        f"/api/admin/listings/{listing_id}/publish",
        headers={"X-User-Id": str(user_id)},
    )


def _reject(user_id: int, listing_id: int):
    return client.post(
        f"/api/admin/listings/{listing_id}/reject",
        headers={"X-User-Id": str(user_id)},
    )


def _inbox(user_id: int) -> list:
    r = client.get("/api/notifications", headers={"X-User-Id": str(user_id)})
    assert r.status_code == 200
    return r.json()["notifications"]


def _create_campaign(user_id: int, subject: str, body: str) -> dict:
    r = client.post(
        "/api/admin/campaigns",
        json={"subject": subject, "body": body},
        headers={"X-User-Id": str(user_id)},
    )
    assert r.status_code == 200
    return r.json()


def _send_campaign(user_id: int, campaign_id: int):
    return client.post(
        f"/api/admin/campaigns/{campaign_id}/send",
        headers={"X-User-Id": str(user_id)},
    )


def _schedule_campaign(user_id: int, campaign_id: int, scheduled_at: str):
    return client.post(
        f"/api/admin/campaigns/{campaign_id}/schedule",
        json={"scheduled_at": scheduled_at},
        headers={"X-User-Id": str(user_id)},
    )


def test_publishing_job_emits_one_listing_published_event(events):
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")
    admin = _login("admin@uwi.edu", "admin-dev")
    listing_id = _submit(
        alum["id"], {"kind": "job", "title": "Observer Job", "body": "Body"}
    )["listing"]["id"]

    r = _publish(admin["id"], listing_id)

    assert r.status_code == 200
    assert len(events) == 1
    assert events[0].event_type == LISTING_PUBLISHED


def test_publishing_event_emits_one_listing_published_event(events):
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")
    admin = _login("admin@uwi.edu", "admin-dev")
    listing_id = _submit(
        alum["id"], {"kind": "event", "title": "Observer Event", "body": "Body"}
    )["listing"]["id"]

    r = _publish(admin["id"], listing_id)

    assert r.status_code == 200
    assert len(events) == 1
    assert events[0].event_type == LISTING_PUBLISHED


def test_event_contains_correct_listing_information(events):
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")
    admin = _login("admin@uwi.edu", "admin-dev")
    listing_id = _submit(
        alum["id"], {"kind": "job", "title": "Observer Job", "body": "Body"}
    )["listing"]["id"]

    _publish(admin["id"], listing_id)

    event = events[0]
    assert event.listing_id == listing_id
    assert event.kind == "job"
    assert event.message == "New job on the board: Observer Job"


def test_submitting_listing_does_not_emit_event(events):
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")

    _submit(alum["id"], {"kind": "job", "title": "Quiet Job", "body": "Body"})

    assert events == []


def test_rejecting_listing_does_not_emit_event(events):
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")
    admin = _login("admin@uwi.edu", "admin-dev")
    listing_id = _submit(
        alum["id"], {"kind": "job", "title": "Rejected Job", "body": "Body"}
    )["listing"]["id"]

    r = _reject(admin["id"], listing_id)

    assert r.status_code == 200
    assert events == []


def test_repeated_and_invalid_publish_attempts_emit_no_extra_events(events):
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")
    admin = _login("admin@uwi.edu", "admin-dev")
    listing_id = _submit(
        alum["id"], {"kind": "job", "title": "Guarded Job", "body": "Body"}
    )["listing"]["id"]

    assert _publish(admin["id"], listing_id).status_code == 200
    assert len(events) == 1

    assert _publish(admin["id"], listing_id).status_code == 400
    assert _publish(admin["id"], 999999).status_code == 400

    rejected_id = _submit(
        alum["id"], {"kind": "event", "title": "Rejected Event", "body": "Body"}
    )["listing"]["id"]
    assert _reject(admin["id"], rejected_id).status_code == 200
    assert _publish(admin["id"], rejected_id).status_code == 400

    assert len(events) == 1


def test_publishing_via_api_notifies_verified_alumni_only():
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")
    unverified = _login("alumni.pending@gmail.com", "alumni-dev")
    admin = _login("admin@uwi.edu", "admin-dev")

    for kind in ("job", "event"):
        title = f"API Notify {kind.title()}"
        listing_id = _submit(
            alum["id"], {"kind": kind, "title": title, "body": "Body"}
        )["listing"]["id"]

        r = _publish(admin["id"], listing_id)

        assert r.status_code == 200
        alum_rows = [row for row in _inbox(alum["id"]) if title in row["message"]]
        assert len(alum_rows) == 1
        assert alum_rows[0]["event_type"] == LISTING_PUBLISHED
        assert set(alum_rows[0]) == {
            "id",
            "user_id",
            "event_type",
            "message",
            "created_at",
        }
        assert not any(title in row["message"] for row in _inbox(unverified["id"]))
        assert not any(title in row["message"] for row in _inbox(admin["id"]))


def test_repeated_publish_does_not_duplicate_notifications():
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")
    admin = _login("admin@uwi.edu", "admin-dev")
    title = "Repeat Notify Job"
    listing_id = _submit(
        alum["id"], {"kind": "job", "title": title, "body": "Body"}
    )["listing"]["id"]

    assert _publish(admin["id"], listing_id).status_code == 200
    assert len([row for row in _inbox(alum["id"]) if title in row["message"]]) == 1

    assert _publish(admin["id"], listing_id).status_code == 400
    assert len([row for row in _inbox(alum["id"]) if title in row["message"]]) == 1


def test_pending_and_rejected_listings_create_no_notifications():
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")
    admin = _login("admin@uwi.edu", "admin-dev")

    pending_title = "Silent Pending Job"
    _submit(alum["id"], {"kind": "job", "title": pending_title, "body": "Body"})

    rejected_title = "Silent Rejected Job"
    rejected_id = _submit(
        alum["id"], {"kind": "job", "title": rejected_title, "body": "Body"}
    )["listing"]["id"]
    assert _reject(admin["id"], rejected_id).status_code == 200

    inbox = _inbox(alum["id"])
    assert not any(pending_title in row["message"] for row in inbox)
    assert not any(rejected_title in row["message"] for row in inbox)


def test_sending_campaign_via_api_notifies_verified_alumni_only():
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")
    unverified = _login("alumni.pending@gmail.com", "alumni-dev")
    admin = _login("admin@uwi.edu", "admin-dev")
    subject = "API Campaign Notify"
    body = "A new alumni opportunity."

    campaign_id = _create_campaign(admin["id"], subject, body)["campaign"]["id"]
    r = _send_campaign(admin["id"], campaign_id)

    assert r.status_code == 200
    rows = [row for row in _inbox(alum["id"]) if subject in row["message"]]
    assert len(rows) == 1
    assert rows[0]["event_type"] == CAMPAIGN_SENT
    assert rows[0]["message"] == f"Campaign: {subject} - {body}"
    assert not any(subject in row["message"] for row in _inbox(unverified["id"]))
    assert not any(subject in row["message"] for row in _inbox(admin["id"]))


def test_schedule_campaign_via_api():
    admin = _login("admin@uwi.edu", "admin-dev")
    campaign_id = _create_campaign(
        admin["id"],
        "Scheduled API Campaign",
        "Scheduled content.",
    )["campaign"]["id"]

    r = _schedule_campaign(
        admin["id"],
        campaign_id,
        "2099-01-02T09:30:00-04:00",
    )

    assert r.status_code == 200
    assert r.json()["campaign"]["status"] == "scheduled"
    assert r.json()["campaign"]["scheduled_at"] == "2099-01-02T13:30:00+00:00"


def test_schedule_campaign_via_api_rejects_invalid_and_sent_campaign():
    admin = _login("admin@uwi.edu", "admin-dev")
    campaign_id = _create_campaign(
        admin["id"],
        "Invalid Schedule Campaign",
        "No delivery.",
    )["campaign"]["id"]

    invalid = _schedule_campaign(
        admin["id"],
        campaign_id,
        "2020-01-01T00:00:00Z",
    )
    assert invalid.status_code == 400

    sent_id = _create_campaign(
        admin["id"],
        "Already Sent Campaign",
        "Immediate content.",
    )["campaign"]["id"]
    assert _send_campaign(admin["id"], sent_id).status_code == 200
    sent_schedule = _schedule_campaign(
        admin["id"],
        sent_id,
        "2099-01-01T00:00:00Z",
    )
    assert sent_schedule.status_code == 400
