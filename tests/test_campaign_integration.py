from sqlmodel import select

import app.cli as cli_module
from app.models.notification import Notification
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


def test_send_campaign_persists_notification_for_verified_alumni(
    test_session, monkeypatch
) -> None:
    bound_engine = test_session.get_bind()
    monkeypatch.setattr(campaigns, "engine", bound_engine)
    monkeypatch.setattr("app.services.board_auth.engine", bound_engine)
    monkeypatch.setattr(
        "app.services.website_notifications.engine",
        bound_engine,
    )

    admin = add_user(test_session, role="admin", email="campaign-admin@uwi.edu")
    verified_alumnus = add_user(
        test_session,
        role="alumni",
        verified=1,
        email="verified@my.uwi.edu",
    )
    unverified_alumnus = add_user(
        test_session,
        role="alumni",
        verified=0,
        email="unverified@gmail.com",
    )
    campaign = add_campaign(test_session, admin.id)
    captured = RecordingObserver()

    with isolate_publisher():
        register_website_observer()
        notification_publisher.subscribe(captured)

        result = campaigns.send_campaign(admin.id, campaign.id)

        assert result["ok"] is True
        assert len(captured.events) == 1
        event = captured.events[0]
        assert event.event_type == CAMPAIGN_SENT
        assert event.campaign_id == campaign.id
        assert event.event_id
        assert event.message == (
            f"Campaign: {campaign.subject} - {campaign.body[:120]}"
        )

        rows = test_session.exec(
            select(Notification).where(Notification.event_id == event.event_id)
        ).all()
        assert [row.user_id for row in rows] == [verified_alumnus.id]
        assert len(rows) == 1
        row = rows[0]
        assert row.event_type == "campaign.sent"
        assert row.message == event.message

        assert not test_session.exec(
            select(Notification).where(Notification.user_id == unverified_alumnus.id)
        ).all()
        assert not test_session.exec(
            select(Notification).where(Notification.user_id == admin.id)
        ).all()

        notification_publisher.notify(event)

        replayed_rows = test_session.exec(
            select(Notification).where(
                Notification.user_id == verified_alumnus.id,
                Notification.event_id == event.event_id,
            )
        ).all()
        assert len(replayed_rows) == 1


def test_auxiliary_observer_failure_does_not_cancel_website_delivery(
    test_session, monkeypatch
) -> None:
    bound_engine = test_session.get_bind()
    monkeypatch.setattr(campaigns, "engine", bound_engine)
    monkeypatch.setattr("app.services.website_notifications.engine", bound_engine)

    admin = add_user(test_session, role="admin", email="aux-admin@uwi.edu")
    eligible = add_user(
        test_session,
        role="alumni",
        verified=1,
        email="aux-alum@my.uwi.edu",
    )
    campaign = add_campaign(test_session, admin.id)
    campaign.status = "sending"
    test_session.add(campaign)
    test_session.commit()

    def failing_auxiliary(event: NotificationEvent) -> None:
        raise RuntimeError("auxiliary failed")

    with isolate_publisher():
        register_website_observer()
        notification_publisher.subscribe(failing_auxiliary)
        result = campaigns.process_due_campaigns(
            now="2027-01-02T00:00:00Z",
        )

    test_session.refresh(campaign)
    rows = test_session.exec(
        select(Notification).where(
            Notification.event_id == f"campaign-{campaign.id}-sent"
        )
    ).all()
    assert result == {"processed": 1, "retained": 0}
    assert campaign.status == "sent"
    assert [row.user_id for row in rows] == [eligible.id]


def test_cli_process_campaigns_command(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "app.database.ensure_db_and_tables",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.services.campaigns.process_due_campaigns",
        lambda: {"processed": 2, "retained": 1},
    )

    cli_module.main(["process-campaigns"])

    assert "2 processed" in capsys.readouterr().out


def test_cli_process_campaigns_registers_observer_and_delivers(
    test_session, monkeypatch
) -> None:
    bound_engine = test_session.get_bind()
    monkeypatch.setattr(campaigns, "engine", bound_engine)
    monkeypatch.setattr("app.services.website_notifications.engine", bound_engine)
    monkeypatch.setattr("app.database.ensure_db_and_tables", lambda: None)

    admin = add_user(test_session, role="admin", email="cli-admin@uwi.edu")
    eligible = add_user(
        test_session,
        role="alumni",
        verified=1,
        email="cli-eligible@my.uwi.edu",
    )
    add_user(
        test_session,
        role="alumni",
        verified=0,
        email="cli-unverified@gmail.com",
    )
    campaign = add_campaign(test_session, admin.id)
    campaign.status = "scheduled"
    campaign.scheduled_at = "2020-01-01T00:00:00+00:00"
    test_session.add(campaign)
    test_session.commit()

    with isolate_publisher():
        cli_module.main(["process-campaigns"])

        rows = test_session.exec(
            select(Notification).where(
                Notification.event_id == f"campaign-{campaign.id}-sent"
            )
        ).all()

    test_session.refresh(campaign)
    assert campaign.status == "sent"
    assert [row.user_id for row in rows] == [eligible.id]
