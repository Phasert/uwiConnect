"""Persist published events as in-app notifications.

Audience selection is delegated to the shared recipient selector so website
and email delivery apply the same eligibility and qualification rules.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.database import _NOTIFICATION_EVENT_INDEX as USER_EVENT_INDEX
from app.database import engine
from app.models.notification import Notification
from app.services.observer import NotificationEvent, notification_publisher
from app.services.recipient_selection import select_recipients

_PG_UNIQUE_VIOLATION = "23505"
_SQLITE_DUPLICATE_MESSAGE = (
    "unique constraint failed: notification.user_id, notification.event_id"
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _is_duplicate_notification(exc: IntegrityError) -> bool:
    """True only for a ``(user_id, event_id)`` unique-index conflict."""
    original = exc.orig
    if original is None:
        return False

    # PostgreSQL: require both the unique-violation SQLSTATE and our index.
    sqlstate = getattr(original, "pgcode", None) or getattr(original, "sqlstate", None)
    if sqlstate == _PG_UNIQUE_VIOLATION:
        diag = getattr(original, "diag", None)
        return getattr(diag, "constraint_name", None) == USER_EVENT_INDEX

    # SQLite: keep the check specific to the two duplicated columns.
    return _SQLITE_DUPLICATE_MESSAGE in str(original).lower()


class WebsiteNotificationObserver:
    def __call__(self, event: NotificationEvent) -> None:
        with Session(engine) as session:
            recipients = select_recipients(event, session)

        created_at = _now()
        for user in recipients:
            # One transaction per recipient keeps a duplicate conflict from
            # rolling back delivery to everyone else in the audience.
            try:
                with Session(engine) as write_session:
                    write_session.add(
                        Notification(
                            user_id=user["id"],
                            event_type=event.event_type,
                            message=event.message or event.event_type,
                            created_at=created_at,
                            event_id=event.event_id,
                        )
                    )
                    write_session.commit()
            except IntegrityError as exc:
                if not _is_duplicate_notification(exc):
                    raise


website_notification_observer = WebsiteNotificationObserver()


def register_website_observer() -> None:
    notification_publisher.subscribe(website_notification_observer)
