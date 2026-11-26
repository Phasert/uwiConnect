import jwt
from fastapi.testclient import TestClient

from app.config import get_settings
from tests.csrf_test_helpers import csrf_token_from_html

_credentials: dict[int, tuple[str, str]] = {}

VERIFIED_ALUMNI_EMAIL = "alumni.verified@my.uwi.edu"
VERIFIED_ALUMNI_PASSWORD = "alumni-dev"


def login(client: TestClient, email: str, password: str) -> dict:
    client.headers.pop("X-CSRF-Token", None)
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
    token = client.cookies.get("access_token")
    assert token is not None
    payload = jwt.decode(
        token,
        get_settings().secret_key,
        algorithms=[get_settings().jwt_algorithm],
    )
    user_id = int(payload["sub"])
    _credentials[user_id] = (email, password)
    csrf_token = client.get("/api/auth/csrf").json()["csrf_token"]
    client.headers.update({"X-CSRF-Token": csrf_token})
    return {"id": user_id, "role": payload["role"]}


def auth_headers(client: TestClient, user_id: int) -> dict[str, str]:
    email, password = _credentials[user_id]
    login(client, email, password)
    return {"X-CSRF-Token": client.headers["X-CSRF-Token"]}


def browse_as_verified(
    client: TestClient, url: str = "/api/boards", params: dict | None = None
) -> list[dict]:
    """Read a board URL with the verified-alumni access rule applied."""
    login(client, VERIFIED_ALUMNI_EMAIL, VERIFIED_ALUMNI_PASSWORD)
    response = client.get(url, params=params)
    assert response.status_code == 200, response.text
    return response.json()["listings"]
