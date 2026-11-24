import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.dependencies.auth import get_current_admin, get_current_user
from app.models.user import User
from app.utilities.security import create_access_token


def _add_user(test_session, *, username: str, role: str) -> User:
    user = User(
        username=username,
        email=f"{username}@uwi.edu",
        password="hashed-password",
        role=role,
    )
    test_session.add(user)
    test_session.commit()
    test_session.refresh(user)
    return user


def _request_with_token(
    token: str | None = None, *, x_user_id: int | None = None
) -> Request:
    headers = []
    if token is not None:
        headers.append((b"cookie", f"access_token={token}".encode("ascii")))
    if x_user_id is not None:
        headers.append((b"x-user-id", str(x_user_id).encode("ascii")))
    return Request({"type": "http", "headers": headers})


def _authenticate(request: Request, test_session) -> User:
    return asyncio.run(get_current_user(request, test_session))


def test_valid_jwt_resolves_user_and_database_role(test_session) -> None:
    user = _add_user(test_session, username="alumni", role="alumni")
    token = create_access_token({"sub": str(user.id), "role": "admin"})

    authenticated_user = _authenticate(_request_with_token(token), test_session)

    assert authenticated_user.id == user.id
    assert authenticated_user.role == "alumni"


def test_missing_jwt_returns_401_even_with_user_id_header(test_session) -> None:
    user = _add_user(test_session, username="alumni", role="alumni")

    with pytest.raises(HTTPException) as exc_info:
        _authenticate(_request_with_token(x_user_id=user.id), test_session)

    assert exc_info.value.status_code == 401


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        create_access_token({"role": "admin"}),
        create_access_token({"sub": "not-an-id"}),
        create_access_token({"sub": "0"}),
    ],
)
def test_invalid_jwt_or_subject_returns_401(test_session, token: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        _authenticate(_request_with_token(token), test_session)

    assert exc_info.value.status_code == 401


def test_expired_jwt_returns_401(test_session) -> None:
    from datetime import timedelta

    user = _add_user(test_session, username="alumni", role="alumni")
    token = create_access_token(
        {"sub": str(user.id)}, expires_delta=timedelta(seconds=-1)
    )

    with pytest.raises(HTTPException) as exc_info:
        _authenticate(_request_with_token(token), test_session)

    assert exc_info.value.status_code == 401


def test_alumni_cannot_access_admin_dependency(test_session) -> None:
    user = _add_user(test_session, username="alumni", role="alumni")
    token = create_access_token({"sub": str(user.id), "role": "admin"})
    authenticated_user = _authenticate(_request_with_token(token), test_session)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_current_admin(authenticated_user))

    assert exc_info.value.status_code == 403


def test_administrator_can_access_admin_dependency(test_session) -> None:
    user = _add_user(test_session, username="admin", role="admin")
    token = create_access_token({"sub": str(user.id), "role": "alumni"})
    authenticated_user = _authenticate(_request_with_token(token), test_session)

    assert asyncio.run(get_current_admin(authenticated_user)) is user


def test_database_role_change_applies_on_next_authentication(test_session) -> None:
    user = _add_user(test_session, username="changing-user", role="alumni")
    token = create_access_token({"sub": str(user.id), "role": "admin"})
    authenticated_user = _authenticate(_request_with_token(token), test_session)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_current_admin(authenticated_user))
    assert exc_info.value.status_code == 403

    user.role = "admin"
    test_session.add(user)
    test_session.commit()
    test_session.expire(user)

    authenticated_user = _authenticate(_request_with_token(token), test_session)

    assert authenticated_user.role == "admin"
    assert asyncio.run(get_current_admin(authenticated_user)) is user


@pytest.mark.parametrize("other_role", ["alumni", "admin"])
def test_x_user_id_cannot_change_cookie_authenticated_identity(
    test_session, other_role: str
) -> None:
    user = _add_user(test_session, username="authenticated", role="alumni")
    other_user = _add_user(test_session, username=f"other-{other_role}", role=other_role)
    token = create_access_token({"sub": str(user.id)})

    authenticated_user = _authenticate(
        _request_with_token(token, x_user_id=other_user.id), test_session
    )

    assert authenticated_user.id == user.id
    assert authenticated_user.role == "alumni"
