from collections.abc import Iterable
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text
from sqlmodel import select

import app.database as database_module
import app.services.email_delivery as email_delivery_module
import app.services.email_sender as email_sender_module
import app.services.website_notifications as website_module
from app.models.email_delivery import (
    EMAIL_DELIVERY_RECIPIENT_EVENT_INDEX,
    EmailDelivery,
)
from app.models.notification import Notification
from app.models.user import User
from app.services.email_delivery import EmailDeliveryStore, EmailDeliveryStoreError
from app.services.email_notifications import (
    EmailDeliveryError,
    EmailNotificationObserver,
)
from app.services.email_sender import EmailSendError, SmtpEmailSender
from app.services.observer import (
    CAMPAIGN_SENT,
    LISTING_PUBLISHED,
    NotificationEvent,
    NotificationPublisher,
)
from app.services.website_notifications import website_notification_observer


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


def _recipients(*addresses: str):
    def select_recipients(event: NotificationEvent) -> list[str]:
        return list(addresses)

    return select_recipients


def _listing_event(event_id: str = "listing-email-1") -> NotificationEvent:
    return NotificationEvent(
        LISTING_PUBLISHED,
        listing_id=7,
        message="New software engineering job",
        event_id=event_id,
    )


def _campaign_event(event_id: str = "campaign-email-1") -> NotificationEvent:
    return NotificationEvent(
        CAMPAIGN_SENT,
        campaign_id=3,
        message="Campaign announcement",
        event_id=event_id,
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


def test_campaign_event_sends_to_all_injected_recipients(
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
    test_session, delivery_store, monkeypatch
) -> None:
    monkeypatch.setattr(
        email_sender_module,
        "get_settings",
        lambda: SimpleNamespace(
            smtp_host="",
            smtp_port=587,
            smtp_username="",
            smtp_password="",
            smtp_from_address="",
            smtp_timeout=10.0,
            smtp_use_implicit_tls=False,
            smtp_use_starttls=True,
        ),
    )
    created: list[object] = []
    sender = SmtpEmailSender(transport_factory=lambda config: created.append(config))
    observer = EmailNotificationObserver(
        sender=sender,
        recipient_selector=_recipients("one@my.uwi.edu"),
        delivery_store=delivery_store,
    )
    event = _listing_event("missing-config")

    with pytest.raises(EmailDeliveryError, match="EmailConfigurationError"):
        observer(event)

    assert created == []
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
        sender=FakeSender(failures=["bad@my.uwi.edu"]),
        recipient_selector=_recipients("bad@my.uwi.edu"),
        delivery_store=delivery_store,
    )
    publisher = NotificationPublisher()
    publisher.subscribe(email_observer)
    publisher.subscribe(website_notification_observer)
    event = _campaign_event("website-survives-email-failure")

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


def test_no_module_level_production_email_observer_or_registration() -> None:
    import app.services.email_notifications as module

    assert not hasattr(module, "email_notification_observer")
    assert not hasattr(module, "register_email_observer")
