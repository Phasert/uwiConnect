from fastapi import Request
from fastapi.responses import HTMLResponse

from app.dependencies.auth import IsUserLoggedIn, get_current_user, is_admin
from app.dependencies.session import SessionDep
from app.services import boards
from app.services.alumni_eligibility import is_eligible_alumni

from . import router, templates

FEATURED_LIMIT = 3


@router.get("/", response_class=HTMLResponse, name="index_view")
async def index_view(
    request: Request,
    user_logged_in: IsUserLoggedIn,
    db: SessionDep,
):
    app_home = request.url_for("login_view")
    admin_link = False
    featured_access = False
    if user_logged_in:
        user = await get_current_user(request, db)
        admin_link = await is_admin(user)
        featured_access = is_eligible_alumni(user)
        if admin_link:
            app_home = request.url_for("admin_home_view")
        else:
            app_home = request.url_for("user_home_view")

    featured_jobs = []
    featured_events = []
    if featured_access:
        featured_jobs = boards.browse_published(kind="job")[:FEATURED_LIMIT]
        featured_events = boards.browse_published(kind="event")[:FEATURED_LIMIT]

    return templates.TemplateResponse(
        request=request,
        name="landing.html",
        context={
            "user_logged_in": user_logged_in,
            "app_home": app_home,
            "is_admin": admin_link,
            "featured_access": featured_access,
            "featured_jobs": featured_jobs,
            "featured_events": featured_events,
        },
    )
