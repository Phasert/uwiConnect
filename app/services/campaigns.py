"""Email campaign compose / send - notification fan-out is the Observer seam."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import update
from sqlmodel import Session, select

from app.database import engine
from app.models.campaign import Campaign
from app.services.board_auth import get_user
from app.services.observer import (
    CAMPAIGN_SENT,
    NotificationEvent,
    notification_publisher,
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_scheduled_at(
    value: str, *, now: str | None = None
) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, "scheduled_at is required"

    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, "scheduled_at must be a valid ISO-8601 timestamp"

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, "scheduled_at must include a UTC offset or Z"

    scheduled = parsed.astimezone(timezone.utc).replace(microsecond=0)
    current = (
        datetime.fromisoformat(now) if now is not None else datetime.now(timezone.utc)
    )
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc).replace(microsecond=0)

    if scheduled < current:
        return None, "scheduled_at cannot be in the past"

    return scheduled.isoformat(), None


def _canonical_utc_timestamp(value: str) -> str | None:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _campaign_event(campaign: Campaign) -> NotificationEvent:
    event_message = f"Campaign: {campaign.subject} - {campaign.body[:120]}"
    return NotificationEvent(
        event_type=CAMPAIGN_SENT,
        campaign_id=campaign.id,
        message=event_message,
        event_id=f"campaign-{campaign.id}-sent",
    )


def _notification_publish_succeeded(event: NotificationEvent) -> bool:
    from app.services.website_notifications import website_notification_observer

    publish_result = notification_publisher.notify(event)
    return (
        website_notification_observer in publish_result.attempted_observers
        and website_notification_observer in publish_result.succeeded_observers
        and website_notification_observer not in publish_result.failed_observers
    )


def _require_admin(admin_id: int) -> dict[str, Any] | None:
    user = get_user(admin_id)
    if not user or user["role"] != "admin":
        return {"ok": False, "error": "admin only"}
    return None


def _load_campaign(campaign_id: int) -> Campaign | None:
    with Session(engine) as session:
        return session.get(Campaign, campaign_id)


def _claim_immediate_send(campaign_id: int) -> tuple[str, Campaign | None]:
    with engine.begin() as connection:
        result = connection.execute(
            update(Campaign)
            .where(
                Campaign.id == campaign_id,
                Campaign.status == "draft",
            )
            .values(status="sending")
        )
        if result.rowcount == 1:
            return "claimed", None

    current = _load_campaign(campaign_id)
    if current is None:
        return "missing", None
    if current.status == "sent":
        return "sent", current
    return "state_changed", current


def create_draft(admin_id: int, *, subject: str, body: str) -> dict[str, Any]:
    admin_error = _require_admin(admin_id)
    if admin_error:
        return admin_error
    with Session(engine) as session:
        campaign = Campaign(
            admin_id=admin_id,
            subject=subject,
            body=body,
            status="draft",
            created_at=_now(),
            sent_at=None,
        )
        session.add(campaign)
        session.commit()
        session.refresh(campaign)
        return {"ok": True, "campaign": campaign.model_dump()}


def list_campaigns() -> list[dict[str, Any]]:
    with Session(engine) as session:
        rows = session.exec(select(Campaign).order_by(Campaign.id.desc())).all()
        return [r.model_dump() for r in rows]


def schedule_campaign(
    admin_id: int,
    campaign_id: int,
    *,
    scheduled_at: str,
) -> dict[str, Any]:
    admin_error = _require_admin(admin_id)
    if admin_error:
        return admin_error

    canonical, error = _parse_scheduled_at(scheduled_at)
    if error:
        return {"ok": False, "error": error}

    with Session(engine) as session:
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            return {"ok": False, "error": "campaign not found"}
        if campaign.status == "sent":
            return {"ok": False, "error": "campaign already sent"}
        if campaign.status != "draft":
            return {"ok": False, "error": "only draft campaigns can be scheduled"}

        campaign.status = "scheduled"
        campaign.scheduled_at = canonical
        session.add(campaign)
        session.commit()
        session.refresh(campaign)
        return {"ok": True, "campaign": campaign.model_dump()}


def send_campaign(admin_id: int, campaign_id: int) -> dict[str, Any]:
    admin_error = _require_admin(admin_id)
    if admin_error:
        return admin_error

    campaign = _load_campaign(campaign_id)
    if campaign is None:
        return {"ok": False, "error": "campaign not found"}
    if campaign.status == "sent":
        return {"ok": True, "campaign": campaign.model_dump()}
    if campaign.status in {"scheduled", "sending"}:
        return {
            "ok": False,
            "error": f"campaign is already {campaign.status}",
        }
    if campaign.status != "draft":
        return {"ok": False, "error": "only draft campaigns can be sent"}

    claim_result, current = _claim_immediate_send(campaign_id)
    if claim_result == "missing":
        return {"ok": False, "error": "campaign not found"}
    if claim_result == "sent":
        return {"ok": True, "campaign": current.model_dump()}
    if claim_result == "state_changed":
        return {
            "ok": False,
            "error": f"campaign is already {current.status}",
        }

    campaign = _load_campaign(campaign_id)
    if campaign is None:
        return {"ok": False, "error": "campaign not found"}
    observers_succeeded = _notification_publish_succeeded(_campaign_event(campaign))
    if not observers_succeeded:
        return {
            "ok": False,
            "error": "campaign notification delivery failed",
            "campaign": campaign.model_dump(),
        }

    _mark_campaign_sent(campaign_id)
    sent_campaign = _load_campaign(campaign_id)
    if sent_campaign is None:
        return {"ok": False, "error": "campaign not found"}
    return {"ok": True, "campaign": sent_campaign.model_dump()}


def _claim_due_campaign(campaign_id: int, now: str) -> bool:
    with engine.begin() as connection:
        result = connection.execute(
            update(Campaign)
            .where(
                Campaign.id == campaign_id,
                Campaign.status == "scheduled",
                Campaign.scheduled_at <= now,
            )
            .values(status="sending")
        )
        return result.rowcount == 1


def _mark_campaign_sent(campaign_id: int) -> None:
    sent_at = _now()
    with engine.begin() as connection:
        connection.execute(
            update(Campaign)
            .where(
                Campaign.id == campaign_id,
                Campaign.status == "sending",
            )
            .values(status="sent", sent_at=sent_at)
        )


def _publish_scheduled_campaign(campaign_id: int) -> bool:
    with Session(engine) as session:
        campaign = session.get(Campaign, campaign_id)
        if campaign is None or campaign.status not in {"sending", "scheduled"}:
            return False

    event = _campaign_event(campaign)
    observers_succeeded = _notification_publish_succeeded(event)

    # Website notification failures leave the campaign in sending for recovery.
    # Email observer failures are logged by the publisher but are best-effort.
    if not observers_succeeded:
        return False

    _mark_campaign_sent(campaign_id)
    return True


def _process_time(now: str | None) -> str | None:
    current = now or _now()
    if now is not None:
        canonical = _canonical_utc_timestamp(now)
        if canonical is None:
            return None
        current = canonical
    return current


def _due_campaigns(current: str) -> list[Campaign]:
    with Session(engine) as session:
        return session.exec(
            select(Campaign).where(
                Campaign.status == "scheduled",
                Campaign.scheduled_at <= current,
            )
        ).all()


def _sending_campaigns() -> list[Campaign]:
    with Session(engine) as session:
        return session.exec(select(Campaign).where(Campaign.status == "sending")).all()


def _process_campaign_batch(
    campaign_batch: list[Campaign],
    *,
    claim_due: bool,
    current: str,
) -> tuple[int, int]:
    processed = 0
    retained = 0
    for campaign in campaign_batch:
        if claim_due and not _claim_due_campaign(campaign.id, current):
            continue
        if _publish_scheduled_campaign(campaign.id):
            processed += 1
        else:
            retained += 1
    return processed, retained


def process_due_campaigns(*, now: str | None = None) -> dict[str, int]:
    current = _process_time(now)
    if current is None:
        return {"processed": 0, "retained": 0}

    processed = 0
    retained = 0
    due_processed, due_retained = _process_campaign_batch(
        _due_campaigns(current),
        claim_due=True,
        current=current,
    )
    processed += due_processed
    retained += due_retained

    # Recovery: a prior crash may have published or failed before status changed.
    # Re-publishing uses the campaign's deterministic event ID, so the existing
    # notification unique index prevents duplicate notification rows.
    sending_processed, sending_retained = _process_campaign_batch(
        _sending_campaigns(),
        claim_due=False,
        current=current,
    )
    processed += sending_processed
    retained += sending_retained

    return {"processed": processed, "retained": retained}
