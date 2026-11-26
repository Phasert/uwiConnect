from argparse import Namespace

import pytest
from sqlalchemy.exc import OperationalError
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
            "notify",
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
        original_notify = notification_publisher.notify
        monkeypatch.setattr(
            notification_publisher,
            "notify",
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
            "notify",
            original_notify,
        )
        recovered = campaigns.process_due_campaigns(
            now="2027-01-02T00:00:00Z",
        )

    test_session.refresh(campaign)
    assert retained == {"processed": 0, "retained": 1}
    assert recovered == {"processed": 1, "retained": 0}
    assert campaign.status == "sent"


@pytest.mark.parametrize(
    "error",
    [
        OperationalError(
            "SELECT users",
            {"email": "sensitive@example.com"},
            Exception("sensitive@example.com"),
        ),
        RuntimeError("sensitive@example.com"),
    ],
)
def test_recipient_selection_failure_isolated_until_next_run(
    test_session, monkeypatch, caplog, error
) -> None:
    bound_engine = test_session.get_bind()
    monkeypatch.setattr(campaigns, "engine", bound_engine)
    monkeypatch.setattr("app.services.website_notifications.engine", bound_engine)
    admin = add_user(test_session, role="admin", email="recovery-admin@uwi.edu")
    eligible = add_user(test_session, role="alumni", email="recovery@my.uwi.edu")
    first = add_campaign(test_session, admin.id)
    second = add_campaign(test_session, admin.id)
    for campaign in (first, second):
        campaign.status = "scheduled"
        campaign.scheduled_at = "2020-01-01T00:00:00+00:00"
        test_session.add(campaign)
    test_session.commit()
    original_select = campaigns.select_recipients
    original_due = campaigns._due_campaigns
    attempts = []

    def select_with_failure(event, session, **kwargs):
        attempts.append(event.campaign_id)
        if event.campaign_id == first.id and attempts.count(first.id) == 1:
            raise error
        return original_select(event, session, **kwargs)

    monkeypatch.setattr(campaigns, "select_recipients", select_with_failure)
    monkeypatch.setattr(
        campaigns,
        "_due_campaigns",
        lambda now: sorted(original_due(now), key=lambda campaign: campaign.id),
    )
    with isolate_publisher():
        register_website_observer()
        result = campaigns.process_due_campaigns(now="2027-01-02T00:00:00Z")
        test_session.refresh(first)
        test_session.refresh(second)
        assert result == {"processed": 1, "retained": 1}
        assert first.status == "sending"
        assert second.status == "sent"
        assert attempts == [first.id, second.id]
        assert f"Could not resolve audience for campaign {first.id}" in caplog.text
        assert type(error).__name__ in caplog.text
        assert "sensitive@example.com" not in caplog.text

        second.status = "sending"
        second.sent_at = None
        test_session.add(second)
        test_session.commit()
        assert campaigns.process_due_campaigns(now="2027-01-02T00:00:00Z") == {
            "processed": 2,
            "retained": 0,
        }
        assert campaigns.process_due_campaigns(now="2027-01-02T00:00:00Z") == {
            "processed": 0,
            "retained": 0,
        }

    test_session.refresh(first)
    assert first.status == "sent"
    rows = test_session.exec(select(Notification)).all()
    assert {(row.user_id, row.event_id) for row in rows} == {
        (eligible.id, f"campaign-{first.id}-sent"),
        (eligible.id, f"campaign-{second.id}-sent"),
    }
    assert len(rows) == 2


def test_unexpected_publisher_error_still_propagates(monkeypatch) -> None:
    monkeypatch.setattr(campaigns, "_campaign_event", lambda campaign: object())

    def fail_publish(event):
        raise RuntimeError("publisher failure")

    monkeypatch.setattr(campaigns, "_notification_publish_succeeded", fail_publish)
    with pytest.raises(RuntimeError, match="publisher failure"):
        campaigns._publish_campaign(object())


def test_scheduled_retry_rechecks_eligibility_and_keeps_audience(
    test_session, monkeypatch
) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    monkeypatch.setattr("app.services.board_auth.engine", test_session.get_bind())
    admin = add_user(test_session, role="admin", email="audience-admin@uwi.edu")
    first = add_user(test_session, role="alumni", email="first@my.uwi.edu")
    second = add_user(test_session, role="alumni", email="second@my.uwi.edu")
    first.department = "Computing"
    second.department = "Other"
    test_session.add(first)
    test_session.add(second)
    test_session.commit()
    created = campaigns.create_draft(
        admin.id,
        subject="Department update",
        body="Message",
        audience_type="department",
        audience_value="Computing",
    )
    campaign_id = created["campaign"]["id"]
    scheduled = campaigns.schedule_campaign(
        admin.id, campaign_id, scheduled_at="2099-01-01T00:00:00Z"
    )
    assert scheduled["campaign"]["audience_type"] == "department"
    assert scheduled["campaign"]["audience_value"] == "Computing"
    assert campaigns._claim_due_campaign(campaign_id, "2099-01-01T00:00:00+00:00")
    attempts = []

    def publish(event):
        attempts.append(event)
        return len(attempts) > 1

    monkeypatch.setattr(campaigns, "_notification_publish_succeeded", publish)
    assert campaigns._publish_scheduled_campaign(campaign_id) is False
    first.account_revoked = True
    second.department = " computing "
    test_session.add(first)
    test_session.add(second)
    test_session.commit()
    assert campaigns._publish_scheduled_campaign(campaign_id) is True

    assert [r.user_id for r in attempts[0].recipients] == [first.id]
    assert [r.user_id for r in attempts[1].recipients] == [second.id]
    assert attempts[0].event_id == attempts[1].event_id
    assert campaigns._load_campaign(campaign_id).status == "sent"


def test_invalid_saved_audience_does_not_publish_or_broaden(
    test_session, monkeypatch
) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    campaign = add_campaign(test_session, 1)
    campaign.status = "sending"
    campaign.audience_type = "invalid"
    test_session.add(campaign)
    test_session.commit()
    published = []
    monkeypatch.setattr(
        campaigns, "_notification_publish_succeeded", lambda event: published.append(event)
    )

    assert campaigns._publish_scheduled_campaign(campaign.id) is False
    assert published == []
    assert campaigns._load_campaign(campaign.id).status == "sending"


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
            "notify",
            lambda event: NotificationPublishResult(
                succeeded=False,
                attempted_observers=(website_notification_observer,),
                failed_observers=(website_notification_observer,),
            ),
        )
        cli_module.main(["process-campaigns"])

        test_session.refresh(campaign)
        assert campaign.status == "sending"
