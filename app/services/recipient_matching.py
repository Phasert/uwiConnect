"""Qualification matching for notification recipients.

This is pure matching logic: it takes explicit recipient qualification values
and listing target values, and depends on neither the User model nor account
eligibility. Sean's #7.7 supplies the account fields/status and eligibility
rules. Wiring recipient selection into notifications remains Micah's #6.5
integration work.
"""

from __future__ import annotations


def normalize_qualification(value: str | None) -> str | None:
    """Trim and casefold a value; blanks count as missing."""
    if value is None:
        return None
    normalized = value.strip().casefold()
    return normalized or None


def qualifications_match(
    *,
    recipient_faculty: str | None,
    recipient_department: str | None,
    target_faculty: str | None = None,
    target_department: str | None = None,
) -> bool:
    """Return whether recipient qualifications satisfy a listing's targets.

    Rules agreed for task #6.5:
    - A recipient missing either qualification never matches, including for a
      listing with no targets.
    - A listing with no faculty/department target matches every recipient that
      has both qualifications.
    - When both targets are present, both must match (AND).
    - Values are trimmed and compared case-insensitively; blanks are missing.
    - Category is not a qualification target.
    """
    faculty = normalize_qualification(recipient_faculty)
    department = normalize_qualification(recipient_department)
    if faculty is None or department is None:
        return False

    wanted_faculty = normalize_qualification(target_faculty)
    wanted_department = normalize_qualification(target_department)
    if wanted_faculty is not None and faculty != wanted_faculty:
        return False
    return wanted_department is None or department == wanted_department
