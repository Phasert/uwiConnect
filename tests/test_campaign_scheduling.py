import pytest
from sqlalchemy import create_engine, text
from sqlalchemy import inspect as sa_inspect
from sqlmodel import select

import app.database as database_module
from app.models.notification import Notification
from app.services import campaigns
from app.services.website_notifications import register_website_observer
from tests.campaign_test_helpers import add_campaign, add_user, isolate_publisher


def test_schedule_draft_campaign_uses_utc(test_session, monkeypatch) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    monkeypatch.setattr("app.services.board_auth.engine", test_session.get_bind())
    admin = add_user(test_session, role="admin")
    campaign = add_campaign(test_session, admin.id)

    result = campaigns.schedule_campaign(
        admin.id,
        campaign.id,
        scheduled_at="2027-01-02T05:30:00-04:00",
    )

    assert result["ok"] is True
    assert result["campaign"]["status"] == "scheduled"
    assert result["campaign"]["scheduled_at"] == "2027-01-02T09:30:00+00:00"


@pytest.mark.parametrize(
    ("scheduled_at", "error"),
    [
        ("not-a-timestamp", "valid ISO-8601"),
        ("2027-01-02T09:30:00", "UTC offset"),
        ("2020-01-01T00:00:00Z", "past"),
    ],
)
def test_schedule_campaign_rejects_invalid_or_past_times(
    test_session, monkeypatch, scheduled_at, error
) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    monkeypatch.setattr("app.services.board_auth.engine", test_session.get_bind())
    admin = add_user(test_session, role="admin")
    campaign = add_campaign(test_session, admin.id)

    result = campaigns.schedule_campaign(
        admin.id,
        campaign.id,
        scheduled_at=scheduled_at,
    )

    assert result["ok"] is False
    assert error in result["error"]


def test_schedule_campaign_rejects_sent_campaign(test_session, monkeypatch) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    monkeypatch.setattr("app.services.board_auth.engine", test_session.get_bind())
    admin = add_user(test_session, role="admin")
    campaign = add_campaign(test_session, admin.id)
    campaign.status = "sent"
    campaign.sent_at = "2026-01-01T00:00:00+00:00"
    test_session.add(campaign)
    test_session.commit()

    result = campaigns.schedule_campaign(
        admin.id,
        campaign.id,
        scheduled_at="2027-01-02T09:30:00Z",
    )

    assert result == {"ok": False, "error": "campaign already sent"}


def test_processor_does_not_process_future_campaign(test_session, monkeypatch) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    campaign = add_campaign(test_session, 1)
    campaign.status = "scheduled"
    campaign.scheduled_at = "2030-01-01T00:00:00+00:00"
    test_session.add(campaign)
    test_session.commit()

    result = campaigns.process_due_campaigns(now="2027-01-01T00:00:00Z")

    test_session.refresh(campaign)
    assert result == {"processed": 0, "retained": 0}
    assert campaign.status == "scheduled"


def test_atomic_claim_only_allows_one_processor(test_session, monkeypatch) -> None:
    monkeypatch.setattr(campaigns, "engine", test_session.get_bind())
    campaign = add_campaign(test_session, 1)
    campaign.status = "scheduled"
    campaign.scheduled_at = "2027-01-01T00:00:00+00:00"
    test_session.add(campaign)
    test_session.commit()

    first = campaigns._claim_due_campaign(
        campaign.id,
        "2027-01-01T00:00:00+00:00",
    )
    second = campaigns._claim_due_campaign(
        campaign.id,
        "2027-01-01T00:00:00+00:00",
    )

    test_session.refresh(campaign)
    assert first is True
    assert second is False
    assert campaign.status == "sending"


def test_process_due_campaign_publishes_and_repeats_without_duplicate_notifications(
    test_session, monkeypatch
) -> None:
    bound_engine = test_session.get_bind()
    monkeypatch.setattr(campaigns, "engine", bound_engine)
    monkeypatch.setattr(
        "app.services.website_notifications.engine",
        bound_engine,
    )

    admin = add_user(test_session, role="admin", email="campaign-admin@uwi.edu")
    verified = add_user(
        test_session,
        role="alumni",
        verified=1,
        email="scheduled@my.uwi.edu",
    )
    unverified = add_user(
        test_session,
        role="alumni",
        verified=0,
        email="pending@gmail.com",
    )
    campaign = add_campaign(test_session, admin.id)
    campaign.status = "scheduled"
    campaign.scheduled_at = "2020-01-01T00:00:00+00:00"
    test_session.add(campaign)
    test_session.commit()

    with isolate_publisher():
        register_website_observer()

        first = campaigns.process_due_campaigns(
            now="2027-01-02T00:00:00Z",
        )
        second = campaigns.process_due_campaigns(
            now="2027-01-02T00:00:00Z",
        )

    test_session.refresh(campaign)
    rows = test_session.exec(
        select(Notification).where(
            Notification.event_id == f"campaign-{campaign.id}-sent"
        )
    ).all()
    assert first == {"processed": 1, "retained": 0}
    assert second == {"processed": 0, "retained": 0}
    assert campaign.status == "sent"
    assert campaign.sent_at is not None
    assert [row.user_id for row in rows] == [verified.id]
    assert unverified.id not in {row.user_id for row in rows}
    assert admin.id not in {row.user_id for row in rows}


def test_campaign_database_schema_upgrade(tmp_path, monkeypatch) -> None:
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE campaign ("
                "id INTEGER PRIMARY KEY, admin_id INTEGER, subject VARCHAR, "
                "body VARCHAR, status VARCHAR, created_at VARCHAR, sent_at VARCHAR)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO campaign "
                "(id, admin_id, subject, body, status, created_at, sent_at) "
                "VALUES (1, 2, 'Legacy', 'Body', 'draft', 'now', NULL)"
            )
        )
    monkeypatch.setattr(database_module, "engine", legacy_engine)

    database_module._ensure_campaign_scheduled_at_column()
    database_module._ensure_campaign_scheduled_at_column()

    columns = {
        column["name"] for column in sa_inspect(legacy_engine).get_columns("campaign")
    }
    with legacy_engine.connect() as connection:
        row = connection.execute(
            text("SELECT id, status, scheduled_at FROM campaign WHERE id = 1")
        ).one()

    assert "scheduled_at" in columns
    assert tuple(row) == (1, "draft", None)
