from contextlib import contextmanager

from sqlmodel import Session

from app.models.campaign import Campaign
from app.models.user import User
from app.services import campaigns
from app.services.observer import (
    CAMPAIGN_SENT,
    NotificationEvent,
    notification_publisher,
)


class RecordingObserver:
    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    def __call__(self, event: NotificationEvent) -> None:
        self.events.append(event)


def add_user(session, *, role: str) -> User:
    user = User(
        username=f"{role}@example.com",
        email=f"{role}@example.com",
        password="x",
        role=role,
        email_verified=1,
        display_name=role.title(),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def add_campaign(session, admin_id: int) -> Campaign:
    campaign = Campaign(
        admin_id=admin_id,
        subject="Career update",
        body="A new opportunity is available for alumni.",
        status="draft",
        created_at="2026-01-01T00:00:00+00:00",
    )
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    return campaign


@contextmanager
def isolate_publisher():
    original = list(notification_publisher._observers)
    notification_publisher._observers.clear()
    try:
        yield
    finally:
        notification_publisher._observers[:] = original


def test_send_campaign_publishes_one_typed_event_after_commit(
    test_session, monkeypatch
) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    monkeypatch.setattr("app.services.board_auth.engine", test_session.get_bind())
    admin = add_user(test_session, role="admin")
    campaign = add_campaign(test_session, admin.id)
    observer = RecordingObserver()

    with isolate_publisher():
        notification_publisher.subscribe(observer)
        persisted_at_publish = {}

        def verify_commit(event: NotificationEvent) -> None:
            observer(event)
            with Session(campaigns.engine) as session:
                persisted = session.get(Campaign, event.campaign_id)
                persisted_at_publish["status"] = persisted.status
                persisted_at_publish["sent_at"] = persisted.sent_at

        notification_publisher.unsubscribe(observer)
        notification_publisher.subscribe(verify_commit)

        result = campaigns.send_campaign(admin.id, campaign.id)

    assert result["ok"] is True
    assert len(observer.events) == 1
    assert persisted_at_publish == {
        "status": "sent",
        "sent_at": result["campaign"]["sent_at"],
    }

    with Session(campaigns.engine) as session:
        persisted = session.get(Campaign, campaign.id)
        assert persisted.status == "sent"
        assert persisted.sent_at == result["campaign"]["sent_at"]


def test_send_campaign_event_has_expected_payload(test_session, monkeypatch) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    monkeypatch.setattr("app.services.board_auth.engine", test_session.get_bind())
    admin = add_user(test_session, role="admin")
    campaign = add_campaign(test_session, admin.id)
    campaign.body = ("A" * 125) + "-THIS-TEXT-MUST-NOT-BE-INCLUDED"
    test_session.add(campaign)
    test_session.commit()
    test_session.refresh(campaign)
    observer = RecordingObserver()

    with isolate_publisher():
        notification_publisher.subscribe(observer)
        campaigns.send_campaign(admin.id, campaign.id)

    assert len(observer.events) == 1
    event = observer.events[0]
    assert event.event_type == CAMPAIGN_SENT
    assert event.campaign_id == campaign.id
    assert len(campaign.body) > 120
    assert event.message == (
        f"Campaign: {campaign.subject} - {campaign.body[:120]}"
    )
    assert "-THIS-TEXT-MUST-NOT-BE-INCLUDED" not in event.message


def test_non_admin_send_does_not_publish_event(test_session, monkeypatch) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    monkeypatch.setattr("app.services.board_auth.engine", test_session.get_bind())
    alumni = add_user(test_session, role="alumni")
    observer = RecordingObserver()

    with isolate_publisher():
        notification_publisher.subscribe(observer)
        result = campaigns.send_campaign(alumni.id, 999)

    assert result == {"ok": False, "error": "admin only"}
    assert observer.events == []


def test_nonexistent_campaign_does_not_publish_event(test_session, monkeypatch) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    monkeypatch.setattr("app.services.board_auth.engine", test_session.get_bind())
    admin = add_user(test_session, role="admin")
    observer = RecordingObserver()

    with isolate_publisher():
        notification_publisher.subscribe(observer)
        result = campaigns.send_campaign(admin.id, 999)

    assert result == {"ok": False, "error": "campaign not found"}
    assert observer.events == []
