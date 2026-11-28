import html

from fastapi import Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.dependencies.auth import AlumniDep
from app.services import boards
from app.utilities.flash import flash

from . import router, templates


def _submission_form_response(
    request: Request,
    user,
    *,
    form_kind: str = "job",
    form_data: dict[str, str] | None = None,
    form_error: str | None = None,
):
    return templates.TemplateResponse(
        request=request,
        name="alumni_submission_form.html",
        context={
            "user": user,
            "form_kind": form_kind,
            "form_data": form_data or {},
            "form_error": form_error,
        },
    )


@router.get("/app", response_class=HTMLResponse)
async def user_home_view(
    request: Request,
    user: AlumniDep,
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
    q: str = "",
    kind: str = "",
    listing_status: str = "",
):
    keyword = q.strip().casefold()
    listings = [
        listing for listing in boards.list_by_author(user.id)
        if (not kind or listing["kind"] == kind)
        and (not listing_status or listing["status"] == listing_status)
        and (not keyword or any(
            keyword in (listing.get(field) or "").casefold()
            for field in ("title", "body")
        ))
    ]
    return templates.TemplateResponse(
        request=request,
        name="alumni_dashboard.html",
        context={
            "user": user,
            "listings": listings,
            "q": q,
            "kind": kind,
            "listing_status": listing_status,
        },
    )


@router.get("/app/dashboard/submit", response_class=HTMLResponse)
async def submission_form_view(
    request: Request,
    user: AlumniDep,
):
    return _submission_form_response(request, user)


@router.post("/app/dashboard/submit", response_class=HTMLResponse)
async def submit_listing_action(
    request: Request,
    user: AlumniDep,
):
    form = await request.form()
    field_names = (
        "kind",
        "title",
        "body",
        "location",
        "category",
        "company_name",
        "application_instructions",
        "application_deadline",
        "faculty",
        "department",
        "event_date",
        "event_time",
        "registration_information",
    )
    values = {name: str(form.get(name, "") or "") for name in field_names}
    kind = values["kind"]

    if kind == "event":
        detail_names = (
            "event_date",
            "event_time",
            "location",
            "category",
            "registration_information",
        )
    else:
        detail_names = (
            "company_name",
            "location",
            "application_instructions",
            "application_deadline",
            "faculty",
            "department",
            "category",
        )
    details = {name: values[name] for name in detail_names}

    result = boards.submit_listing(
        user.id,
        kind=kind,
        title=values["title"],
        body=values["body"],
        **details,
    )
    if not result.get("ok"):
        return _submission_form_response(
            request,
            user,
            form_kind=kind,
            form_data={name: html.escape(value) for name, value in values.items()},
            form_error=result.get("error", "The listing could not be submitted."),
        )

    flash(
        request,
        "Listing submitted for review. It is pending until an admin approves it.",
        "success",
    )
    return RedirectResponse(
        url=request.url_for("user_dashboard_view"),
        status_code=status.HTTP_303_SEE_OTHER,
    )
