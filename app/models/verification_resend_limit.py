from sqlmodel import Field, SQLModel


class VerificationResendLimit(SQLModel, table=True):
    recipient_hash: str = Field(primary_key=True)
    requested_at: float = Field(index=True)
