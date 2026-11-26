import smtplib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app.database import get_session
from app.main import app
from app.models.user import User
from app.models.verification_resend_limit import VerificationResendLimit
from app.services import accounts, board_auth, notification_stream, verification_resend
from app.services.verification_tokens import issue_verification_token
from app.utilities.security import create_access_token, encrypt_password
from tests.csrf_test_helpers import csrf_token_from_html


@pytest.fixture
def verification_client(test_session, monkeypatch):
    engine = test_session.get_bind()
    monkeypatch.setattr(accounts, "engine", engine)
    monkeypatch.setattr(board_auth, "engine", engine)
    monkeypatch.setattr(notification_stream, "engine", engine)
    monkeypatch.setattr(verification_resend, "_attempts", {})

    def override_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


def add_user(session, email, *, verified, role="alumni"):
    user = User(
        username=email,
        email=email,
        password=encrypt_password("password"),
        role=role,
        email_verified=int(verified),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def csrf(client):
    return client.get("/api/auth/csrf").json()["csrf_token"]


def token_from_email(body):
    url = body.splitlines()[2]
    return parse_qs(urlsplit(url).query)["token"][0]


@pytest.mark.parametrize("verified", [False, True])
def test_web_and_api_login_require_verified_email(
    verification_client, test_session, verified
):
    client = verification_client
    user = add_user(test_session, "login@uwi.edu", verified=verified)

    api = client.post(
        "/api/auth/login", json={"email": user.email, "password": "password"}
    )
    assert api.status_code == (200 if verified else 401)

    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "username": user.username,
            "password": "password",
            "csrf_token": csrf_token_from_html(page.text),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/app" if verified else "/login")
    assert ("access_token" in response.cookies) is verified


def test_existing_session_and_stream_reject_unverified_user(
    verification_client, test_session
):
    client = verification_client
    user = add_user(test_session, "session@uwi.edu", verified=True)
    token = create_access_token({"sub": str(user.id)})
    client.cookies.set("access_token", token)
    assert client.get("/api/notifications").status_code == 200

    user.email_verified = 0
    test_session.commit()
    assert client.get("/api/notifications").status_code == 401
    assert notification_stream._read_batch(user_id=user.id, cursor=0, batch_size=10) == (
        False,
        [],
    )


def test_resend_invalidates_previous_token_and_new_link_is_single_use(
    verification_client, test_session, monkeypatch
):
    client = verification_client
    user = add_user(test_session, "resend@uwi.edu", verified=False)
    old_token = issue_verification_token(
        test_session, user.id, now=datetime.now(timezone.utc) - timedelta(minutes=11)
    )
    test_session.commit()
    deliveries = []
    monkeypatch.setattr(
        "app.services.email_service.SMTPEmailService.send_email",
        lambda _service, recipient, subject, body, **kwargs: deliveries.append(
            (recipient, body)
        ),
    )

    response = client.post(
        "/api/auth/resend-verification",
        json={"email": "RESEND@UWI.EDU"},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 200
    assert response.json() == {"message": verification_resend.RESEND_MESSAGE}
    assert len(deliveries) == 1
    assert deliveries[0][0] == user.email
    new_token = token_from_email(deliveries[0][1])
    assert client.get("/api/auth/verify-email", params={"token": old_token}).status_code == 400
    assert client.get("/api/auth/verify-email", params={"token": new_token}).status_code == 200
    assert client.get("/api/auth/verify-email", params={"token": new_token}).status_code == 400


def test_resend_is_generic_for_unknown_and_verified_accounts(
    verification_client, test_session, monkeypatch
):
    client = verification_client
    add_user(test_session, "verified@uwi.edu", verified=True)
    deliveries = []
    monkeypatch.setattr(
        "app.services.email_service.SMTPEmailService.send_email",
        lambda *args, **kwargs: deliveries.append(args),
    )
    headers = {"X-CSRF-Token": csrf(client)}
    for email in ("unknown@uwi.edu", "verified@uwi.edu"):
        response = client.post(
            "/api/auth/resend-verification", json={"email": email}, headers=headers
        )
        assert response.status_code == 200
        assert response.json() == {"message": verification_resend.RESEND_MESSAGE}
    assert deliveries == []


def test_smtp_failure_during_resend_preserves_previous_link(
    verification_client, test_session, monkeypatch
):
    client = verification_client
    user = add_user(test_session, "failure@uwi.edu", verified=False)
    old_token = issue_verification_token(
        test_session, user.id, now=datetime.now(timezone.utc) - timedelta(minutes=11)
    )
    test_session.commit()

    def fail(*args, **kwargs):
        raise smtplib.SMTPException("delivery failed")

    monkeypatch.setattr("app.services.email_service.SMTPEmailService.send_email", fail)
    response = client.post(
        "/api/auth/resend-verification",
        json={"email": user.email},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 200
    assert response.json() == {"message": verification_resend.RESEND_MESSAGE}
    test_session.expire_all()
    assert test_session.get(User, user.id).email_verified == 0
    assert client.get("/api/auth/verify-email", params={"token": old_token}).status_code == 200


def test_resend_csrf_and_shared_rate_limit(verification_client, monkeypatch):
    client = verification_client
    now = [1000.0]
    monkeypatch.setattr(verification_resend, "monotonic", lambda: now[0])
    assert client.post(
        "/api/auth/resend-verification", json={"email": "unknown@uwi.edu"}
    ).status_code == 403
    assert client.post(
        "/resend-verification", data={"email": "unknown@uwi.edu"}
    ).status_code == 403

    token = csrf(client)
    headers = {"X-CSRF-Token": token}
    for _ in range(4):
        response = client.post(
            "/api/auth/resend-verification",
            json={"email": "unknown@uwi.edu"},
            headers=headers,
        )
        assert response.status_code == 200
    response = client.post(
        "/resend-verification",
        data={"email": "unknown@uwi.edu", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert verification_resend.RESEND_MESSAGE in client.get("/login").text
    assert client.post(
        "/api/auth/resend-verification",
        json={"email": "unknown@uwi.edu"},
        headers=headers,
    ).status_code == 429
    now[0] += 601
    assert client.post(
        "/api/auth/resend-verification",
        json={"email": "unknown@uwi.edu"},
        headers=headers,
    ).status_code == 200


def test_user_list_requires_verified_administrator(verification_client, test_session):
    client = verification_client
    alumni = add_user(test_session, "alumni@uwi.edu", verified=True)
    admin = add_user(test_session, "admin@uwi.edu", verified=True, role="admin")

    assert client.get("/api/users").status_code == 401
    client.cookies.set("access_token", create_access_token({"sub": str(alumni.id)}))
    assert client.get("/api/users").status_code == 403
    client.cookies.set("access_token", create_access_token({"sub": str(admin.id)}))
    response = client.get("/api/users")
    assert response.status_code == 200
    assert {row["email"] for row in response.json()} == {alumni.email, admin.email}
    assert all("password" not in row for row in response.json())


def test_recipient_limit_applies_across_ips_and_restarts(
    verification_client, test_session, monkeypatch
):
    client = verification_client
    user = add_user(test_session, "recipient@uwi.edu", verified=False)
    sent = []
    now = [1000.0]
    monkeypatch.setattr(verification_resend, "time", lambda: now[0])
    monkeypatch.setattr(
        "app.services.email_service.SMTPEmailService.send_email",
        lambda _service, recipient, subject, body, **kwargs: sent.append(body),
    )
    headers = {"X-CSRF-Token": csrf(client)}
    first = client.post(
        "/api/auth/resend-verification", json={"email": user.email}, headers=headers
    )
    assert first.status_code == 200
    assert len(sent) == 1
    assert user.id is not None
    accounts.send_verification_email(user.id)
    assert len(sent) == 1

    other_client = TestClient(app, client=("198.51.100.2", 1234))
    other_headers = {"X-CSRF-Token": csrf(other_client)}
    verification_resend._attempts.clear()
    repeated = other_client.post(
        "/api/auth/resend-verification",
        json={"email": " RECIPIENT@UWI.EDU "},
        headers=other_headers,
    )
    assert repeated.json() == first.json()
    assert len(sent) == 1
    assert client.get(
        "/api/auth/verify-email", params={"token": token_from_email(sent[0])}
    ).status_code == 200

    stored = test_session.exec(select(VerificationResendLimit)).one()
    assert user.email not in stored.recipient_hash
    with Session(test_session.get_bind()) as session:
        assert not verification_resend.claim_resend_recipient(session, user.email)
        now[0] += 600
        assert verification_resend.claim_resend_recipient(session, user.email)


def test_recent_registration_link_survives_resend_from_another_ip(
    verification_client, test_session, monkeypatch
):
    user = add_user(test_session, "recent@uwi.edu", verified=False)
    token = issue_verification_token(test_session, user.id)
    test_session.commit()
    sent = []
    monkeypatch.setattr(
        "app.services.email_service.SMTPEmailService.send_email",
        lambda *args, **kwargs: sent.append(args),
    )
    client = TestClient(app, client=("198.51.100.3", 1234))
    response = client.post(
        "/api/auth/resend-verification",
        json={"email": user.email},
        headers={"X-CSRF-Token": csrf(client)},
    )
    assert response.status_code == 200
    assert sent == []
    assert client.get("/api/auth/verify-email", params={"token": token}).status_code == 200


def test_concurrent_resends_submit_only_one_valid_link(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'resend-concurrent.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            user = add_user(session, "concurrent@uwi.edu", verified=False)
            user_id = user.id
        monkeypatch.setattr(accounts, "engine", engine)
        entered = Event()
        second_started = Event()
        release = Event()
        sent = []

        def capture(_service, recipient, subject, body, **kwargs):
            entered.set()
            assert release.wait(5)
            sent.append(body)

        monkeypatch.setattr(
            "app.services.email_service.SMTPEmailService.send_email", capture
        )

        def resend_again():
            second_started.set()
            accounts.resend_verification_email(user.email)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(accounts.resend_verification_email, user.email)
            assert entered.wait(5)
            second = executor.submit(resend_again)
            assert second_started.wait(5)
            release.set()
            first.result(timeout=10)
            second.result(timeout=10)

        assert len(sent) == 1
        assert accounts.verify_email_token(token_from_email(sent[0]))["ok"]
        with Session(engine) as session:
            assert session.get(User, user_id).email_verified == 1
    finally:
        engine.dispose()
