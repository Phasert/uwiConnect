import pytest

from app.services.recipient_matching import qualifications_match


def test_both_targets_match_after_trimming_and_case_normalization() -> None:
    assert (
        qualifications_match(
            recipient_faculty="  Science and Technology ",
            recipient_department="Computer Science",
            target_faculty="science AND technology",
            target_department="  computer science  ",
        )
        is True
    )


@pytest.mark.parametrize(
    ("target_faculty", "target_department"),
    [
        ("Engineering", "Science and Technology"),
        ("Science and Technology", "Computer Science"),
        ("Science and Technology", "Electrical Engineering"),
    ],
)
def test_mismatched_target_does_not_match_when_two_targets_are_present(
    target_faculty: str, target_department: str
) -> None:
    assert (
        qualifications_match(
            recipient_faculty="Engineering",
            recipient_department="Electrical Engineering",
            target_faculty=target_faculty,
            target_department=target_department,
        )
        is False
    )


@pytest.mark.parametrize(
    ("target_faculty", "target_department"),
    [
        ("Engineering", None),
        (None, "Electrical Engineering"),
        ("  engineering  ", ""),
    ],
)
def test_single_target_matches_the_corresponding_qualification(
    target_faculty: str | None, target_department: str | None
) -> None:
    assert (
        qualifications_match(
            recipient_faculty="Engineering",
            recipient_department="Electrical Engineering",
            target_faculty=target_faculty,
            target_department=target_department,
        )
        is True
    )


@pytest.mark.parametrize(
    ("target_faculty", "target_department"),
    [("Engineering", None), (None, "Electrical Engineering")],
)
def test_single_mismatched_target_does_not_match(
    target_faculty: str | None, target_department: str | None
) -> None:
    assert (
        qualifications_match(
            recipient_faculty="Science and Technology",
            recipient_department="Computer Science",
            target_faculty=target_faculty,
            target_department=target_department,
        )
        is False
    )


@pytest.mark.parametrize(
    ("target_faculty", "target_department"), [(None, None), ("", "  ")]
)
def test_listing_without_targets_matches_qualified_recipient(
    target_faculty: str | None, target_department: str | None
) -> None:
    assert (
        qualifications_match(
            recipient_faculty="Engineering",
            recipient_department="Electrical Engineering",
            target_faculty=target_faculty,
            target_department=target_department,
        )
        is True
    )


@pytest.mark.parametrize(
    ("recipient_faculty", "recipient_department"),
    [
        (None, "Computer Science"),
        ("Science and Technology", None),
        ("  ", "Computer Science"),
        ("Science and Technology", ""),
        (None, None),
    ],
)
@pytest.mark.parametrize(
    ("target_faculty", "target_department"),
    [
        (None, None),
        ("Science and Technology", None),
        (None, "Computer Science"),
        ("Science and Technology", "Computer Science"),
    ],
)
def test_missing_recipient_qualification_never_matches(
    recipient_faculty: str | None,
    recipient_department: str | None,
    target_faculty: str | None,
    target_department: str | None,
) -> None:
    assert (
        qualifications_match(
            recipient_faculty=recipient_faculty,
            recipient_department=recipient_department,
            target_faculty=target_faculty,
            target_department=target_department,
        )
        is False
    )
