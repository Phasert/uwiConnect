from typing import Annotated

from fastapi import BackgroundTasks, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.dependencies import SessionDep
from app.dependencies.csrf import rotate_csrf_token
from app.repositories.user import UserRepository
from app.services.accounts import resend_verification_email
from app.services.auth_service import AuthService
from app.services.verification_resend import RESEND_MESSAGE, check_resend_rate_limit
from app.utilities.flash import flash
from app.utilities.security import access_token_cookie_kwargs

from . import router, templates


@router.get("/login", response_class=HTMLResponse)
async def login_view(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
    )


@router.post("/login", response_class=HTMLResponse)
async def login_action_ajax(
    db: SessionDep,
    request: Request,
    username: str = Form(),
    password: str = Form(),
):
    user_repo = UserRepository(db)
    auth_service = AuthService(user_repo)
    access_token = auth_service.authenticate_user(username, password)
    if not access_token:
        flash(request, "Incorrect username or password", "danger")
        return RedirectResponse(
            url=request.url_for("login_view"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    user = user_repo.get_by_username(username)
    dest = "admin_home_view" if user and user.role == "admin" else "user_home_view"
    response = RedirectResponse(
        url=request.url_for(dest),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    rotate_csrf_token(request)
    response.set_cookie(
        key="access_token",
        value=access_token,
        **access_token_cookie_kwargs(),
    )
    return response


@router.post("/resend-verification")
async def resend_verification_action(
    request: Request,
    background_tasks: BackgroundTasks,
    email: Annotated[str, Form(max_length=320)],
):
    check_resend_rate_limit(request)
    background_tasks.add_task(resend_verification_email, email)
    flash(request, RESEND_MESSAGE)
    return RedirectResponse(
        url=request.url_for("login_view"),
        status_code=status.HTTP_303_SEE_OTHER,
    )
