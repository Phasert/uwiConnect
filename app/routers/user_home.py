from fastapi import Request
from fastapi.responses import HTMLResponse

from app.dependencies.auth import AlumniDep, AuthDep
from app.dependencies.session import SessionDep
from app.services import boards

from . import router, templates


@router.get("/app", response_class=HTMLResponse)
async def user_home_view(
    request: Request,
    user: AuthDep,
    db:SessionDep
):
    return templates.TemplateResponse(
        request=request, 
        name="app.html",
        context={
            "user": user
        }
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
