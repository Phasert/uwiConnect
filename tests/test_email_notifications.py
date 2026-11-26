import asyncio
from argparse import Namespace
from collections.abc import Iterable
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text
from sqlmodel import select

import app.cli as cli_module
import app.database as database_module
import app.main as main_module
import app.services.campaigns as campaigns_module
import app.services.email_delivery as email_delivery_module
import app.services.email_notifications as email_notifications_module
import app.services.recipient_selection as selection_module
import app.services.website_notifications as website_module
from app.config import Settings
from app.models.email_delivery import (
    EMAIL_DELIVERY_RECIPIENT_EVENT_INDEX,
    EmailDelivery,
)
from app.models.listing import Listing
from app.models.notification import Notification
from app.models.user import User
from app.services.email_delivery import EmailDeliveryStore, EmailDeliveryStoreError
from app.services.email_notifications import (
    EmailDeliveryError,
    EmailNotificationObserver,
    register_email_observer,
)
from app.services.email_sender import (
    EmailConfigurationError,
    EmailSendError,
    SmtpEmailSender,
)
from app.services.observer import (
    CAMPAIGN_SENT,
    LISTING_PUBLISHED,
    NotificationEvent,
    NotificationPublisher,
    NotificationRecipient,
    notification_publisher,
)
from app.services.recipient_selection import email_recipient_selector
from app.services.website_notifications import website_notification_observer
from tests.campaign_test_helpers import add_campaign, add_user, isolate_publisher


class FakeSender:
    def __init__(self, failures: Iterable[str] = ()) -> None:
        self.failures = set(failures)
        self.calls: list[dict[str, str]] = []

    def send(self, *, recipient: str, subject: str, message: str) -> None:
        self.calls.append(
            {"recipient": recipient, "subject": subject, "message": message}
        )
        if recipient in self.failures:
            raise EmailSendError("fake SMTP rejection")


class FailingRecordStore(EmailDeliveryStore):
    def __init__(self, wrapped: EmailDeliveryStore, *fail_recipients: str) -> None:
        self._wrapped = wrapped
        self._fail_recipients = set(fail_recipients)

    def was_sent(self, *, recipient: str, event_id: str) -> bool:
        return self._wrapped.was_sent(recipient=recipient, event_id=event_id)

    def record_success(self, **kwargs) -> None:
        if kwargs["recipient"] in self._fail_recipients:
            raise EmailDeliveryStoreError("recording failed")
        self._wrapped.record_success(**kwargs)


class InvalidConfigService:
    def validate_configuration(self) -> None:
        raise ValueError("SMTP host and sender email must be configured")

    def send_email(self, *args, **kwargs) -> None:
        raise AssertionError("send_email must not be called without configuration")


class ValidatingService:
    def __init__(self) -> None:
        self.validation_calls = 0

    def validate_configuration(self) -> None:
        self.validation_calls += 1

    def send_email(self, *args, **kwargs) -> None:
        raise AssertionError("startup validation must not send email")


class RecordingObserver:
    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    def __call__(self, event: NotificationEvent) -> None:
        self.events.append(event)


@pytest.fixture
def delivery_store(test_session, monkeypatch) -> EmailDeliveryStore:
    monkeypatch.setattr(email_delivery_module, "engine", test_session.get_bind())
    return EmailDeliveryStore()


@pytest.fixture
def website_engine(test_session, monkeypatch) -> None:
    monkeypatch.setattr(website_module, "engine", test_session.get_bind())
    monkeypatch.setattr(selection_module, "engine", test_session.get_bind())


def _settings(**overrides):
    values = {
        "database_uri": "sqlite://",
        "secret_key": "test-key",
        "env": "test",
        "smtp_host": "smtp.example.test",
        "smtp_port": 587,
        "smtp_username": "sender@example.test",
        "smtp_password": SecretStr("not-a-real-password"),
        "smtp_from_email": "no-reply@example.test",
        "smtp_from_name": "UWI Connect",
        "smtp_use_tls": True,
        "smtp_timeout": 10.0,
        "notification_email_enabled": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _eligible_user(
    test_session,
    email: str,
    *,
    faculty: str | None = "Science and Technology",
    department: str | None = "Computer Science",
) -> User:
    user = add_user(test_session, role="alumni", email=email)
    user.faculty = faculty
    user.department = department
    test_session.add(user)
    test_session.commit()
    test_session.refresh(user)
    return user


def _listing(test_session, *, faculty: str, department: str) -> Listing:
    listing = Listing(
        author_id=1,
        kind="job",
        title="Targeted Role",
        body="Body",
        status="published",
        created_at="now",
        faculty=faculty,
        department=department,
    )
    test_session.add(listing)
    test_session.commit()
    test_session.refresh(listing)
    return listing


def _recipients(*addresses: str):
    def select_recipients(event: NotificationEvent) -> list[str]:
        return list(addresses)

    return select_recipients


def _listing_event(
    event_id: str = "listing-email-1", *, listing_id: int = 7
) -> NotificationEvent:
    return NotificationEvent(
        LISTING_PUBLISHED,
        listing_id=listing_id,
        message="New software engineering job",
        event_id=event_id,
    )


def _campaign_event(
    event_id: str = "campaign-email-1",
    recipients: tuple[NotificationRecipient, ...] = (
        NotificationRecipient(1, "one@my.uwi.edu"),
        NotificationRecipient(2, "two@uwi.edu"),
    ),
) -> NotificationEvent:
    return NotificationEvent(
        CAMPAIGN_SENT,
        campaign_id=3,
        message="Campaign announcement",
        event_id=event_id,
        recipients=recipients,
    )


def _rows(test_session) -> list[EmailDelivery]:
    return test_session.exec(select(EmailDelivery).order_by(EmailDelivery.id)).all()


def test_observer_sends_to_explicit_recipients_and_tracks_success_separately(
    test_session, delivery_store
) -> None:
    sender = FakeSender()
    observer = EmailNotificationObserver(
        sender=sender,
        recipient_selector=_recipients("one@my.uwi.edu", "two@uwi.edu"),
        delivery_store=delivery_store,
    )
    event = _listing_event()

    observer(event)

    assert [call["recipient"] for call in sender.calls] == [
        "one@my.uwi.edu",
        "two@uwi.edu",
    ]
    assert {call["subject"] for call in sender.calls} == {"New UWI Connect listing"}
    assert {call["message"] for call in sender.calls} == {
        "New software engineering job"
    }
    rows = _rows(test_session)
    assert {(row.recipient, row.event_id, row.event_type) for row in rows} == {
        ("one@my.uwi.edu", event.event_id, LISTING_PUBLISHED),
        ("two@uwi.edu", event.event_id, LISTING_PUBLISHED),
    }
    assert test_session.exec(select(Notification)).all() == []


def test_campaign_event_sends_to_all_resolved_recipients(
    test_session, delivery_store
) -> None:
    sender = FakeSender()
    observer = EmailNotificationObserver(
        sender=sender,
        recipient_selector=_recipients("one@my.uwi.edu", "two@uwi.edu"),
        delivery_store=delivery_store,
    )
    event = _campaign_event()

    observer(event)

    assert [call["recipient"] for call in sender.calls] == [
        "one@my.uwi.edu",
        "two@uwi.edu",
    ]
    assert {call["subject"] for call in sender.calls} == {"UWI Connect campaign"}
    assert {row.event_type for row in _rows(test_session)} == {CAMPAIGN_SENT}


def test_replaying_event_skips_recorded_successes(test_session, delivery_store) -> None:
    sender = FakeSender()
    observer = EmailNotificationObserver(
        sender=sender,
        recipient_selector=_recipients("one@my.uwi.edu", "two@uwi.edu"),
        delivery_store=delivery_store,
    )
    event = _listing_event("replay-event")

    observer(event)
    observer(event)

    assert [call["recipient"] for call in sender.calls] == [
        "one@my.uwi.edu",
        "two@uwi.edu",
    ]
    assert len(_rows(test_session)) == 2


def test_recipient_keys_use_existing_email_normalization(
    test_session, delivery_store
) -> None:
    sender = FakeSender()
    observer = EmailNotificationObserver(
        sender=sender,
        recipient_selector=_recipients("  One@MY.UWI.EDU ", "one@my.uwi.edu"),
        delivery_store=delivery_store,
    )
    event = _listing_event("normalized-recipient")

    observer(event)
    observer(event)

    assert [call["recipient"] for call in sender.calls] == ["one@my.uwi.edu"]
    assert [row.recipient for row in _rows(test_session)] == ["one@my.uwi.edu"]


def test_failed_recipient_is_not_recorded_and_does_not_block_others(
    test_session, delivery_store
) -> None:
    sender = FakeSender(failures=["bad@my.uwi.edu"])
    observer = EmailNotificationObserver(
        sender=sender,
        recipient_selector=_recipients(
            "bad@my.uwi.edu", "good@my.uwi.edu", "also-good@uwi.edu"
        ),
        delivery_store=delivery_store,
    )
    event = _listing_event("partial-failure")

    with pytest.raises(EmailDeliveryError, match=r"failed for 1 of 3"):
        observer(event)

    assert [call["recipient"] for call in sender.calls] == [
        "bad@my.uwi.edu",
        "good@my.uwi.edu",
        "also-good@uwi.edu",
    ]
    assert {row.recipient for row in _rows(test_session)} == {
        "good@my.uwi.edu",
        "also-good@uwi.edu",
    }


def test_record_failure_is_reported_and_does_not_block_other_recipients(
    test_session, delivery_store
) -> None:
    sender = FakeSender()
    store = FailingRecordStore(delivery_store, "unrecorded@my.uwi.edu")
    observer = EmailNotificationObserver(
        sender=sender,
        recipient_selector=_recipients("unrecorded@my.uwi.edu", "good@my.uwi.edu"),
        delivery_store=store,
    )
    event = _listing_event("record-failure")

    with pytest.raises(EmailDeliveryError, match=r"failed for 1 of 2"):
        observer(event)

    assert [call["recipient"] for call in sender.calls] == [
        "unrecorded@my.uwi.edu",
        "good@my.uwi.edu",
    ]
    assert {row.recipient for row in _rows(test_session)} == {"good@my.uwi.edu"}


def test_missing_sender_configuration_is_reported_without_recording(
    test_session, delivery_store
) -> None:
    observer = EmailNotificationObserver(
        sender=SmtpEmailSender(InvalidConfigService()),
        recipient_selector=_recipients("one@my.uwi.edu"),
        delivery_store=delivery_store,
    )
    event = _listing_event("missing-config")

    with pytest.raises(EmailDeliveryError, match="EmailConfigurationError"):
        observer(event)

    assert _rows(test_session) == []


def test_failed_recipient_is_retried_on_replay(test_session, delivery_store) -> None:
    sender = FakeSender(failures=["bad@my.uwi.edu"])
    observer = EmailNotificationObserver(
        sender=sender,
        recipient_selector=_recipients("bad@my.uwi.edu", "good@my.uwi.edu"),
        delivery_store=delivery_store,
    )
    event = _listing_event("retry-event")

    with pytest.raises(EmailDeliveryError):
        observer(event)
    with pytest.raises(EmailDeliveryError):
        observer(event)

    assert [call["recipient"] for call in sender.calls] == [
        "bad@my.uwi.edu",
        "good@my.uwi.edu",
        "bad@my.uwi.edu",
    ]
    assert {row.recipient for row in _rows(test_session)} == {"good@my.uwi.edu"}


def test_dispatch_result_reports_partial_failure_and_continues_dispatch(
    delivery_store,
) -> None:
    observer = EmailNotificationObserver(
        sender=FakeSender(failures=["bad@my.uwi.edu"]),
        recipient_selector=_recipients("bad@my.uwi.edu", "good@my.uwi.edu"),
        delivery_store=delivery_store,
    )
    healthy = RecordingObserver()
    publisher = NotificationPublisher()
    publisher.subscribe(observer)
    publisher.subscribe(healthy)
    event = _listing_event("dispatch-failure")

    result = publisher.notify_with_result(event)

    assert result.succeeded is False
    assert observer in result.failed_observers
    assert healthy in result.attempted_observers
    assert healthy not in result.failed_observers
    assert healthy.events == [event]


def test_website_delivery_survives_email_observer_failure(
    test_session, website_engine, delivery_store
) -> None:
    user = User(
        username="eligible@my.uwi.edu",
        email="eligible@my.uwi.edu",
        password="x",
        role="alumni",
        email_verified=1,
        display_name="Eligible Alum",
    )
    test_session.add(user)
    test_session.commit()
    test_session.refresh(user)

    email_observer = EmailNotificationObserver(
        sender=FakeSender(failures=[user.email]),
        recipient_selector=_recipients("ignored@my.uwi.edu"),
        delivery_store=delivery_store,
    )
    publisher = NotificationPublisher()
    publisher.subscribe(email_observer)
    publisher.subscribe(website_notification_observer)
    event = _campaign_event(
        "website-survives-email-failure",
        (NotificationRecipient(user.id, user.email),),
    )

    result = publisher.notify_with_result(event)

    assert email_observer in result.failed_observers
    assert website_notification_observer not in result.failed_observers
    rows = test_session.exec(
        select(Notification).where(Notification.event_id == event.event_id)
    ).all()
    assert [row.user_id for row in rows] == [user.id]


def test_database_initialization_adds_email_table_without_changing_legacy_rows(
    tmp_path, monkeypatch
) -> None:
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with legacy_engine.begin() as conn:
        conn.execute(
            sa_text(
                "CREATE TABLE notification ("
                "id INTEGER PRIMARY KEY, user_id INTEGER, event_type VARCHAR, "
                "message VARCHAR, created_at VARCHAR)"
            )
        )
        conn.execute(
            sa_text(
                "INSERT INTO notification "
                "(id, user_id, event_type, message, created_at) "
                "VALUES (7, 1, 'listing.published', 'legacy', "
                "'2026-01-01T00:00:00+00:00')"
            )
        )
    monkeypatch.setattr(database_module, "engine", legacy_engine)

    database_module.create_db_and_tables()

    inspector = sa_inspect(legacy_engine)
    assert "emaildelivery" in inspector.get_table_names()
    indexes = {index["name"]: index for index in inspector.get_indexes("emaildelivery")}
    recipient_event_index = indexes[EMAIL_DELIVERY_RECIPIENT_EVENT_INDEX]
    assert recipient_event_index["column_names"] == ["recipient", "event_id"]
    assert bool(recipient_event_index["unique"])
    with legacy_engine.connect() as conn:
        rows = conn.execute(
            sa_text(
                "SELECT id, user_id, event_type, message, created_at, event_id "
                "FROM notification"
            )
        ).all()
    assert len(rows) == 1
    assert tuple(rows[0]) == (
        7,
        1,
        "listing.published",
        "legacy",
        "2026-01-01T00:00:00+00:00",
        None,
    )


def test_observer_uses_shared_selector_for_listing_audience(
    test_session, delivery_store, website_engine
) -> None:
    qualified = _eligible_user(test_session, "qualified@my.uwi.edu")
    _eligible_user(test_session, "other-faculty@my.uwi.edu", faculty="Engineering")
    add_user(
        test_session,
        role="alumni",
        email="unverified@my.uwi.edu",
        verified=0,
    )
    listing = _listing(
        test_session,
        faculty="Science and Technology",
        department="Computer Science",
    )
    sender = FakeSender()
    observer = EmailNotificationObserver(
        sender=sender,
        recipient_selector=email_recipient_selector,
        delivery_store=delivery_store,
    )

    observer(_listing_event(f"shared-selector-{listing.id}", listing_id=listing.id))

    assert [call["recipient"] for call in sender.calls] == [qualified.email]


@pytest.mark.parametrize(
    "email_failure",
    [False, True],
    ids=["email-success", "email-failure"],
)
def test_campaign_send_success_depends_on_website_delivery(
    test_session, monkeypatch, delivery_store, website_engine, email_failure, caplog
) -> None:
    monkeypatch.setattr(campaigns_module, "engine", test_session.get_bind())
    monkeypatch.setattr("app.services.board_auth.engine", test_session.get_bind())
    admin = add_user(test_session, role="admin", email="campaign-admin@uwi.edu")
    alumnus = _eligible_user(
        test_session, "campaign-alum@my.uwi.edu", faculty=None, department=None
    )
    campaign = add_campaign(test_session, admin.id)
    sender = FakeSender(failures=[alumnus.email] if email_failure else [])
    email_observer = EmailNotificationObserver(
        sender=sender,
        recipient_selector=email_recipient_selector,
        delivery_store=delivery_store,
    )

    with isolate_publisher():
        notification_publisher.subscribe(email_observer)
        notification_publisher.subscribe(website_notification_observer)
        result = campaigns_module.send_campaign(admin.id, campaign.id)

    assert result["ok"] is True
    assert result["campaign"]["status"] == "sent"
    assert len(sender.calls) == 1
    if email_failure:
        assert "EmailNotificationObserver failed for event type campaign.sent" in caplog.text

    rows = test_session.exec(
        select(Notification).where(
            Notification.event_id == f"campaign-{campaign.id}-sent"
        )
    ).all()
    assert [row.user_id for row in rows] == [alumnus.id]

    retry = campaigns_module.send_campaign(admin.id, campaign.id)
    assert retry["ok"] is True
    assert retry["campaign"]["status"] == "sent"
    assert len(sender.calls) == 1
    retry_rows = test_session.exec(
        select(Notification).where(
            Notification.event_id == f"campaign-{campaign.id}-sent"
        )
    ).all()
    assert len(retry_rows) == 1


def test_campaign_observers_share_one_resolved_target_set(
    test_session, monkeypatch, website_engine, delivery_store
) -> None:
    monkeypatch.setattr(campaigns_module, "engine", test_session.get_bind())
    monkeypatch.setattr("app.services.board_auth.engine", test_session.get_bind())
    admin = add_user(test_session, role="admin", email="shared-admin@uwi.edu")
    eligible = _eligible_user(test_session, "shared@my.uwi.edu", faculty="Science")
    _eligible_user(test_session, "other@my.uwi.edu", faculty="Other")
    campaign = add_campaign(test_session, admin.id)
    campaign.audience_type = "faculty"
    campaign.audience_value = "science"
    test_session.add(campaign)
    test_session.commit()
    sender = FakeSender()
    selected = []
    original_select = campaigns_module.select_recipients

    def select_once(*args, **kwargs):
        users = original_select(*args, **kwargs)
        selected.append([user["id"] for user in users])
        return users

    def unexpected_selector(*args, **kwargs):
        raise AssertionError("an observer independently selected campaign recipients")

    monkeypatch.setattr(campaigns_module, "select_recipients", select_once)
    monkeypatch.setattr(website_module, "select_recipients", unexpected_selector)
    email_observer = EmailNotificationObserver(
        sender=sender, recipient_selector=unexpected_selector, delivery_store=delivery_store
    )
    captured = RecordingObserver()
    with isolate_publisher():
        notification_publisher.subscribe(website_notification_observer)
        notification_publisher.subscribe(email_observer)
        notification_publisher.subscribe(captured)
        result = campaigns_module.send_campaign(admin.id, campaign.id)

    rows = test_session.exec(
        select(Notification).where(Notification.event_id == f"campaign-{campaign.id}-sent")
    ).all()
    assert result["ok"] is True
    assert selected == [[eligible.id]]
    assert [row.user_id for row in rows] == [eligible.id]
    assert [call["recipient"] for call in sender.calls] == [eligible.email]
    assert [recipient.user_id for recipient in captured.events[0].recipients] == [eligible.id]
    assert isinstance(captured.events[0].recipients, tuple)


def test_campaign_send_succeeds_when_email_observer_is_not_subscribed(
    test_session, monkeypatch, website_engine
) -> None:
    monkeypatch.setattr(campaigns_module, "engine", test_session.get_bind())
    monkeypatch.setattr("app.services.board_auth.engine", test_session.get_bind())
    admin = add_user(test_session, role="admin", email="campaign-admin-no-email@uwi.edu")
    alumnus = _eligible_user(
        test_session, "campaign-alum-no-email@my.uwi.edu", faculty=None, department=None
    )
    campaign = add_campaign(test_session, admin.id)

    with isolate_publisher():
        notification_publisher.subscribe(website_notification_observer)
        result = campaigns_module.send_campaign(admin.id, campaign.id)

    assert result["ok"] is True
    assert result["campaign"]["status"] == "sent"
    rows = test_session.exec(
        select(Notification).where(
            Notification.event_id == f"campaign-{campaign.id}-sent"
        )
    ).all()
    assert [row.user_id for row in rows] == [alumnus.id]


def test_disabled_notification_email_does_not_register_or_contact_smtp(
    monkeypatch,
) -> None:
    factory = MagicMock(
        side_effect=AssertionError("SMTP service must not be constructed when disabled")
    )
    monkeypatch.setattr(email_notifications_module, "SMTPEmailService", factory)

    with isolate_publisher():
        monkeypatch.setattr(
            email_notifications_module, "email_notification_observer", None
        )
        assert (
            register_email_observer(_settings(notification_email_enabled=False))
            is False
        )
        assert email_notifications_module.email_notification_observer is None
        assert notification_publisher._observers == []

    factory.assert_not_called()


def test_enabled_registration_validates_without_sending_and_is_idempotent(
    monkeypatch,
) -> None:
    service = ValidatingService()
    factory = MagicMock(return_value=service)
    monkeypatch.setattr(email_notifications_module, "SMTPEmailService", factory)

    with isolate_publisher():
        monkeypatch.setattr(
            email_notifications_module, "email_notification_observer", None
        )
        assert register_email_observer(_settings()) is True
        observer = email_notifications_module.email_notification_observer
        assert observer is not None
        assert notification_publisher.is_subscribed(observer)

        assert register_email_observer(_settings()) is True
        assert email_notifications_module.email_notification_observer is observer
        assert notification_publisher._observers == [observer]

    assert service.validation_calls == 2
    assert factory.call_count == 2


def test_enabled_registration_rejects_invalid_config_without_subscribing(
    monkeypatch,
) -> None:
    factory = MagicMock(return_value=InvalidConfigService())
    monkeypatch.setattr(email_notifications_module, "SMTPEmailService", factory)

    with isolate_publisher():
        monkeypatch.setattr(
            email_notifications_module, "email_notification_observer", None
        )
        with pytest.raises(
            EmailConfigurationError,
            match="Notification email configuration is invalid",
        ):
            register_email_observer(_settings())
        assert email_notifications_module.email_notification_observer is None
        assert notification_publisher._observers == []


def test_main_lifespan_registers_email_observer(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        main_module, "register_website_observer", lambda: calls.append("website")
    )
    monkeypatch.setattr(
        main_module, "register_email_observer", lambda: calls.append("email")
    )
    monkeypatch.setattr("app.seed.seed", lambda reset=False: calls.append("seed"))

    async def run_lifespan() -> None:
        async with main_module.lifespan(object()):
            pass

    asyncio.run(run_lifespan())

    assert calls == ["website", "email", "seed"]


def _patch_cli_processor(monkeypatch) -> MagicMock:
    @contextmanager
    def acquired():
        yield True

    monkeypatch.setattr(cli_module, "_campaign_processor_lock", acquired)
    monkeypatch.setattr("app.database.ensure_db_and_tables", lambda: None)
    monkeypatch.setattr(
        "app.services.website_notifications.register_website_observer", lambda: None
    )
    process = MagicMock(return_value={"processed": 0, "retained": 0})
    monkeypatch.setattr("app.services.campaigns.process_due_campaigns", process)
    return process


def test_cli_process_campaigns_registers_email_observer(monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    process = _patch_cli_processor(monkeypatch)
    register = MagicMock(return_value=True)
    monkeypatch.setattr(
        "app.services.email_notifications.register_email_observer", register
    )

    cli_module.cmd_process_campaigns(Namespace())

    register.assert_called_once_with(settings)
    process.assert_called_once_with()


def test_cli_process_campaigns_reports_invalid_email_config(
    monkeypatch, capsys
) -> None:
    settings = _settings()
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    process = _patch_cli_processor(monkeypatch)
    monkeypatch.setattr(
        "app.services.email_notifications.register_email_observer",
        MagicMock(side_effect=EmailConfigurationError("bad SMTP settings")),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_module.cmd_process_campaigns(Namespace())

    assert exc_info.value.code == 1
    assert "Notification email configuration error: bad SMTP settings" in (
        capsys.readouterr().err
    )
    process.assert_not_called()
