"""Job/event board submit and publish."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
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


_DETAIL_FIELDS = (
    "company_name",
    "location",
    "application_instructions",
    "application_deadline",
    "faculty",
    "department",
    "event_date",
    "event_time",
    "category",
    "registration_information",
)


def _clean_listing_data(data: dict[str, Any]) -> dict[str, Any]:
    cleaned = data.copy()
    for field in ("title", "body", *_DETAIL_FIELDS):
        value = cleaned.get(field)
        if isinstance(value, str):
            cleaned[field] = value.strip() or None
    return cleaned


def _validate_listing_data(data: dict[str, Any]) -> str | None:
    kind = data.get("kind")
    if kind not in {"job", "event"}:
        return "kind must be job|event"
    if not data.get("title") or not data.get("body"):
        return "title and body are required"

    if kind == "job":
        for field in ("company_name", "location", "application_instructions"):
            if not data.get(field):
                return f"{field} is required for job listings"
        if not data.get("faculty") and not data.get("department"):
            return "faculty or department is required for job listings"
        deadline = data.get("application_deadline")
        if deadline:
            try:
                date.fromisoformat(deadline)
            except ValueError:
                return "application_deadline must use YYYY-MM-DD"
    else:
        for field in ("event_date", "event_time", "location"):
            if not data.get(field):
                return f"{field} is required for event listings"
        try:
            date.fromisoformat(data["event_date"])
        except ValueError:
            return "event_date must use YYYY-MM-DD"
        try:
            time.fromisoformat(data["event_time"])
        except ValueError:
            return "event_time must use HH:MM or HH:MM:SS"
    return None


def browse_published(
    *,
    kind: str | None = None,
    q: str | None = None,
    category: str | None = None,
    faculty: str | None = None,
    department: str | None = None,
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
        if category:
            stmt = stmt.where(
                or_(
                    Listing.category.ilike(category),
                    Listing.faculty.ilike(category),
                    Listing.department.ilike(category),
                )
            )
        if faculty:
            stmt = stmt.where(Listing.faculty.ilike(faculty))
        if department:
            stmt = stmt.where(Listing.department.ilike(department))
        stmt = stmt.order_by(Listing.id.desc())
        return [r.model_dump() for r in session.exec(stmt).all()]


def submit_listing(
    author_id: int, *, kind: str, title: str, body: str, **details: str | None
) -> dict[str, Any]:
    user = get_user(author_id)
    err = require_verified_alumni(user)
    if user and user["role"] == "admin":
        err = None
    if err:
        return {"ok": False, "error": err}
    data = _clean_listing_data(
        {"kind": kind, "title": title, "body": body, **details}
    )
    validation_error = _validate_listing_data(data)
    if validation_error:
        return {"ok": False, "error": validation_error}

    with Session(engine) as session:
        listing = Listing(
            author_id=author_id,
            **data,
            status="pending",
            created_at=_now(),
        )
        session.add(listing)
        session.commit()
        session.refresh(listing)
        row = listing.model_dump()

    return {"ok": True, "listing": row}


def edit_listing(
    author_id: int,
    listing_id: int,
    updates: dict[str, Any],
) -> dict[str, Any]:
    with Session(engine) as session:
        listing = session.get(Listing, listing_id)
        if listing is None:
            return {"ok": False, "error": "listing not found"}
        if listing.author_id != author_id:
            return {"ok": False, "error": "listing owner only"}
        if listing.status not in {"pending", "rejected"}:
            return {"ok": False, "error": "only pending or rejected listings can be edited"}
        if listing.deletion_requested:
            return {"ok": False, "error": "listing has a pending deletion request"}
        data = listing.model_dump()
        data.update(updates)
        if data.get("kind") == "job":
            for field in ("event_date", "event_time", "category", "registration_information"):
                data[field] = None
        elif data.get("kind") == "event":
            for field in (
                "company_name",
                "application_instructions",
                "application_deadline",
                "faculty",
                "department",
            ):
                data[field] = None
        data = _clean_listing_data(data)
        validation_error = _validate_listing_data(data)
        if validation_error:
            return {"ok": False, "error": validation_error}

        listing.kind = data["kind"]
        listing.title = data["title"]
        listing.body = data["body"]
        for field in _DETAIL_FIELDS:
            setattr(listing, field, data.get(field))
        listing.status = "pending"
        listing.rejection_reason = None
        session.add(listing)
        session.commit()
        session.refresh(listing)
        return {"ok": True, "listing": listing.model_dump()}


def request_deletion(author_id: int, listing_id: int) -> dict[str, Any]:
    with Session(engine) as session:
        listing = session.get(Listing, listing_id)
        if listing is None:
            return {"ok": False, "error": "listing not found"}
        if listing.author_id != author_id:
            return {"ok": False, "error": "listing owner only"}
        if listing.deletion_requested:
            return {"ok": False, "error": "deletion already requested"}

        listing.deletion_requested = True
        session.add(listing)
        session.commit()
        session.refresh(listing)
        return {"ok": True, "listing": listing.model_dump()}


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


def list_deletion_requests(admin_id: int) -> dict[str, Any]:
    user = get_user(admin_id)
    if not user or user["role"] != "admin":
        return {"ok": False, "error": "admin only"}
    with Session(engine) as session:
        rows = session.exec(
            select(Listing)
            .where(Listing.deletion_requested.is_(True))
            .order_by(Listing.id)
        ).all()
        return {"ok": True, "listings": [row.model_dump() for row in rows]}


def approve_deletion(admin_id: int, listing_id: int) -> dict[str, Any]:
    user = get_user(admin_id)
    if not user or user["role"] != "admin":
        return {"ok": False, "error": "admin only"}
    with Session(engine) as session:
        listing = session.get(Listing, listing_id)
        if listing is None:
            return {"ok": False, "error": "listing not found"}
        if not listing.deletion_requested:
            return {"ok": False, "error": "deletion is not awaiting review"}
        session.delete(listing)
        session.commit()
    return {"ok": True, "listing_id": listing_id, "deleted": True}


def deny_deletion(admin_id: int, listing_id: int) -> dict[str, Any]:
    user = get_user(admin_id)
    if not user or user["role"] != "admin":
        return {"ok": False, "error": "admin only"}
    with Session(engine) as session:
        listing = session.get(Listing, listing_id)
        if listing is None:
            return {"ok": False, "error": "listing not found"}
        if not listing.deletion_requested:
            return {"ok": False, "error": "deletion is not awaiting review"}
        listing.deletion_requested = False
        session.add(listing)
        session.commit()
        session.refresh(listing)
        return {"ok": True, "listing": listing.model_dump()}


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
        listing.rejection_reason = None
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


def reject_listing(
    admin_id: int, listing_id: int, rejection_reason: str | None = None
) -> dict[str, Any]:
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
        reason = rejection_reason.strip() if rejection_reason else None
        listing.rejection_reason = reason or None
        session.add(listing)
        session.commit()
        session.refresh(listing)
        row = listing.model_dump()
    return {"ok": True, "listing": row}
