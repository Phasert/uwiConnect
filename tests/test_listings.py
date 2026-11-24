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


def test_new_job_listing_is_pending():
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")

    result = _submit(
        alum["id"], {"kind": "job", "title": "Test Job", "body": "Job body"}
    )

    assert result["listing"]["status"] == "pending"


def test_new_event_listing_is_pending():
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")

    result = _submit(
        alum["id"], {"kind": "event", "title": "Test Event", "body": "Event body"}
    )

    assert result["listing"]["status"] == "pending"


def test_alumni_cannot_choose_their_own_status():
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")

    for bad_status in ("published", "approved"):
        result = _submit(
            alum["id"],
            {
                "kind": "job",
                "title": f"Sneaky {bad_status} job",
                "body": "Trying to bypass review",
                "status": bad_status,
            },
        )
        assert result["listing"]["status"] == "pending"


def test_submission_is_not_published_until_admin_publishes():
    alum = _login("alumni.verified@my.uwi.edu", "alumni-dev")
    admin = _login("admin@uwi.edu", "admin-dev")

    result = _submit(
        alum["id"],
        {"kind": "event", "title": "Unpublished Event", "body": "Not public yet."},
    )
    listing_id = result["listing"]["id"]

    for url in ("/api/boards", "/api/boards?kind=event", "/api/boards?kind=job"):
        public_ids = [row["id"] for row in client.get(url).json()["listings"]]
        assert listing_id not in public_ids

    pending_ids = [
        row["id"]
        for row in client.get(
            "/api/admin/listings/pending",
            headers=auth_headers(client, admin["id"]),
        ).json()["listings"]
    ]
    assert listing_id in pending_ids

    inbox = client.get(
        "/api/notifications", headers={"X-User-Id": str(alum["id"])}
    ).json()["notifications"]
    assert not any("Unpublished Event" in row["message"] for row in inbox)
