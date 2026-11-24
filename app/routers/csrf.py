from fastapi import APIRouter, Request, Response

from app.dependencies.csrf import get_csrf_token

router = APIRouter(tags=["authentication"])


@router.get(
    "/auth/csrf",
    description="Return a same-origin CSRF token without exposing the access token.",
)
async def get_csrf_token_endpoint(
    request: Request, response: Response
) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    return {"csrf_token": get_csrf_token(request)}
