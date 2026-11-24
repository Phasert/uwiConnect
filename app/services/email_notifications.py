from __future__ import annotations

from collections.abc import Callable, Iterable

from app.services.accounts import normalize_email
from app.services.email_delivery import EmailDeliveryStore, EmailDeliveryStoreError
from app.services.email_sender import EmailError, EmailSender
from app.services.observer import CAMPAIGN_SENT, NotificationEvent

RecipientSelector = Callable[[NotificationEvent], Iterable[str]]


class EmailDeliveryError(RuntimeError):
    pass


def _subject(event: NotificationEvent) -> str:
    if event.event_type == CAMPAIGN_SENT:
        return "UWI Connect campaign"
    return "New UWI Connect listing"


def _body(event: NotificationEvent) -> str:
    return event.message.strip() if event.message.strip() else event.event_type


class EmailNotificationObserver:
    def __init__(
        self,
        *,
        sender: EmailSender,
        recipient_selector: RecipientSelector,
        delivery_store: EmailDeliveryStore | None = None,
    ) -> None:
        self._sender = sender
        self._recipient_selector = recipient_selector
        self._delivery_store = delivery_store or EmailDeliveryStore()

    def __call__(self, event: NotificationEvent) -> None:
        failures: list[str] = []
        attempted = 0

        for recipient in self._unique_recipients(event):
            attempted += 1
            try:
                if self._delivery_store.was_sent(
                    recipient=recipient, event_id=event.event_id
                ):
                    continue
                self._sender.send(
                    recipient=recipient,
                    subject=_subject(event),
                    message=_body(event),
                )
                self._delivery_store.record_success(
                    recipient=recipient,
                    event_id=event.event_id,
                    event_type=event.event_type,
                )
            except (EmailError, EmailDeliveryStoreError) as exc:
                failures.append(type(exc).__name__)

        if failures:
            raise EmailDeliveryError(
                f"Email delivery failed for {len(failures)} of {attempted} "
                f"recipient(s): {', '.join(sorted(set(failures)))}"
            )

    def _unique_recipients(self, event: NotificationEvent) -> Iterable[str]:
        seen: set[str] = set()
        for value in self._recipient_selector(event):
            recipient = normalize_email(value) if isinstance(value, str) else ""
            if not recipient or recipient in seen:
                continue
            seen.add(recipient)
            yield recipient
