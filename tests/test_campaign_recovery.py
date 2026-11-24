from argparse import Namespace

import pytest
from sqlmodel import select

import app.cli as cli_module
from app.models.notification import Notification
from app.services import campaigns
from app.services.observer import (
    NotificationEvent,
    NotificationPublishResult,
    notification_publisher,
)
from app.services.website_notifications import (
    register_website_observer,
    website_notification_observer,
)
from tests.campaign_test_helpers import add_campaign, add_user, isolate_publisher


def test_auxiliary_success_without_website_observer_keeps_campaign_recoverable(
    test_session, monkeypatch
) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    campaign = add_campaign(test_session, 1)
    campaign.status = "sending"
    test_session.add(campaign)
    test_session.commit()

    auxiliary_calls = []

    def auxiliary(event: NotificationEvent) -> None:
        auxiliary_calls.append(event)

    with isolate_publisher():
        notification_publisher.subscribe(auxiliary)
        result = campaigns.process_due_campaigns(
            now="2027-01-02T00:00:00Z",
        )

    test_session.refresh(campaign)
    assert result == {"processed": 0, "retained": 1}
    assert campaign.status == "sending"
    assert len(auxiliary_calls) == 1


def test_website_observer_failure_does_not_become_successful_auxiliary_delivery(
    test_session, monkeypatch
) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    campaign = add_campaign(test_session, 1)
    campaign.status = "sending"
    test_session.add(campaign)
    test_session.commit()

    auxiliary_calls = []

    def auxiliary(event: NotificationEvent) -> None:
        auxiliary_calls.append(event)

    def publish_with_website_failure(
        event: NotificationEvent,
    ) -> NotificationPublishResult:
        auxiliary(event)
        return NotificationPublishResult(
            succeeded=False,
            attempted_observers=(auxiliary, website_notification_observer),
            failed_observers=(website_notification_observer,),
        )

    with isolate_publisher():
        notification_publisher.subscribe(auxiliary)
        notification_publisher.subscribe(website_notification_observer)
        monkeypatch.setattr(
            notification_publisher,
            "notify_with_result",
            publish_with_website_failure,
        )
        result = campaigns.process_due_campaigns(
            now="2027-01-02T00:00:00Z",
        )

    test_session.refresh(campaign)
    assert result == {"processed": 0, "retained": 1}
    assert campaign.status == "sending"
    assert len(auxiliary_calls) == 1


def test_website_observer_not_attempted_keeps_campaign_sending(
    test_session, monkeypatch
) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    campaign = add_campaign(test_session, 1)
    campaign.status = "sending"
    test_session.add(campaign)
    test_session.commit()

    auxiliary_calls = []

    def auxiliary(event: NotificationEvent) -> None:
        auxiliary_calls.append(event)

    with isolate_publisher():
        notification_publisher.subscribe(auxiliary)
        result = campaigns.process_due_campaigns(
            now="2027-01-02T00:00:00Z",
        )

    test_session.refresh(campaign)
    assert result == {"processed": 0, "retained": 1}
    assert campaign.status == "sending"
    assert len(auxiliary_calls) == 1


def test_campaign_processor_lock_releases_after_success(tmp_path) -> None:
    lock_path = tmp_path / "campaign.lock"

    with cli_module._campaign_processor_lock(lock_path) as first:
        assert first is True

    with cli_module._campaign_processor_lock(lock_path) as second:
        assert second is True


def test_campaign_processor_lock_releases_after_exception(tmp_path) -> None:
    lock_path = tmp_path / "campaign.lock"

    with (
        pytest.raises(RuntimeError, match="processor failed"),
        cli_module._campaign_processor_lock(lock_path) as acquired,
    ):
        assert acquired is True
        raise RuntimeError("processor failed")

    with cli_module._campaign_processor_lock(lock_path) as acquired:
        assert acquired is True


def test_second_concurrent_cli_processor_is_prevented(tmp_path, monkeypatch) -> None:
    lock_path = tmp_path / "campaign.lock"
    original_lock = cli_module._campaign_processor_lock
    monkeypatch.setattr(
        cli_module,
        "_campaign_processor_lock",
        lambda: original_lock(lock_path),
    )
    monkeypatch.setattr("app.database.ensure_db_and_tables", lambda: None)
    monkeypatch.setattr(
        "app.services.website_notifications.register_website_observer",
        lambda: None,
    )

    processed = []
    monkeypatch.setattr(
        "app.services.campaigns.process_due_campaigns",
        lambda: processed.append(True) or {"processed": 0, "retained": 0},
    )

    with original_lock(lock_path):
        cli_module.cmd_process_campaigns(Namespace())

    assert processed == []


def test_crash_retry_recovers_sending_campaign(test_session, monkeypatch) -> None:
    bound_engine = test_session.get_bind()
    monkeypatch.setattr(campaigns, "engine", bound_engine)
    monkeypatch.setattr(
        "app.services.website_notifications.engine",
        bound_engine,
    )
    admin = add_user(test_session, role="admin", email="admin@uwi.edu")
    add_user(
        test_session,
        role="alumni",
        verified=1,
        email="retry@my.uwi.edu",
    )
    campaign = add_campaign(test_session, admin.id)
    campaign.status = "sending"
    campaign.scheduled_at = "2027-01-01T00:00:00+00:00"
    test_session.add(campaign)
    test_session.commit()

    with isolate_publisher():
        register_website_observer()
        original_notify = notification_publisher.notify_with_result
        monkeypatch.setattr(
            notification_publisher,
            "notify_with_result",
            lambda event: NotificationPublishResult(
                succeeded=False,
                attempted_observers=(website_notification_observer,),
                failed_observers=(website_notification_observer,),
            ),
        )
        retained = campaigns.process_due_campaigns(
            now="2027-01-02T00:00:00Z",
        )
        test_session.refresh(campaign)
        assert campaign.status == "sending"
        monkeypatch.setattr(
            notification_publisher,
            "notify_with_result",
            original_notify,
        )
        recovered = campaigns.process_due_campaigns(
            now="2027-01-02T00:00:00Z",
        )

    test_session.refresh(campaign)
    assert retained == {"processed": 0, "retained": 1}
    assert recovered == {"processed": 1, "retained": 0}
    assert campaign.status == "sent"


def test_immediate_send_failure_leaves_sending_and_retry_is_deduplicated(
    test_session, monkeypatch
) -> None:
    bound_engine = test_session.get_bind()
    monkeypatch.setattr(campaigns, "engine", bound_engine)
    monkeypatch.setattr("app.services.board_auth.engine", bound_engine)
    monkeypatch.setattr("app.services.website_notifications.engine", bound_engine)

    admin = add_user(test_session, role="admin", email="failure-admin@uwi.edu")
    eligible = add_user(
        test_session,
        role="alumni",
        verified=1,
        email="failure-alum@my.uwi.edu",
    )
    campaign = add_campaign(test_session, admin.id)

    with isolate_publisher():
        failed = campaigns.send_campaign(admin.id, campaign.id)
        test_session.refresh(campaign)
        assert failed["ok"] is False
        assert campaign.status == "sending"

        register_website_observer()
        retried = campaigns.process_due_campaigns(
            now="2027-01-02T00:00:00Z",
        )
        repeated = campaigns.process_due_campaigns(
            now="2027-01-02T00:00:00Z",
        )

        rows = test_session.exec(
            select(Notification).where(
                Notification.event_id == f"campaign-{campaign.id}-sent"
            )
        ).all()

    test_session.refresh(campaign)
    assert retried == {"processed": 1, "retained": 0}
    assert repeated == {"processed": 0, "retained": 0}
    assert campaign.status == "sent"
    assert [row.user_id for row in rows] == [eligible.id]


def test_cli_process_campaigns_observer_failure_keeps_campaign_sending(
    test_session, monkeypatch
) -> None:
    bound_engine = test_session.get_bind()
    monkeypatch.setattr(campaigns, "engine", bound_engine)
    monkeypatch.setattr("app.services.website_notifications.engine", bound_engine)
    monkeypatch.setattr("app.database.ensure_db_and_tables", lambda: None)

    admin = add_user(test_session, role="admin", email="cli-failure@uwi.edu")
    campaign = add_campaign(test_session, admin.id)
    campaign.status = "scheduled"
    campaign.scheduled_at = "2020-01-01T00:00:00+00:00"
    test_session.add(campaign)
    test_session.commit()

    with isolate_publisher():
        monkeypatch.setattr(
            notification_publisher,
            "notify_with_result",
            lambda event: NotificationPublishResult(
                succeeded=False,
                attempted_observers=(website_notification_observer,),
                failed_observers=(website_notification_observer,),
            ),
        )
        cli_module.main(["process-campaigns"])

        test_session.refresh(campaign)
        assert campaign.status == "sending"
