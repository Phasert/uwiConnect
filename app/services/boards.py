"""Job/event board submit and publish."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_
from sqlmodel import Session, select

from app.database import engine
from app.models.listing import Listing, ListingRevision
from app.services.accounts import require_verified_alumni
from app.services.board_auth import get_user
from app.services.observer import (
    LISTING_PUBLISHED,
    NotificationEvent,
    notification_publisher,
)

TRINIDAD_TIMEZONE = timezone(timedelta(hours=-4))


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _local_today() -> date:
    return datetime.now(TRINIDAD_TIMEZONE).date()


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


def _prepare_listing_update(
    listing: Listing, updates: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    data = listing.model_dump()
    data.update(updates)
    if data.get("kind") == "job":
        for field in ("event_date", "event_time", "registration_information"):
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
    return data, _validate_listing_data(data)


def browse_published(
    *,
    kind: str | None = None,
    q: str | None = None,
    category: str | None = None,
    faculty: str | None = None,
    department: str | None = None,
) -> list[dict[str, Any]]:
    with Session(engine) as session:
        today = _local_today().isoformat()
        stmt = select(Listing).where(
            Listing.status == "published",
            or_(
                and_(
                    Listing.kind == "job",
                    or_(
                        Listing.application_deadline.is_(None),
                        Listing.application_deadline >= today,
                    ),
                ),
                and_(
                    Listing.kind == "event",
                    or_(Listing.event_date.is_(None), Listing.event_date >= today),
                ),
            ),
        )
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
        return [row.model_dump() for row in session.exec(stmt).all()]


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
        data, validation_error = _prepare_listing_update(listing, updates)
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


def request_revision(
    author_id: int, listing_id: int, updates: dict[str, Any]
) -> dict[str, Any]:
    with Session(engine) as session:
        listing = session.get(Listing, listing_id)
        if listing is None:
            return {"ok": False, "error": "listing not found"}
        if listing.author_id != author_id:
            return {"ok": False, "error": "listing owner only"}
        if listing.status != "published":
            return {"ok": False, "error": "only published listings can be revised"}
        if listing.deletion_requested:
            return {"ok": False, "error": "listing has a pending deletion request"}
        if session.get(ListingRevision, listing_id):
            return {"ok": False, "error": "revision already requested"}
        data, validation_error = _prepare_listing_update(listing, updates)
        if validation_error:
            return {"ok": False, "error": validation_error}

        revision = ListingRevision(
            listing_id=listing_id,
            author_id=author_id,
            status="pending",
            requested_at=_now(),
            kind=data["kind"],
            title=data["title"],
            body=data["body"],
            **{field: data.get(field) for field in _DETAIL_FIELDS},
        )
        session.add(revision)
        session.commit()
        session.refresh(revision)
        return {"ok": True, "revision": revision.model_dump()}


def request_deletion(author_id: int, listing_id: int) -> dict[str, Any]:
    with Session(engine) as session:
        listing = session.get(Listing, listing_id)
        if listing is None:
            return {"ok": False, "error": "listing not found"}
        if listing.author_id != author_id:
            return {"ok": False, "error": "listing owner only"}
        if listing.deletion_requested:
            return {"ok": False, "error": "deletion already requested"}
        if session.get(ListingRevision, listing_id):
            return {"ok": False, "error": "listing has a pending revision request"}

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
        rows = session.exec(stmt).all()
        revision_ids = set(
            session.exec(
                select(ListingRevision.listing_id).where(
                    ListingRevision.author_id == author_id,
                    ListingRevision.status == "pending",
                )
            ).all()
        )
        return [
            {**row.model_dump(), "revision_requested": row.id in revision_ids}
            for row in rows
        ]


def _listing_order(sort: str):
    if sort == "created_asc":
        return (Listing.created_at.asc(), Listing.id.asc())
    if sort == "title_asc":
        return (Listing.title.asc(), Listing.id.asc())
    if sort == "title_desc":
        return (Listing.title.desc(), Listing.id.desc())
    return (Listing.created_at.desc(), Listing.id.desc())


def _apply_listing_filters(stmt, *, q: str | None = None, kind: str | None = None):
    if kind in {"job", "event"}:
        stmt = stmt.where(Listing.kind == kind)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(Listing.title.ilike(pattern), Listing.body.ilike(pattern))
        )
    return stmt


def count_pending() -> int:
    with Session(engine) as session:
        return int(
            session.exec(
                select(func.count()).select_from(Listing).where(Listing.status == "pending")
            ).one()
        )


def count_published() -> int:
    with Session(engine) as session:
        today = _local_today().isoformat()
        stmt = (
            select(func.count())
            .select_from(Listing)
            .where(
                Listing.status == "published",
                or_(
                    and_(
                        Listing.kind == "job",
                        or_(
                            Listing.application_deadline.is_(None),
                            Listing.application_deadline >= today,
                        ),
                    ),
                    and_(
                        Listing.kind == "event",
                        or_(Listing.event_date.is_(None), Listing.event_date >= today),
                    ),
                ),
            )
        )
        return int(session.exec(stmt).one())


def list_pending(
    *,
    q: str | None = None,
    kind: str | None = None,
    sort: str = "created_desc",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    with Session(engine) as session:
        stmt = select(Listing).where(Listing.status == "pending")
        stmt = _apply_listing_filters(stmt, q=q, kind=kind)
        stmt = stmt.order_by(*_listing_order(sort))
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = session.exec(stmt).all()
        return [r.model_dump() for r in rows]


def count_deletion_requests(admin_id: int) -> int:
    user = get_user(admin_id)
    if not user or user["role"] != "admin":
        return 0
    with Session(engine) as session:
        return int(
            session.exec(
                select(func.count())
                .select_from(Listing)
                .where(Listing.deletion_requested.is_(True))
            ).one()
        )


def list_deletion_requests(
    admin_id: int,
    *,
    q: str | None = None,
    kind: str | None = None,
    sort: str = "created_desc",
) -> dict[str, Any]:
    user = get_user(admin_id)
    if not user or user["role"] != "admin":
        return {"ok": False, "error": "admin only"}
    with Session(engine) as session:
        stmt = select(Listing).where(Listing.deletion_requested.is_(True))
        stmt = _apply_listing_filters(stmt, q=q, kind=kind)
        stmt = stmt.order_by(*_listing_order(sort))
        rows = session.exec(stmt).all()
        return {"ok": True, "listings": [row.model_dump() for row in rows]}


def _revision_order(sort: str):
    if sort == "requested_asc":
        return (ListingRevision.requested_at.asc(), ListingRevision.listing_id.asc())
    if sort == "title_asc":
        return (ListingRevision.title.asc(), ListingRevision.listing_id.asc())
    if sort == "title_desc":
        return (ListingRevision.title.desc(), ListingRevision.listing_id.desc())
    return (ListingRevision.requested_at.desc(), ListingRevision.listing_id.desc())


def count_revision_requests(admin_id: int) -> int:
    user = get_user(admin_id)
    if not user or user["role"] != "admin":
        return 0
    with Session(engine) as session:
        return int(
            session.exec(
                select(func.count())
                .select_from(ListingRevision)
                .where(ListingRevision.status == "pending")
            ).one()
        )


def list_revision_requests(
    admin_id: int,
    *,
    q: str | None = None,
    kind: str | None = None,
    sort: str = "requested_desc",
) -> dict[str, Any]:
    user = get_user(admin_id)
    if not user or user["role"] != "admin":
        return {"ok": False, "error": "admin only"}
    with Session(engine) as session:
        stmt = select(ListingRevision).where(ListingRevision.status == "pending")
        if kind in {"job", "event"}:
            stmt = stmt.where(ListingRevision.kind == kind)
        if q:
            pattern = f"%{q}%"
            stmt = stmt.where(
                or_(ListingRevision.title.ilike(pattern), ListingRevision.body.ilike(pattern))
            )
        stmt = stmt.order_by(*_revision_order(sort))
        revisions = session.exec(stmt).all()
        requests = []
        for revision in revisions:
            listing = session.get(Listing, revision.listing_id)
            if listing:
                proposed = revision.model_dump()
                requests.append(
                    {
                        "listing_id": revision.listing_id,
                        "requesting_author_id": revision.author_id,
                        "status": revision.status,
                        "original": listing.model_dump(),
                        "proposed": {
                            field: proposed[field]
                            for field in ("kind", "title", "body", *_DETAIL_FIELDS)
                        },
                        "requested_at": revision.requested_at,
                    }
                )
        return {"ok": True, "revision_requests": requests}


def approve_revision(admin_id: int, listing_id: int) -> dict[str, Any]:
    user = get_user(admin_id)
    if not user or user["role"] != "admin":
        return {"ok": False, "error": "admin only"}
    with Session(engine) as session:
        listing = session.get(Listing, listing_id)
        revision = session.get(ListingRevision, listing_id)
        if listing is None:
            return {"ok": False, "error": "listing not found"}
        if revision is None or revision.status != "pending":
            return {"ok": False, "error": "revision is not awaiting review"}
        if listing.status != "published" or listing.deletion_requested:
            return {"ok": False, "error": "listing is not eligible for revision"}
        data = revision.model_dump()
        validation_error = _validate_listing_data(data)
        if validation_error:
            return {"ok": False, "error": validation_error}

        listing.kind = data["kind"]
        listing.title = data["title"]
        listing.body = data["body"]
        for field in _DETAIL_FIELDS:
            setattr(listing, field, data.get(field))
        session.add(listing)
        session.delete(revision)
        session.commit()
        session.refresh(listing)
        return {"ok": True, "listing": listing.model_dump()}


def deny_revision(admin_id: int, listing_id: int) -> dict[str, Any]:
    user = get_user(admin_id)
    if not user or user["role"] != "admin":
        return {"ok": False, "error": "admin only"}
    with Session(engine) as session:
        listing = session.get(Listing, listing_id)
        revision = session.get(ListingRevision, listing_id)
        if listing is None:
            return {"ok": False, "error": "listing not found"}
        if revision is None or revision.status != "pending":
            return {"ok": False, "error": "revision is not awaiting review"}
        session.delete(revision)
        session.commit()
        return {"ok": True, "listing_id": listing_id, "status": "denied"}


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
        revision = session.get(ListingRevision, listing_id)
        if revision:
            session.delete(revision)
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
