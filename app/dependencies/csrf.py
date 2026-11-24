import secrets

from fastapi import HTTPException, Request, status

_CSRF_SESSION_KEY = "_csrf_token"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def get_csrf_token(request: Request) -> str:
    token = request.session.get(_CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = generate_csrf_token()
        request.session[_CSRF_SESSION_KEY] = token
    return token


def rotate_csrf_token(request: Request) -> str:
    token = generate_csrf_token()
    request.session[_CSRF_SESSION_KEY] = token
    return token


async def get_csrf_protection(request: Request) -> None:
    if request.method.upper() in _SAFE_METHODS:
        return

    expected_token = request.session.get(_CSRF_SESSION_KEY)
    provided_token = request.headers.get("X-CSRF-Token")

    if provided_token is None:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type in {
            "application/x-www-form-urlencoded",
            "multipart/form-data",
        }:
            form = await request.form()
            form_token = form.get("csrf_token")
            if isinstance(form_token, str):
                provided_token = form_token

    if (
        not isinstance(expected_token, str)
        or not expected_token.isascii()
        or not isinstance(provided_token, str)
        or not provided_token.isascii()
        or not secrets.compare_digest(expected_token, provided_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token missing or invalid",
        )
