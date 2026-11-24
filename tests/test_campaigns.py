import pytest
from sqlmodel import Session

from app.models.campaign import Campaign
from app.services import campaigns
from app.services.observer import (
    CAMPAIGN_SENT,
    NotificationEvent,
    notification_publisher,
)
from app.services.website_notifications import register_website_observer
from tests.campaign_test_helpers import (
    RecordingObserver,
    add_campaign,
    add_user,
    isolate_publisher,
)


def test_create_draft_campaign(test_session, monkeypatch) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    monkeypatch.setattr("app.services.board_auth.engine", test_session.get_bind())
    admin = add_user(test_session, role="admin")

    result = campaigns.create_draft(
        admin.id,
        subject="New opportunity",
        body="Details for verified alumni.",
    )

    assert result["ok"] is True
    assert result["campaign"]["admin_id"] == admin.id
    assert result["campaign"]["subject"] == "New opportunity"
    assert result["campaign"]["status"] == "draft"
    assert result["campaign"]["sent_at"] is None


def test_list_campaigns_returns_newest_first(test_session, monkeypatch) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    first = add_campaign(test_session, 1)
    second = add_campaign(test_session, 1)
    second.subject = "Second update"
    test_session.add(second)
    test_session.commit()

    rows = campaigns.list_campaigns()

    assert [row["id"] for row in rows] == [second.id, first.id]


def test_send_campaign_publishes_one_typed_event_after_commit(
    test_session, monkeypatch
) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    monkeypatch.setattr("app.services.board_auth.engine", test_session.get_bind())
    monkeypatch.setattr(
        "app.services.website_notifications.engine",
        test_session.get_bind(),
    )
    admin = add_user(test_session, role="admin")
    campaign = add_campaign(test_session, admin.id)
    observer = RecordingObserver()

    with isolate_publisher():
        register_website_observer()
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
    assert persisted_at_publish == {"status": "sending", "sent_at": None}

    with Session(campaigns.engine) as session:
        persisted = session.get(Campaign, campaign.id)
        assert persisted.status == "sent"
        assert persisted.sent_at == result["campaign"]["sent_at"]


def test_send_campaign_event_has_expected_payload(test_session, monkeypatch) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    monkeypatch.setattr("app.services.board_auth.engine", test_session.get_bind())
    monkeypatch.setattr(
        "app.services.website_notifications.engine",
        test_session.get_bind(),
    )
    admin = add_user(test_session, role="admin")
    campaign = add_campaign(test_session, admin.id)
    campaign.body = ("A" * 125) + "-THIS-TEXT-MUST-NOT-BE-INCLUDED"
    test_session.add(campaign)
    test_session.commit()
    test_session.refresh(campaign)
    observer = RecordingObserver()

    with isolate_publisher():
        register_website_observer()
        notification_publisher.subscribe(observer)
        campaigns.send_campaign(admin.id, campaign.id)

    assert len(observer.events) == 1
    event = observer.events[0]
    assert event.event_type == CAMPAIGN_SENT
    assert event.campaign_id == campaign.id
    assert len(campaign.body) > 120
    assert event.message == (f"Campaign: {campaign.subject} - {campaign.body[:120]}")
    assert "-THIS-TEXT-MUST-NOT-BE-INCLUDED" not in event.message


@pytest.mark.parametrize("body_length", [119, 120, 121])
def test_send_campaign_truncates_message_at_120_characters(
    test_session, monkeypatch, body_length
) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    monkeypatch.setattr("app.services.board_auth.engine", test_session.get_bind())
    monkeypatch.setattr(
        "app.services.website_notifications.engine",
        test_session.get_bind(),
    )
    admin = add_user(test_session, role="admin")
    campaign = add_campaign(test_session, admin.id)
    body = "A" * body_length
    campaign.body = body
    test_session.add(campaign)
    test_session.commit()
    test_session.refresh(campaign)
    observer = RecordingObserver()

    with isolate_publisher():
        register_website_observer()
        notification_publisher.subscribe(observer)
        result = campaigns.send_campaign(admin.id, campaign.id)

    assert result["ok"] is True
    assert len(observer.events) == 1
    event = observer.events[0]
    assert event.event_type == CAMPAIGN_SENT
    assert event.campaign_id == campaign.id
    assert event.message == f"Campaign: {campaign.subject} - {body[:120]}"


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


@pytest.mark.parametrize("status", ["scheduled", "sending"])
def test_immediate_send_cannot_bypass_scheduled_or_sending_states(
    test_session, monkeypatch, status
) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    monkeypatch.setattr("app.services.board_auth.engine", test_session.get_bind())
    admin = add_user(test_session, role="admin")
    campaign = add_campaign(test_session, admin.id)
    campaign.status = status
    test_session.add(campaign)
    test_session.commit()

    result = campaigns.send_campaign(admin.id, campaign.id)

    test_session.refresh(campaign)
    assert result["ok"] is False
    assert result["error"] == f"campaign is already {status}"
    assert campaign.status == status
