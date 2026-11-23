from sqlalchemy import Index
from sqlmodel import Field, SQLModel


class Notification(SQLModel, table=True):
    __table_args__ = (
        Index("uq_notification_user_event", "user_id", "event_id", unique=True),
    )
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    event_type: str
    message: str
    created_at: str
    event_id: str | None = Field(default=None, index=True, exclude=True)
