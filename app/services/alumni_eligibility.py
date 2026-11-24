from collections.abc import Mapping
from typing import Any

from app.models.user import User


def _value(user: User | Mapping[str, Any], field: str, default=None):
    if isinstance(user, Mapping):
        return user.get(field, default)
    return getattr(user, field, default)


def is_account_accessible(user: User | Mapping[str, Any] | None) -> bool:
    if user is None:
        return False
    return bool(_value(user, "account_active", True)) and not bool(
        _value(user, "account_revoked", False)
    )


def is_eligible_alumni(user: User | Mapping[str, Any] | None) -> bool:
    if user is None:
        return False
    return (
        _value(user, "role") == "alumni"
        and bool(_value(user, "email_verified", 0))
        and is_account_accessible(user)
    )


def clean_qualification(
    programme: str | None,
    faculty: str | None,
    department: str | None,
) -> tuple[dict[str, str | None], str | None]:
    values = {
        "programme": programme.strip() if programme else None,
        "faculty": faculty.strip() if faculty else None,
        "department": department.strip() if department else None,
    }
    if not values["programme"]:
        return values, "programme is required"
    if not values["faculty"] and not values["department"]:
        return values, "faculty or department is required"
    return values, None
