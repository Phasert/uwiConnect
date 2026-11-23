"""Job/event board submit and publish."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_
from sqlmodel import Session, select

from app.database import engine
from app.models.listing import Listing
from app.services.accounts import require_verified_alumni
from app.services.board_auth import get_user
from app.services.observer import (
    LISTING_PUBLISHED,
    NotificationEvent,
    notification_publisher,
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def browse_published(
    *, kind: str | None = None, q: str | None = None
) -> list[dict[str, Any]]:
    with Session(engine) as session:
        stmt = select(Listing).where(Listing.status == "published")
        if kind:
            stmt = stmt.where(Listing.kind == kind)
        if q:
            pattern = f"%{q}%"
            stmt = stmt.where(
                or_(Listing.title.ilike(pattern), Listing.body.ilike(pattern))
            )
        stmt = stmt.order_by(Listing.id.desc())
        return [r.model_dump() for r in session.exec(stmt).all()]


def submit_listing(
    author_id: int, *, kind: str, title: str, body: str
) -> dict[str, Any]:
    user = get_user(author_id)
    err = require_verified_alumni(user)
    if user and user["role"] == "admin":
        err = None
    if err:
        return {"ok": False, "error": err}
    if kind not in {"job", "event"}:
        return {"ok": False, "error": "kind must be job|event"}
    if not title.strip() or not body.strip():
        return {"ok": False, "error": "title and body are required"}

    status = "pending"
    with Session(engine) as session:
        listing = Listing(
            author_id=author_id,
            kind=kind,
            title=title,
            body=body,
            status=status,
            created_at=_now(),
        )
        session.add(listing)
        session.commit()
        session.refresh(listing)
        row = listing.model_dump()

    return {"ok": True, "listing": row}


def list_by_author(author_id: int) -> list[dict[str, Any]]:
    with Session(engine) as session:
        stmt = (
            select(Listing)
            .where(Listing.author_id == author_id)
            .order_by(Listing.id.desc())
        )
        return [r.model_dump() for r in session.exec(stmt).all()]


def list_pending() -> list[dict[str, Any]]:
    with Session(engine) as session:
        rows = session.exec(
            select(Listing).where(Listing.status == "pending").order_by(Listing.id)
        ).all()
        return [r.model_dump() for r in rows]


def publish_listing(admin_id: int, listing_id: int) -> dict[str, Any]:
    user = get_user(admin_id)
    if not user or user["role"] != "admin":
        return {"ok": False, "error": "admin only"}

    with Session(engine) as session:
        listing = session.get(Listing, listing_id)
        if listing is None:
            return {"ok": False, "error": "listing not found"}
        if listing.status != "pending":
            return {"ok": False, "error": "listing is not pending"}
        listing.status = "published"
        session.add(listing)
        session.commit()
        session.refresh(listing)
        row = listing.model_dump()

    notification_publisher.notify(
        NotificationEvent(
            event_type=LISTING_PUBLISHED,
            listing_id=listing_id,
            kind=row["kind"],
            message=f"New {row['kind']} on the board: {row['title']}",
        )
    )
    return {"ok": True, "listing": row, "notified": True}


def reject_listing(admin_id: int, listing_id: int) -> dict[str, Any]:
    user = get_user(admin_id)
    if not user or user["role"] != "admin":
        return {"ok": False, "error": "admin only"}
    with Session(engine) as session:
        listing = session.get(Listing, listing_id)
        if listing is None:
            return {"ok": False, "error": "listing not found"}
        if listing.status != "pending":
            return {"ok": False, "error": "listing is not pending"}
        listing.status = "rejected"
        session.add(listing)
        session.commit()
        session.refresh(listing)
        row = listing.model_dump()
    return {"ok": True, "listing": row}
