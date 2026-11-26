"""Shared audience selection for notification delivery.

Eligibility stays with the existing account helpers. Listing observers use
this selector independently; campaign delivery resolves it once per attempt
and shares the result with both observers.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from app.database import engine
from app.models.listing import Listing
from app.models.user import User
from app.services.accounts import require_verified_alumni
from app.services.board_auth import public_user
from app.services.observer import CAMPAIGN_SENT, LISTING_PUBLISHED, NotificationEvent
from app.services.recipient_matching import (
    normalize_qualification,
    qualifications_match,
)

TARGET_AUDIENCE_TYPES = frozenset({"faculty", "department"})
AUDIENCE_TYPES = TARGET_AUDIENCE_TYPES | {"all"}


class RecipientSelectionError(RuntimeError):
    """Raised when an event's audience cannot be resolved for delivery."""


def _listing_targets(
    event: NotificationEvent, session: Session
) -> tuple[str | None, str | None] | None:
    if event.event_type != LISTING_PUBLISHED:
        return None
    listing = session.get(Listing, event.listing_id)
    if listing is None:
        raise RecipientSelectionError(
            f"listing {event.listing_id} not found for event {event.event_id}"
        )
    return listing.faculty, listing.department


def select_recipients(
    event: NotificationEvent,
    session: Session,
    *,
    audience_type: str = "all",
    audience_value: str | None = None,
) -> list[dict[str, Any]]:
    """Return the shared audience for ``event`` as public user dicts.

    Listing events require a real listing and a qualification match. Campaign
    events use account eligibility before applying their audience criteria.
    """
    if event.event_type == CAMPAIGN_SENT:
        if audience_type not in AUDIENCE_TYPES:
            raise RecipientSelectionError("invalid campaign audience type")
        if audience_type == "all" and audience_value is not None:
            raise RecipientSelectionError("all-alumni audience cannot have a value")
        if audience_type != "all" and (
            not isinstance(audience_value, str) or not audience_value.strip()
        ):
            raise RecipientSelectionError("campaign audience value is required")
    wanted = normalize_qualification(audience_value)
    targets = _listing_targets(event, session)
    users = session.exec(
        select(User).where(User.role == "alumni", User.email_verified == 1)
    ).all()

    recipients: list[dict[str, Any]] = []
    for user in users:
        public = public_user(user)
        if require_verified_alumni(public) is not None:
            continue
        if event.event_type == CAMPAIGN_SENT and audience_type != "all":
            value = public.get(audience_type)
            if not isinstance(value, str) or normalize_qualification(value) != wanted:
                continue
        if targets is not None and not qualifications_match(
            recipient_faculty=public["faculty"],
            recipient_department=public["department"],
            target_faculty=targets[0],
            target_department=targets[1],
        ):
            continue
        recipients.append(public)
    return recipients


def email_recipient_selector(event: NotificationEvent) -> list[str]:
    """Reuse point for EmailNotificationObserver's injected selector (#6.6)."""
    with Session(engine) as session:
        return [user["email"] for user in select_recipients(event, session)]
