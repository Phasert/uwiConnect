from pydantic import EmailStr
from sqlmodel import Field, SQLModel


class UserBase(SQLModel):
    username: str = Field(index=True, unique=True)
    email: EmailStr = Field(index=True, unique=True)
    password: str = Field(exclude=True, repr=False)
    role: str = ""
    email_verified: int = 0
    display_name: str = ""
    programme: str | None = None
    faculty: str | None = None
    department: str | None = None
    account_active: bool = True
    account_revoked: bool = False


class User(UserBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
