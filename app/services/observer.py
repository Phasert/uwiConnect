from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import uuid4

logger = logging.getLogger(__name__)

LISTING_PUBLISHED = "listing.published"
CAMPAIGN_SENT = "campaign.sent"

NotificationEventType = Literal["listing.published", "campaign.sent"]


@dataclass(frozen=True)
class NotificationEvent:
    event_type: NotificationEventType
    listing_id: int | None = None
    campaign_id: int | None = None
    message: str = ""
    event_id: str = field(default_factory=lambda: str(uuid4()))
    kind: str | None = None

    def __post_init__(self) -> None:
        if self.event_type == LISTING_PUBLISHED:
            if self.listing_id is None or self.campaign_id is not None:
                raise ValueError(
                    "listing.published requires listing_id and no campaign_id"
                )
        elif self.event_type == CAMPAIGN_SENT:
            if self.campaign_id is None or self.listing_id is not None:
                raise ValueError("campaign.sent requires campaign_id and no listing_id")
        else:
            raise ValueError(f"unsupported event type: {self.event_type!r}")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "message": self.message,
        }
        if self.listing_id is not None:
            payload["listing_id"] = self.listing_id
        if self.campaign_id is not None:
            payload["campaign_id"] = self.campaign_id
        if self.kind is not None:
            payload["kind"] = self.kind
        return payload


@runtime_checkable
class NotificationObserver(Protocol):
    def __call__(self, event: NotificationEvent) -> None: ...


@dataclass(frozen=True)
class NotificationPublishResult:
    succeeded: bool
    attempted_observers: tuple[NotificationObserver, ...] = ()
    failed_observers: tuple[NotificationObserver, ...] = ()


def _observer_identity(observer: NotificationObserver) -> str:
    qualname = getattr(observer, "__qualname__", None)
    if qualname is not None:
        return qualname
    return type(observer).__qualname__


class NotificationPublisher:
    def __init__(self) -> None:
        self._observers: list[NotificationObserver] = []

    def subscribe(self, observer: NotificationObserver) -> None:
        if observer not in self._observers:
            self._observers.append(observer)

    def unsubscribe(self, observer: NotificationObserver) -> None:
        if observer in self._observers:
            self._observers.remove(observer)

    def is_subscribed(self, observer: NotificationObserver) -> bool:
        return observer in self._observers

    def notify_with_result(
        self, event: NotificationEvent
    ) -> NotificationPublishResult:
        attempted_observers = tuple(self._observers)
        if not attempted_observers:
            return NotificationPublishResult(
                succeeded=False,
                attempted_observers=(),
                failed_observers=(),
            )
        all_succeeded = True
        failed_observers: list[NotificationObserver] = []
        for observer in attempted_observers:
            try:
                observer(event)
            except Exception:
                all_succeeded = False
                failed_observers.append(observer)
                logger.exception(
                    "Notification observer %s failed for event type %s (event_id=%s)",
                    _observer_identity(observer),
                    event.event_type,
                    event.event_id,
                )
        return NotificationPublishResult(
            succeeded=all_succeeded,
            attempted_observers=attempted_observers,
            failed_observers=tuple(failed_observers),
        )

    def notify(self, event: NotificationEvent) -> None:
        self.notify_with_result(event)


notification_publisher = NotificationPublisher()
