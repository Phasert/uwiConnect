from sqlmodel import Field, SQLModel


class VerificationToken(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    token_hash: str = Field(index=True, unique=True, exclude=True, repr=False)
    expires_at: str
    consumed_at: str | None = None
