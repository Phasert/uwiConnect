from urllib.parse import urlencode

from fastapi import Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.dependencies.auth import AdminDep
from app.services import boards, campaigns
from app.services.board_auth import get_user
from app.utilities.flash import flash

from . import router, templates


def _author_names(listings):
    names = {}
    for listing in listings:
        author_id = listing["author_id"]
        if author_id not in names:
            author = get_user(author_id)
            names[author_id] = (
                (author.get("display_name") or author.get("email"))
                if author
                else f"Author #{author_id}"
            )
    return names


PENDING_PREVIEW_LIMIT = 5


def _pending_preview():
    return boards.list_pending(sort="created_desc", limit=PENDING_PREVIEW_LIMIT)


LISTING_SORT_OPTIONS = {"created_desc", "created_asc", "title_asc", "title_desc"}
REVISION_SORT_OPTIONS = {"requested_desc", "requested_asc", "title_asc", "title_desc"}
CAMPAIGN_SORT_OPTIONS = {"created_desc", "created_asc", "subject_asc", "subject_desc"}
LISTING_KIND_OPTIONS = {"", "job", "event"}
CAMPAIGN_STATUS_OPTIONS = {"", "draft", "scheduled", "sending", "sent"}


def _normalize_q(value: str | None) -> str:
    return (value or "").strip()


def _normalize_choice(value: str | None, allowed: set[str], default: str = "") -> str:
    return value if value in allowed else default


def _listing_filter_state(request: Request, sort_options: set[str], default_sort: str):
    query = request.query_params
    return {
        "q": _normalize_q(query.get("q")),
        "kind": _normalize_choice(query.get("kind"), LISTING_KIND_OPTIONS),
        "sort": _normalize_choice(query.get("sort"), sort_options, default_sort),
    }


def _campaign_filter_state(request: Request):
    query = request.query_params
    return {
        "q": _normalize_q(query.get("q")),
        "status": _normalize_choice(query.get("status"), CAMPAIGN_STATUS_OPTIONS),
        "sort": _normalize_choice(query.get("sort"), CAMPAIGN_SORT_OPTIONS, "created_desc"),
    }


def _filter_query(filters: dict[str, str]) -> str:
    params = [(key, value) for key, value in filters.items() if value]
    return f"?{urlencode(params)}" if params else ""


def _render(request, user, template_name, context=None):
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={"user": user, **(context or {})},
    )


def _redirect(request, route_name, filters: dict[str, str] | None = None):
    url = request.url_for(route_name)
    if filters:
        query = _filter_query(filters)
        if query:
            url = f"{url}{query}"
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/admin", response_class=HTMLResponse)
async def admin_home_view(request: Request, user: AdminDep):
    pending_preview = _pending_preview()
    campaign_summary = campaigns.campaign_summary()

    return _render(
        request,
        user,
        "admin.html",
        {
            "pending_count": boards.count_pending(),
            "deletion_count": boards.count_deletion_requests(user.id),
            "revision_count": boards.count_revision_requests(user.id),
            "published_count": boards.count_published(),
            "campaign_summary": campaign_summary,
            "pending_preview": pending_preview,
            "pending_preview_authors": _author_names(pending_preview),
        },
    )


@router.get("/admin/moderation", response_class=HTMLResponse)
async def admin_moderation_view(request: Request, user: AdminDep):
    filters = _listing_filter_state(request, LISTING_SORT_OPTIONS, "created_desc")
    pending = boards.list_pending(**filters)

    return _render(
        request,
        user,
        "admin/moderation.html",
        {
            "pending_listings": pending,
            "authors": _author_names(pending),
            "filters": filters,
            "result_count": len(pending),
            "filter_query": _filter_query(filters),
        },
    )


@router.get("/admin/revisions", response_class=HTMLResponse)
async def admin_revisions_view(request: Request, user: AdminDep):
    filters = _listing_filter_state(request, REVISION_SORT_OPTIONS, "requested_desc")
    result = boards.list_revision_requests(user.id, **filters)
    revision_requests = result["revision_requests"] if result.get("ok") else []

    return _render(
        request,
        user,
        "admin/revisions.html",
        {
            "revision_requests": revision_requests,
            "authors": _author_names([item["original"] for item in revision_requests]),
            "filters": filters,
            "result_count": len(revision_requests),
            "filter_query": _filter_query(filters),
        },
    )


@router.get("/admin/deletions", response_class=HTMLResponse)
async def admin_deletions_view(request: Request, user: AdminDep):
    filters = _listing_filter_state(request, LISTING_SORT_OPTIONS, "created_desc")
    result = boards.list_deletion_requests(user.id, **filters)
    deletion_requests = result["listings"] if result.get("ok") else []

    return _render(
        request,
        user,
        "admin/deletions.html",
        {
            "deletion_requests": deletion_requests,
            "authors": _author_names(deletion_requests),
            "filters": filters,
            "result_count": len(deletion_requests),
            "filter_query": _filter_query(filters),
        },
    )


@router.get("/admin/campaigns", response_class=HTMLResponse)
async def admin_campaigns_view(request: Request, user: AdminDep):
    filters = _campaign_filter_state(request)
    rows = campaigns.list_campaigns(**filters)
    return _render(
        request,
        user,
        "admin/campaigns.html",
        {
            "campaigns": rows,
            "audience_options": campaigns.audience_options(),
            "filters": filters,
            "result_count": len(rows),
            "filter_query": _filter_query(filters),
        },
    )


@router.post("/admin/listings/{listing_id}/publish")
async def admin_publish_listing(request: Request, listing_id: int, user: AdminDep):
    filters = _listing_filter_state(request, LISTING_SORT_OPTIONS, "created_desc")
    result = boards.publish_listing(user.id, listing_id)
    if result.get("ok"):
        flash(request, f"Listing #{listing_id} approved and published.", "success")
    else:
        flash(
            request,
            result.get("error", "The listing could not be approved."),
            "danger",
        )
    return _redirect(request, "admin_moderation_view", filters)


@router.post("/admin/listings/{listing_id}/reject")
async def admin_reject_listing(
    request: Request,
    listing_id: int,
    user: AdminDep,
    rejection_reason: str | None = Form(default=None),
):
    filters = _listing_filter_state(request, LISTING_SORT_OPTIONS, "created_desc")
    result = boards.reject_listing(
        user.id, listing_id, rejection_reason=rejection_reason
    )
    if result.get("ok"):
        flash(request, f"Listing #{listing_id} rejected.", "success")
    else:
        flash(
            request,
            result.get("error", "The listing could not be rejected."),
            "danger",
        )
    return _redirect(request, "admin_moderation_view", filters)


@router.post("/admin/listings/{listing_id}/deletion-request/approve")
async def admin_approve_deletion(request: Request, listing_id: int, user: AdminDep):
    filters = _listing_filter_state(request, LISTING_SORT_OPTIONS, "created_desc")
    result = boards.approve_deletion(user.id, listing_id)
    if result.get("ok"):
        flash(request, f"Listing #{listing_id} permanently deleted.", "success")
    else:
        flash(
            request,
            result.get("error", "The deletion could not be approved."),
            "danger",
        )
    return _redirect(request, "admin_deletions_view", filters)


@router.post("/admin/listings/{listing_id}/deletion-request/deny")
async def admin_deny_deletion(request: Request, listing_id: int, user: AdminDep):
    filters = _listing_filter_state(request, LISTING_SORT_OPTIONS, "created_desc")
    result = boards.deny_deletion(user.id, listing_id)
    if result.get("ok"):
        flash(request, f"Deletion request for listing #{listing_id} denied.", "success")
    else:
        flash(
            request,
            result.get("error", "The request could not be denied."),
            "danger",
        )
    return _redirect(request, "admin_deletions_view", filters)


@router.post("/admin/listings/{listing_id}/revision-request/approve")
async def admin_approve_revision(request: Request, listing_id: int, user: AdminDep):
    filters = _listing_filter_state(request, REVISION_SORT_OPTIONS, "requested_desc")
    result = boards.approve_revision(user.id, listing_id)
    if result.get("ok"):
        flash(request, f"Listing #{listing_id} revision approved.", "success")
    else:
        flash(
            request,
            result.get("error", "The revision could not be approved."),
            "danger",
        )
    return _redirect(request, "admin_revisions_view", filters)


@router.post("/admin/listings/{listing_id}/revision-request/deny")
async def admin_deny_revision(request: Request, listing_id: int, user: AdminDep):
    filters = _listing_filter_state(request, REVISION_SORT_OPTIONS, "requested_desc")
    result = boards.deny_revision(user.id, listing_id)
    if result.get("ok"):
        flash(request, f"Revision request for listing #{listing_id} denied.", "success")
    else:
        flash(
            request,
            result.get("error", "The revision request could not be denied."),
            "danger",
        )
    return _redirect(request, "admin_revisions_view", filters)


@router.post("/admin/campaigns")
async def admin_create_campaign(
    request: Request,
    user: AdminDep,
    subject: str = Form(default=""),
    body: str = Form(default=""),
    audience_type: str = Form(default="all"),
    faculty: str = Form(default=""),
    department: str = Form(default=""),
):
    filters = _campaign_filter_state(request)
    if not subject.strip() or not body.strip():
        flash(request, "Campaign title and message are required.", "danger")
        return _redirect(request, "admin_campaigns_view", filters)

    if (
        (audience_type == "all" and (faculty.strip() or department.strip()))
        or (audience_type == "faculty" and department.strip())
        or (audience_type == "department" and faculty.strip())
    ):
        flash(request, "Choose only the value for the selected audience.", "danger")
        return _redirect(request, "admin_campaigns_view", filters)

    audience_value = {"faculty": faculty, "department": department}.get(audience_type)
    result = campaigns.create_draft(
        user.id,
        subject=subject,
        body=body,
        audience_type=audience_type,
        audience_value=audience_value,
    )
    if result.get("ok"):
        flash(request, "Campaign draft created.", "success")
    else:
        flash(
            request,
            result.get("error", "The campaign could not be created."),
            "danger",
        )
    return _redirect(request, "admin_campaigns_view", filters)


@router.post("/admin/campaigns/{campaign_id}/send")
async def admin_send_campaign(request: Request, campaign_id: int, user: AdminDep):
    filters = _campaign_filter_state(request)
    result = campaigns.send_campaign(user.id, campaign_id)
    if result.get("ok"):
        flash(
            request,
            "Campaign notification processing completed. Eligible alumni receive website notifications; enabled email delivery is best-effort and does not confirm inbox receipt.",
            "success",
        )
    else:
        flash(
            request,
            result.get("error", "The campaign could not be sent."),
            "danger",
        )
    return _redirect(request, "admin_campaigns_view", filters)


@router.post("/admin/campaigns/{campaign_id}/schedule")
async def admin_schedule_campaign(
    request: Request,
    campaign_id: int,
    user: AdminDep,
    scheduled_at: str = Form(default=""),
):
    filters = _campaign_filter_state(request)
    result = campaigns.schedule_campaign(
        user.id, campaign_id, scheduled_at=scheduled_at
    )
    if result.get("ok"):
        scheduled = result["campaign"]["scheduled_at"]
        flash(request, f"Campaign scheduled for {scheduled} (UTC).", "success")
    else:
        flash(
            request,
            result.get("error", "The campaign could not be scheduled."),
            "danger",
        )
    return _redirect(request, "admin_campaigns_view", filters)
