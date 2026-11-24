from fastapi.testclient import TestClient

from app.main import app
from app.seed import seed
from tests.listing_auth_helpers import auth_headers, login
from tests.listing_test_helpers import complete_listing_payload


def setup_module() -> None:
    seed(reset=True)


client = TestClient(app)


def _register_other_alumnus() -> dict:
    response = client.post(
        "/api/auth/register",
        json={
            "email": "listing.security@my.uwi.edu",
            "password": "security-dev",
            "display_name": "Security Alum",
        },
    )
    assert response.status_code == 200
    user = response.json()["user"]
    assert client.post(
        "/api/auth/verify-email",
        headers={"X-User-Id": str(user["id"])},
    ).status_code == 200
    return login(client, "listing.security@my.uwi.edu", "security-dev")


def _submit(user_id: int, title: str) -> dict:
    response = client.post(
        "/api/boards/listings",
        json=complete_listing_payload(
            {"kind": "job", "title": title, "body": "Security test body"}
        ),
        headers=auth_headers(client, user_id),
    )
    assert response.status_code == 200
    return response.json()["listing"]


def _publish(admin_id: int, listing_id: int):
    return client.post(
        f"/api/admin/listings/{listing_id}/publish",
        headers=auth_headers(client, admin_id),
    )


def test_x_user_id_cannot_authenticate_or_impersonate_admin():
    alum = login(client, "alumni.verified@my.uwi.edu", "alumni-dev")
    admin = login(client, "admin@uwi.edu", "admin-dev")
    listing = _submit(alum["id"], "Header Impersonation")
    csrf_token = client.get("/api/auth/csrf").json()["csrf_token"]
    client.cookies.clear()

    anonymous_submit = client.post(
        "/api/boards/listings",
        json=complete_listing_payload(
            {"kind": "job", "title": "Forged Submit", "body": "Body"}
        ),
        headers={
            "X-CSRF-Token": csrf_token,
            "X-User-Id": str(alum["id"]),
        },
    )
    anonymous_publish = client.post(
        f"/api/admin/listings/{listing['id']}/publish",
        headers={
            "X-CSRF-Token": csrf_token,
            "X-User-Id": str(admin["id"]),
        },
    )

    assert anonymous_submit.status_code == 401
    assert anonymous_publish.status_code == 401

    forged_headers = auth_headers(client, alum["id"])
    forged_headers["X-User-Id"] = str(admin["id"])
    assert client.post(
        f"/api/admin/listings/{listing['id']}/publish",
        headers=forged_headers,
    ).status_code == 403


def test_logged_in_user_cannot_impersonate_another_listing_owner():
    owner = login(client, "alumni.verified@my.uwi.edu", "alumni-dev")
    other = _register_other_alumnus()
    admin = login(client, "admin@uwi.edu", "admin-dev")
    listing = _submit(owner["id"], "Owner Only")
    assert _publish(admin["id"], listing["id"]).status_code == 200
    forged_headers = auth_headers(client, other["id"])
    forged_headers["X-User-Id"] = str(owner["id"])
    payload = {
        "kind": "job",
        "title": "Forged Owner Edit",
        "body": "Forged body",
    }

    edit = client.put(
        f"/api/boards/listings/{listing['id']}",
        json=payload,
        headers=forged_headers,
    )
    deletion = client.post(
        f"/api/boards/listings/{listing['id']}/deletion-request",
        headers=forged_headers,
    )
    revision = client.post(
        f"/api/boards/listings/{listing['id']}/revision-request",
        json=payload,
        headers=forged_headers,
    )

    assert edit.status_code == 400
    assert deletion.status_code == 400
    assert revision.status_code == 400


def test_listing_mutations_require_valid_csrf_tokens():
    alum = login(client, "alumni.verified@my.uwi.edu", "alumni-dev")
    payload = complete_listing_payload(
        {"kind": "job", "title": "CSRF Listing", "body": "Body"}
    )
    client.headers.pop("X-CSRF-Token", None)

    missing = client.post("/api/boards/listings", json=payload)
    invalid = client.post(
        "/api/boards/listings",
        json=payload,
        headers={"X-CSRF-Token": "invalid-token"},
    )
    valid = client.post(
        "/api/boards/listings",
        json=payload,
        headers=auth_headers(client, alum["id"]),
    )
    listing_id = valid.json()["listing"]["id"]
    edit_payload = {"kind": "job", "title": "CSRF Edit", "body": "Updated"}
    client.headers.pop("X-CSRF-Token", None)
    missing_put = client.put(
        f"/api/boards/listings/{listing_id}", json=edit_payload
    )
    valid_put = client.put(
        f"/api/boards/listings/{listing_id}",
        json=edit_payload,
        headers=auth_headers(client, alum["id"]),
    )

    assert missing.status_code == 403
    assert invalid.status_code == 403
    assert valid.status_code == 200
    assert missing_put.status_code == 403
    assert valid_put.status_code == 200


def test_admin_routes_use_authenticated_role_and_csrf():
    alum = login(client, "alumni.verified@my.uwi.edu", "alumni-dev")
    admin = login(client, "admin@uwi.edu", "admin-dev")
    listing = _submit(alum["id"], "Admin Security")

    assert client.get(
        "/api/admin/listings/pending",
        headers=auth_headers(client, alum["id"]),
    ).status_code == 403
    assert client.get(
        "/api/admin/listings/pending",
        headers=auth_headers(client, admin["id"]),
    ).status_code == 200
    client.headers.pop("X-CSRF-Token", None)
    assert client.post(
        f"/api/admin/listings/{listing['id']}/publish"
    ).status_code == 403
    assert client.post(
        f"/api/admin/listings/{listing['id']}/publish",
        headers={"X-CSRF-Token": "invalid-token"},
    ).status_code == 403
    assert _publish(admin["id"], listing["id"]).status_code == 200

    rejected = _submit(alum["id"], "Real Admin Rejection")
    assert client.post(
        f"/api/admin/listings/{rejected['id']}/reject",
        json={"rejection_reason": "Admin reviewed."},
        headers=auth_headers(client, admin["id"]),
    ).status_code == 200


def test_public_browse_remains_read_only_and_available_without_login():
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)

    response = client.get("/api/boards")

    assert response.status_code == 200
    assert isinstance(response.json()["listings"], list)
