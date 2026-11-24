"""End-to-end notification verification against an isolated fake-SMTP stack.

These tests deliberately exercise the real publisher, shared recipient
selector, website store, email delivery store, and SMTP adapter. The SMTP
socket factory is replaced with an in-memory fake, and every service uses the
isolated in-memory SQLite engine supplied by ``test_session``.
"""

from __future__ import annotations

from email.utils import getaddresses
from types import SimpleNamespace
from typing import Self

import pytest
from pydantic import SecretStr
from sqlmodel import Session, select

import app.services.board_auth as board_auth_module
import app.services.boards as boards_module
import app.services.campaigns as campaigns_module
import app.services.email_delivery as email_delivery_module
import app.services.email_notifications as email_notifications_module
import app.services.email_service as email_service_module
import app.services.notification_stream as notification_stream_module
import app.services.notifications as notifications_module
import app.services.recipient_selection as recipient_selection_module
import app.services.website_notifications as website_module
from app.config import Settings
from app.models.email_delivery import EmailDelivery
from app.models.listing import Listing
from app.models.notification import Notification
from app.models.user import User
from app.services.email_notifications import register_email_observer
from app.services.observer import (
    CAMPAIGN_SENT,
    LISTING_PUBLISHED,
    NotificationEvent,
    notification_publisher,
)
from app.services.website_notifications import register_website_observer
from tests.campaign_test_helpers import isolate_publisher


class FakeSmtpClient:
    def __init__(self, transport: FakeSmtpTransport) -> None:
        self._transport = transport

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def starttls(self, *, context: object | None = None) -> None:
        self._transport.tls_calls += 1

    def login(self, username: str, password: str) -> None:
        self._transport.logins.append((username, password))

    def send_message(self, message) -> dict[str, tuple[int, bytes]]:
        recipients = [
            address for _, address in getaddresses([str(message.get("To", ""))])
        ]
        self._transport.messages.append(
            {
                "recipients": recipients,
                "subject": str(message.get("Subject", "")),
                "body": message.get_body(preferencelist=("plain",))
                .get_content()
                .strip(),
            }
        )
        return {
            recipient: (550, b"fake recipient rejection")
            for recipient in recipients
            if recipient in self._transport.fail_recipients
        }


class FakeSmtpTransport:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.connections: list[tuple[object, ...]] = []
        self.logins: list[tuple[str, str]] = []
        self.tls_calls = 0
        self.fail_recipients: set[str] = set()

    def smtp_factory(self, host, port, **kwargs) -> FakeSmtpClient:
        self.connections.append((host, port, kwargs.get("timeout")))
        return FakeSmtpClient(self)


class RecordingObserver:
    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    def __call__(self, event: NotificationEvent) -> None:
        self.events.append(event)


def _settings(*, enabled: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        database_uri="sqlite://",
        secret_key="test-key",
        env="test",
        smtp_host="smtp.invalid.test",
        smtp_port=2525,
        smtp_username="sender@example.test",
        smtp_password=SecretStr("fake-not-a-secret"),
        smtp_from_email="no-reply@example.test",
        smtp_from_name="UWI Connect",
        smtp_use_tls=True,
        smtp_timeout=2.0,
        notification_email_enabled=enabled,
    )


@pytest.fixture
def stack(test_session, monkeypatch):
    bound_engine = test_session.get_bind()
    for module in (
        board_auth_module,
        boards_module,
        campaigns_module,
        email_delivery_module,
        notification_stream_module,
        notifications_module,
        recipient_selection_module,
        website_module,
    ):
        monkeypatch.setattr(module, "engine", bound_engine)

    transport = FakeSmtpTransport()
    monkeypatch.setattr(email_service_module.smtplib, "SMTP", transport.smtp_factory)
    # Avoid reusing a globally registered observer bound to the application DB.
    monkeypatch.setattr(email_notifications_module, "email_notification_observer", None)
    return SimpleNamespace(session=test_session, engine=bound_engine, transport=transport)


def _user(
    session,
    email: str,
    *,
    role: str = "alumni",
    verified: int = 1,
    faculty: str | None = "Science and Technology",
    department: str | None = "Computer Science",
    account_active: bool = True,
    account_revoked: bool = False,
) -> User:
    user = User(
        username=email,
        email=email,
        password="x",
        role=role,
        email_verified=verified,
        display_name="Test",
        programme="Computer Science",
        faculty=faculty,
        department=department,
        account_active=account_active,
        account_revoked=account_revoked,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _audience(session) -> SimpleNamespace:
    return SimpleNamespace(
        matching=_user(session, "matching@my.uwi.edu"),
        unqualified=_user(
            session, "unqualified@my.uwi.edu", faculty=None, department=None
        ),
        other_faculty=_user(session, "other@my.uwi.edu", faculty="Engineering"),
        admin=_user(session, "admin@uwi.edu", role="admin"),
        unverified=_user(session, "unverified@my.uwi.edu", verified=0),
        inactive=_user(session, "inactive@my.uwi.edu", account_active=False),
        revoked=_user(session, "revoked@my.uwi.edu", account_revoked=True),
        external=_user(session, "external@example.com"),
    )


def _pending_listing(
    session,
    admin_id: int,
    *,
    kind: str,
    title: str,
    faculty: str = "Science and Technology",
    department: str = "Computer Science",
) -> Listing:
    listing = Listing(
        author_id=admin_id,
        kind=kind,
        title=title,
        body="Body",
        status="pending",
        created_at="2026-11-24T00:00:00+00:00",
        faculty=faculty,
        department=department,
    )
    session.add(listing)
    session.commit()
    session.refresh(listing)
    return listing


def _email_recipients(transport: FakeSmtpTransport, *, subject: str) -> list[str]:
    recipients: list[str] = []
    for message in transport.messages:
        if message["subject"] == subject:
            recipients.extend(message["recipients"])  # type: ignore[arg-type]
    return recipients


def _notification_rows(bound_engine, event_type: str | None = None) -> list[Notification]:
    with Session(bound_engine) as session:
        statement = select(Notification)
        if event_type is not None:
            statement = statement.where(Notification.event_type == event_type)
        return list(session.exec(statement).all())


def test_approved_job_and_event_publication_reach_shared_audience(stack) -> None:
    people = _audience(stack.session)
    captured = RecordingObserver()
    settings = _settings()

    with isolate_publisher():
        notification_publisher.subscribe(captured)
        register_website_observer()
        assert register_email_observer(settings) is True

        results = [
            boards_module.publish_listing(
                people.admin.id,
                _pending_listing(
                    stack.session,
                    people.admin.id,
                    kind=kind,
                    title=f"Verified {kind.title()}",
                ).id,
            )
            for kind in ("job", "event")
        ]

    assert all(result["ok"] and result["notified"] for result in results)
    assert [event.event_type for event in captured.events] == [
        LISTING_PUBLISHED,
        LISTING_PUBLISHED,
    ]

    expected = [people.matching.email, people.matching.email]
    assert (
        sorted(_email_recipients(stack.transport, subject="New UWI Connect listing"))
        == expected
    )

    rows = _notification_rows(stack.engine, LISTING_PUBLISHED)
    assert {row.user_id for row in rows} == {people.matching.id}
    assert len(rows) == 2

    inbox = notifications_module.inbox(people.matching.id)
    assert len(inbox) == 2
    assert {row["message"] for row in inbox} == {
        "New job on the board: Verified Job",
        "New event on the board: Verified Event",
    }

    accessible, stream_rows = notification_stream_module._read_batch(
        user_id=people.matching.id, cursor=0, batch_size=10
    )
    assert accessible
    assert {row["message"] for row in stream_rows} == {
        row["message"] for row in inbox
    }

    for excluded in (
        people.unqualified,
        people.other_faculty,
        people.admin,
        people.unverified,
        people.inactive,
        people.revoked,
        people.external,
    ):
        assert notifications_module.inbox(excluded.id) == []

    assert not any(
        excluded.email
        in _email_recipients(stack.transport, subject="New UWI Connect listing")
        for excluded in (
            people.unqualified,
            people.other_faculty,
            people.admin,
            people.unverified,
            people.inactive,
            people.revoked,
            people.external,
        )
    )


def test_campaign_includes_unqualified_eligible_alumni_and_replay_is_idempotent(
    stack,
) -> None:
    people = _audience(stack.session)
    captured = RecordingObserver()

    with isolate_publisher():
        notification_publisher.subscribe(captured)
        register_website_observer()
        assert register_email_observer(_settings()) is True

        created = campaigns_module.create_draft(
            people.admin.id, subject=" alumni update ", body="A new opportunity."
        )
        assert created["ok"] is True
        campaign_id = created["campaign"]["id"]
        sent = campaigns_module.send_campaign(people.admin.id, campaign_id)

        assert sent["ok"] is True
        assert sent["campaign"]["status"] == "sent"
        assert len(captured.events) == 1
        event = captured.events[0]
        assert event.event_id == f"campaign-{campaign_id}-sent"

        website_before = _notification_rows(stack.engine, CAMPAIGN_SENT)
        email_before = list(stack.transport.messages)

        notification_publisher.notify(event)
        website_after = _notification_rows(stack.engine, CAMPAIGN_SENT)

    expected_ids = {
        people.matching.id,
        people.unqualified.id,
        people.other_faculty.id,
    }
    assert {row.user_id for row in website_before} == expected_ids
    assert len(website_before) == len(expected_ids)
    assert website_after == website_before

    expected_emails = {
        people.matching.email,
        people.unqualified.email,
        people.other_faculty.email,
    }
    assert (
        set(_email_recipients(stack.transport, subject="UWI Connect campaign"))
        == expected_emails
    )
    assert stack.transport.messages == email_before

    assert all(
        " ".join(str(message["body"]).split())
        == "Campaign: alumni update - A new opportunity."
        for message in stack.transport.messages
    )


def test_partial_smtp_failure_keeps_website_and_healthy_observer_success(stack) -> None:
    people = _audience(stack.session)
    healthy = RecordingObserver()
    settings = _settings()
    stack.transport.fail_recipients = {people.unqualified.email}

    created = campaigns_module.create_draft(
        people.admin.id, subject="Partial failure", body="Some mail is rejected."
    )
    campaign_id = created["campaign"]["id"]
    event = NotificationEvent(
        CAMPAIGN_SENT,
        campaign_id=campaign_id,
        message="Campaign: Partial failure",
        event_id="fake-smtp-partial-failure",
    )

    with isolate_publisher():
        register_website_observer()
        assert register_email_observer(settings) is True
        email_observer = email_notifications_module.email_notification_observer
        assert email_observer is not None
        notification_publisher.subscribe(healthy)

        result = notification_publisher.notify(event)

    assert result.succeeded is False
    assert email_observer in result.failed_observers
    assert website_module.website_notification_observer in result.succeeded_observers
    assert healthy in result.succeeded_observers
    assert healthy.events == [event]

    expected_ids = {
        people.matching.id,
        people.unqualified.id,
        people.other_faculty.id,
    }
    assert {
        row.user_id
        for row in _notification_rows(stack.engine, CAMPAIGN_SENT)
    } == expected_ids

    sent_emails = {
        recipient
        for message in stack.transport.messages
        for recipient in message["recipients"]  # type: ignore[union-attr]
    }
    assert sent_emails == {
        people.matching.email,
        people.unqualified.email,
        people.other_faculty.email,
    }

    with Session(stack.engine) as session:
        recorded = {
            row.recipient
            for row in session.exec(
                select(EmailDelivery).where(EmailDelivery.event_id == event.event_id)
            ).all()
        }
    assert recorded == {
        people.matching.email,
        people.other_faculty.email,
    }

    assert stack.transport.connections == [
        (settings.smtp_host, settings.smtp_port, settings.smtp_timeout)
    ] * 3
    assert stack.transport.logins == [
        (settings.smtp_username, settings.smtp_password.get_secret_value())
    ] * 3
    assert stack.transport.tls_calls == 3


def test_enable_disable_and_duplicate_registration_are_safe(stack) -> None:
    disabled = _settings(enabled=False)
    enabled = _settings()

    with isolate_publisher():
        assert register_email_observer(disabled) is False
        assert email_notifications_module.email_notification_observer is None
        assert notification_publisher._observers == []

        register_website_observer()
        register_website_observer()
        assert notification_publisher._observers == [
            website_module.website_notification_observer
        ]

        assert register_email_observer(enabled) is True
        observer = email_notifications_module.email_notification_observer
        assert observer is not None
        assert register_email_observer(enabled) is True
        assert email_notifications_module.email_notification_observer is observer
        assert notification_publisher._observers == [
            website_module.website_notification_observer,
            observer,
        ]

        assert register_email_observer(disabled) is False
        assert email_notifications_module.email_notification_observer is None
        assert notification_publisher._observers == [
            website_module.website_notification_observer
        ]

    # Registration validates configuration; it must never contact SMTP.
    assert stack.transport.connections == []
    assert stack.transport.messages == []
