"""Email campaign compose / send - notification fan-out is the Observer seam."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

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


def create_draft(admin_id: int, *, subject: str, body: str) -> dict[str, Any]:
    user = get_user(admin_id)
    if not user or user["role"] != "admin":
        return {"ok": False, "error": "admin only"}
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


def send_campaign(admin_id: int, campaign_id: int) -> dict[str, Any]:
    user = get_user(admin_id)
    if not user or user["role"] != "admin":
        return {"ok": False, "error": "admin only"}
    with Session(engine) as session:
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            return {"ok": False, "error": "campaign not found"}
        campaign.status = "sent"
        campaign.sent_at = _now()
        session.add(campaign)
        session.commit()
        session.refresh(campaign)
        row = campaign.model_dump()

    event_message = f"Campaign: {row['subject']} - {row['body'][:120]}"
    notification_publisher.notify(
        NotificationEvent(
            event_type=CAMPAIGN_SENT,
            campaign_id=campaign_id,
            message=event_message,
        )
    )

    return {"ok": True, "campaign": row}
