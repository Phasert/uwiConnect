
from sqlmodel import Field, SQLModel


class Campaign(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    admin_id: int = Field(index=True)
    subject: str
    body: str
    status: str
    created_at: str
    sent_at: str | None = None
    scheduled_at: str | None = None
    audience_type: str = "all"
    audience_value: str | None = None
