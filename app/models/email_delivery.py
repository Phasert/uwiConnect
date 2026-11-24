from sqlalchemy import Index
from sqlmodel import Field, SQLModel

EMAIL_DELIVERY_RECIPIENT_EVENT_INDEX = "uq_email_delivery_recipient_event"


class EmailDelivery(SQLModel, table=True):
    __table_args__ = (
        Index(
            EMAIL_DELIVERY_RECIPIENT_EVENT_INDEX,
            "recipient",
            "event_id",
            unique=True,
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    recipient: str = Field(index=True)
    event_id: str = Field(index=True)
    event_type: str
    sent_at: str
