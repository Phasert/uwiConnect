from contextlib import contextmanager

from app.models.campaign import Campaign
from app.models.user import User
from app.services.observer import NotificationEvent, notification_publisher


class RecordingObserver:
    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    def __call__(self, event: NotificationEvent) -> None:
        self.events.append(event)


def add_user(
    session, *, role: str, verified: int = 1, email: str | None = None
) -> User:
    email = email or f"{role}@example.com"
    user = User(
        username=email,
        email=email,
        password="x",
        role=role,
        email_verified=verified,
        display_name=role.title(),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def add_campaign(session, admin_id: int) -> Campaign:
    campaign = Campaign(
        admin_id=admin_id,
        subject="Career update",
        body="A new opportunity is available for alumni.",
        status="draft",
        created_at="2026-01-01T00:00:00+00:00",
    )
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    return campaign


@contextmanager
def isolate_publisher():
    original = list(notification_publisher._observers)
    notification_publisher._observers.clear()
    try:
        yield
    finally:
        notification_publisher._observers[:] = original
