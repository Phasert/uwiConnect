from fastapi import Request
from fastapi.responses import HTMLResponse

from app.dependencies.auth import AlumniDep, AuthDep
from app.services import boards

from . import router, templates


@router.get("/app", response_class=HTMLResponse)
async def user_home_view(
    request: Request,
    user: AuthDep,
    q: str | None = None,
    kind: str | None = None,
):
    return templates.TemplateResponse(
        request=request,
        name="app.html",
        context={
            "user": user,
            "listings": boards.browse_published(kind=kind, q=q),
            "q": q or "",
            "kind": kind or "",
        },
    )


@router.get("/app/dashboard", response_class=HTMLResponse)
async def user_dashboard_view(
    request: Request,
    user: AlumniDep,
):
    return templates.TemplateResponse(
        request=request,
        name="alumni_dashboard.html",
        context={
            "user": user,
            "listings": boards.list_by_author(user.id),
        },
    )
