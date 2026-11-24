from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from csrf_test_helpers import csrf_token_from_html
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.routers import server_config
from app.seed import seed
from app.utilities import security

ALUMNI_USERNAME = "alumni.verified@my.uwi.edu"
ALUMNI_PASSWORD = "alumni-dev"


def setup_module() -> None:
    seed(reset=True)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _login(client: TestClient) -> tuple[str, str]:
    csrf_token = csrf_token_from_html(client.get("/login").text)
    response = client.post(
        "/login",
        data={
            "username": ALUMNI_USERNAME,
            "password": ALUMNI_PASSWORD,
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    jwt_cookie = client.cookies.get("access_token")
    assert jwt_cookie is not None
    return csrf_token, jwt_cookie


def test_safe_get_does_not_require_csrf_header(client: TestClient) -> None:
    response = client.get("/login")

    assert response.status_code == 200
    assert "X-CSRF-Token" not in response.request.headers


@pytest.mark.parametrize("token", [None, "invalid-token"], ids=["missing", "invalid"])
def test_login_rejects_missing_or_invalid_csrf_token(
    client: TestClient, token: str | None
) -> None:
    data = {"username": ALUMNI_USERNAME, "password": ALUMNI_PASSWORD}
    if token is not None:
        data["csrf_token"] = token

    response = client.post("/login", data=data, follow_redirects=False)

    assert response.status_code == 403
    assert client.cookies.get("access_token") is None


def test_valid_native_login_rotates_csrf_token_and_keeps_jwt_private(
    client: TestClient,
) -> None:
    old_token, jwt_cookie = _login(client)
    payload = jwt.decode(
        jwt_cookie,
        get_settings().secret_key,
        algorithms=[get_settings().jwt_algorithm],
    )
    assert "csrf_token" not in payload

    page = client.get("/app")
    new_token = csrf_token_from_html(page.text)
    assert new_token != old_token

    response = client.get("/api/auth/csrf")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"csrf_token"}
    assert response.json()["csrf_token"] == new_token
    assert "access_token" not in response.text
    assert client.cookies.get("access_token") == jwt_cookie


def test_csrf_token_endpoint_works_without_jwt(client: TestClient) -> None:
    response = client.get("/api/auth/csrf")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"csrf_token"}
    assert client.cookies.get("access_token") is None


def test_public_registration_remains_available_without_jwt(
    client: TestClient,
) -> None:
    csrf_token = csrf_token_from_html(client.get("/register").text)
    username = f"csrf-{uuid4().hex}"

    response = client.post(
        "/register",
        data={
            "username": username,
            "email": f"{username}@uwi.edu",
            "password": "csrf-registration-password",
            "programme": "Computer Science",
            "faculty": "FST",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert client.cookies.get("access_token") is None


def test_logout_requires_post_and_valid_csrf_then_clears_jwt(
    client: TestClient,
) -> None:
    _login(client)
    jwt_cookie = client.cookies.get("access_token")
    assert jwt_cookie is not None

    get_response = client.get("/logout", follow_redirects=False)
    assert get_response.status_code == 405
    assert client.cookies.get("access_token") == jwt_cookie

    missing_token_response = client.post("/logout", follow_redirects=False)
    assert missing_token_response.status_code == 403
    assert client.cookies.get("access_token") == jwt_cookie

    csrf_token = client.get("/api/auth/csrf").json()["csrf_token"]
    logout_response = client.post(
        "/logout",
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )

    assert logout_response.status_code == 303
    assert client.cookies.get("access_token") is None


def test_config_native_login_and_actions_require_csrf(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        server_config,
        "get_settings",
        lambda: SimpleNamespace(config_password="test-config-password"),
    )
    csrf_token = csrf_token_from_html(client.get("/config/login").text)

    login_response = client.post(
        "/config/login",
        data={"password": "test-config-password", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert login_response.status_code == 303

    action_response = client.post(
        "/config/actions/seed",
        headers={"X-CSRF-Token": "invalid-token"},
        follow_redirects=False,
    )

    assert action_response.status_code == 403


def test_access_token_cookie_is_lax_and_secure_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(env="production"),
    )

    cookie_options = security.access_token_cookie_kwargs()

    assert cookie_options == {
        "httponly": True,
        "samesite": "lax",
        "secure": True,
    }
