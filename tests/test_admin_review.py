from fastapi.testclient import TestClient

from app.main import app
from app.seed import seed
from tests.listing_auth_helpers import auth_headers, login
from tests.listing_test_helpers import complete_listing_payload


def setup_module() -> None:
    seed(reset=True)


client = TestClient(app)


def _login(email: str, password: str) -> dict:
    return login(client, email, password)


def _submit(user_id: int, payload: dict) -> dict:
    r = client.post(
        "/api/boards/listings",
        json=complete_listing_payload(payload),
        headers=auth_headers(client, user_id),
    )
    assert r.status_code == 200
    return r.json()


def _publish(user_id: int, listing_id: int):
    return client.post(
        f"/api/admin/listings/{listing_id}/publish",
        headers=auth_headers(client, user_id),
    )


def _reject(user_id: int, listing_id: int, rejection_reason: str | None = None):
    json = None
    if rejection_reason is not None:
        json = {"rejection_reason": rejection_reason}
    return client.post(
        f"/api/admin/listings/{listing_id}/reject",
        headers=auth_headers(client, user_id),
        json=json,
    )


def _pending_ids(admin_id: int) -> list:
    rows = client.get(
        "/api/admin/listings/pending", headers=auth_headers(client, admin_id)
    ).json()["listings"]
    return [row["id"] for row in rows]


def _public_ids(url: str) -> list:
    return [row["id"] for row in client.get(url).json()["listings"]]


def test_alumni_cannot_approve_or_reject():
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")
    admin = _login("admin@uwi.edu", "admin-dev")
    listing_id = _submit(
        alum["id"], {"kind": "job", "title": "Guarded Job", "body": "Body"}
    )["listing"]["id"]

    assert _publish(alum["id"], listing_id).status_code == 403
    assert _reject(alum["id"], listing_id).status_code == 403

    assert listing_id in _pending_ids(admin["id"])
    assert listing_id not in _public_ids("/api/boards?kind=job")


def test_admin_approval_publishes_pending_listing():
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")
    admin = _login("admin@uwi.edu", "admin-dev")
    listing_id = _submit(
        alum["id"], {"kind": "event", "title": "Approved Event", "body": "Body"}
    )["listing"]["id"]

    r = _publish(admin["id"], listing_id)

    assert r.status_code == 200
    assert r.json()["listing"]["status"] == "published"
    assert listing_id in _public_ids("/api/boards?kind=event")
    assert listing_id not in _pending_ids(admin["id"])


def test_admin_rejection_hides_pending_listing():
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")
    admin = _login("admin@uwi.edu", "admin-dev")
    listing_id = _submit(
        alum["id"], {"kind": "job", "title": "Rejected Job", "body": "Body"}
    )["listing"]["id"]

    r = _reject(admin["id"], listing_id, "  Add a salary range.  ")

    assert r.status_code == 200
    assert r.json()["listing"]["status"] == "rejected"
    assert r.json()["listing"]["rejection_reason"] == "Add a salary range."
    assert listing_id not in _public_ids("/api/boards")
    assert listing_id not in _public_ids("/api/boards?kind=job")
    assert listing_id not in _pending_ids(admin["id"])


def test_admin_rejects_event_without_reason_for_compatibility():
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")
    admin = _login("admin@uwi.edu", "admin-dev")
    listing_id = _submit(
        alum["id"], {"kind": "event", "title": "Rejected Event", "body": "Body"}
    )["listing"]["id"]

    r = _reject(admin["id"], listing_id)

    assert r.status_code == 200
    assert r.json()["listing"]["status"] == "rejected"
    assert r.json()["listing"]["rejection_reason"] is None


def test_invalid_listing_id_returns_error():
    admin = _login("admin@uwi.edu", "admin-dev")

    assert _publish(admin["id"], 999999).status_code == 400
    assert _reject(admin["id"], 999999).status_code == 400


def test_repeated_approval_or_rejection_is_rejected():
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")
    admin = _login("admin@uwi.edu", "admin-dev")

    job_id = _submit(
        alum["id"], {"kind": "job", "title": "Double Publish", "body": "Body"}
    )["listing"]["id"]
    assert _publish(admin["id"], job_id).status_code == 200
    assert _publish(admin["id"], job_id).status_code == 400
    assert _reject(admin["id"], job_id).status_code == 400

    event_id = _submit(
        alum["id"], {"kind": "event", "title": "Double Reject", "body": "Body"}
    )["listing"]["id"]
    assert _reject(admin["id"], event_id).status_code == 200
    assert _reject(admin["id"], event_id).status_code == 400
    assert _publish(admin["id"], event_id).status_code == 400
