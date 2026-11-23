from sqlmodel import Field, SQLModel


class Listing(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    author_id: int = Field(index=True)
    kind: str = Field(index=True)
    title: str
    body: str
    status: str
    rejection_reason: str | None = None
    created_at: str
