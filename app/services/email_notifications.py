from __future__ import annotations

from collections.abc import Callable, Iterable

from app.config import Settings, get_settings
from app.services.accounts import normalize_email
from app.services.email_delivery import EmailDeliveryStore, EmailDeliveryStoreError
from app.services.email_sender import (
    EmailConfigurationError,
    EmailError,
    EmailSender,
    SmtpEmailSender,
)
from app.services.email_service import SMTPEmailService
from app.services.observer import (
    CAMPAIGN_SENT,
    NotificationEvent,
    notification_publisher,
)
from app.services.recipient_selection import email_recipient_selector

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


email_notification_observer: EmailNotificationObserver | None = None


def register_email_observer(settings: Settings | None = None) -> bool:
    global email_notification_observer

    values = settings if settings is not None else get_settings()
    if not values.notification_email_enabled:
        if email_notification_observer is not None:
            notification_publisher.unsubscribe(email_notification_observer)
            email_notification_observer = None
        return False

    service = SMTPEmailService(values)
    try:
        service.validate_configuration()
    except ValueError as exc:
        raise EmailConfigurationError(
            f"Notification email configuration is invalid: {exc}"
        ) from None

    if email_notification_observer is None:
        email_notification_observer = EmailNotificationObserver(
            sender=SmtpEmailSender(service),
            recipient_selector=email_recipient_selector,
            delivery_store=EmailDeliveryStore(),
        )
    if not notification_publisher.is_subscribed(email_notification_observer):
        notification_publisher.subscribe(email_notification_observer)
    return True
