import re


def csrf_token_from_html(html: str) -> str:
    match = re.search(
        r'<input type="hidden" name="csrf_token" value="([^"]+)"',
        html,
    )
    assert match is not None, "CSRF token input not found"
    return match.group(1)
