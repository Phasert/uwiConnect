from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, select

from app.database import _EMAIL_DELIVERY_EVENT_INDEX as EMAIL_EVENT_INDEX
from app.database import engine
from app.models.email_delivery import EmailDelivery

_SQLITE_DUPLICATE_MESSAGE = (
    "unique constraint failed: emaildelivery.recipient, emaildelivery.event_id"
)


class EmailDeliveryStoreError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _is_duplicate_email_delivery(exc: IntegrityError) -> bool:
    original = exc.orig
    if original is None:
        return False

    sqlstate = getattr(original, "pgcode", None) or getattr(original, "sqlstate", None)
    if sqlstate == "23505":
        diag = getattr(original, "diag", None)
        return getattr(diag, "constraint_name", None) == EMAIL_EVENT_INDEX

    return _SQLITE_DUPLICATE_MESSAGE in str(original).lower()


class EmailDeliveryStore:
    def was_sent(self, *, recipient: str, event_id: str) -> bool:
        try:
            with Session(engine) as session:
                row = session.exec(
                    select(EmailDelivery).where(
                        EmailDelivery.recipient == recipient,
                        EmailDelivery.event_id == event_id,
                    )
                ).first()
                return row is not None
        except SQLAlchemyError:
            raise EmailDeliveryStoreError("could not query email deliveries") from None

    def record_success(
        self,
        *,
        recipient: str,
        event_id: str,
        event_type: str,
        sent_at: str | None = None,
    ) -> None:
        values = {
            "recipient": recipient,
            "event_id": event_id,
            "event_type": event_type,
            "sent_at": sent_at or _now(),
        }
        try:
            with Session(engine) as session:
                session.add(EmailDelivery(**values))
                session.commit()
        except IntegrityError as exc:
            if not _is_duplicate_email_delivery(exc):
                raise EmailDeliveryStoreError(
                    "could not record email delivery"
                ) from None
        except SQLAlchemyError:
            raise EmailDeliveryStoreError("could not record email delivery") from None
