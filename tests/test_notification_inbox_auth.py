from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import engine
from app.main import app
from app.models.notification import Notification
from app.models.user import User
from app.seed import seed
from app.services.observer import LISTING_PUBLISHED
from app.utilities.security import create_access_token
from tests.listing_auth_helpers import login

OWNER_EMAIL = "alumni.verified@my.uwi.edu"
OTHER_EMAIL = "alumni.pending@gmail.com"
PASSWORD = "alumni-dev"
INBOX_FIELDS = {"id", "user_id", "event_type", "message", "created_at"}


def setup_module() -> None:
    seed(reset=True)


client = TestClient(app)


def _login(email: str) -> dict:
    return login(client, email, PASSWORD)


def _user_id(email: str) -> int:
    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == email)).one()
        assert user.id is not None
        return user.id


def _add_notification(user_id: int, message: str, event_id: str) -> None:
    with Session(engine) as session:
        session.add(
            Notification(
                user_id=user_id,
                event_type=LISTING_PUBLISHED,
                message=message,
                created_at="2026-11-24T00:00:00+00:00",
                event_id=event_id,
            )
        )
        session.commit()


@pytest.fixture(scope="module")
def inbox_users() -> dict[str, int]:
    owner_id = _user_id(OWNER_EMAIL)
    other_id = _user_id(OTHER_EMAIL)

    _add_notification(owner_id, "Inbox owner oldest", "inbox-auth-owner-oldest")
    _add_notification(owner_id, "Inbox owner newest", "inbox-auth-owner-newest")
    _add_notification(other_id, "Inbox other", "inbox-auth-other")

    return {"owner": owner_id, "other": other_id}


def test_authenticated_user_gets_only_own_notifications(inbox_users) -> None:
    owner_id = inbox_users["owner"]
    authenticated = _login(OWNER_EMAIL)

    assert authenticated["id"] == owner_id
    response = client.get("/api/notifications")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"notifications"}
    rows = payload["notifications"]
    assert [row["message"] for row in rows] == [
        "Inbox owner newest",
        "Inbox owner oldest",
    ]
    assert all(row["user_id"] == owner_id for row in rows)
    assert all(set(row) == INBOX_FIELDS for row in rows)


def test_x_user_id_cannot_expose_another_users_inbox(inbox_users) -> None:
    owner_id = inbox_users["owner"]
    other_id = inbox_users["other"]
    authenticated = _login(OTHER_EMAIL)

    assert authenticated["id"] == other_id
    response = client.get(
        "/api/notifications", headers={"X-User-Id": str(owner_id)}
    )

    assert response.status_code == 200
    rows = response.json()["notifications"]
    assert [row["message"] for row in rows] == ["Inbox other"]
    assert all(row["user_id"] == other_id for row in rows)


def test_header_only_notification_request_is_rejected(inbox_users) -> None:
    client.cookies.clear()

    response = client.get(
        "/api/notifications", headers={"X-User-Id": str(inbox_users["owner"])}
    )

    assert response.status_code == 401
    assert "Inbox owner" not in response.text


@pytest.mark.parametrize("token_kind", ["invalid", "expired"])
def test_invalid_or_expired_cookie_is_rejected(inbox_users, token_kind: str) -> None:
    owner_id = inbox_users["owner"]
    if token_kind == "invalid":
        token = "not-a-jwt"
    else:
        token = create_access_token(
            {"sub": str(owner_id)}, expires_delta=timedelta(seconds=-1)
        )

    client.cookies.clear()
    client.cookies.set("access_token", token)
    response = client.get(
        "/api/notifications", headers={"X-User-Id": str(owner_id)}
    )

    assert response.status_code == 401
    assert "Inbox owner" not in response.text


@pytest.mark.parametrize(
    ("account_active", "account_revoked"),
    [
        (False, False),
        (True, True),
    ],
)
def test_inaccessible_account_is_rejected(
    inbox_users, account_active: bool, account_revoked: bool
) -> None:
    owner_id = inbox_users["owner"]
    original: tuple[bool, bool] | None = None

    with Session(engine) as session:
        user = session.get(User, owner_id)
        assert user is not None
        original = (user.account_active, user.account_revoked)
        assert original == (True, False)
        user.account_active = account_active
        user.account_revoked = account_revoked
        session.add(user)
        session.commit()

    try:
        client.cookies.clear()
        client.cookies.set(
            "access_token", create_access_token({"sub": str(owner_id)})
        )
        response = client.get(
            "/api/notifications", headers={"X-User-Id": str(owner_id)}
        )

        assert response.status_code == 401
        assert "Inbox owner" not in response.text
    finally:
        if original is not None:
            with Session(engine) as session:
                user = session.get(User, owner_id)
                assert user is not None
                user.account_active, user.account_revoked = original
                session.add(user)
                session.commit()
