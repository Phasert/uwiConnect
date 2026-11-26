import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

from app.config import get_settings
from app.routers import api_router, router, static_files, templates
from app.services.website_notifications import register_website_observer

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.seed import seed

    register_website_observer()
    seed(reset=False)
    yield


is_production = get_settings().env.lower() in {"prod", "production"}

app = FastAPI(
    docs_url=None,
    redoc_url=None,
    middleware=[
        Middleware(
            SessionMiddleware,
            secret_key=get_settings().secret_key,
            same_site="lax",
            https_only=is_production,
        )
    ],
    lifespan=lifespan,
)

app.include_router(router)
app.include_router(api_router)
app.mount("/static", static_files, name="static")


@app.middleware("http")
async def recover_uninitialized_db(request: Request, call_next):
    """If a request hits a missing schema, create tables once and retry."""
    from app.database import recover_if_uninitialized

    try:
        return await call_next(request)
    except Exception as exc:
        if not recover_if_uninitialized(exc):
            raise
        logger.info("Retrying %s after database init", request.url.path)
        try:
            return await call_next(request)
        except Exception:
            if request.method.upper() == "GET":
                return RedirectResponse(url=str(request.url), status_code=303)
            raise


@app.get("/health")
async def health():
    return {"ok": True, "status": "ok", "scenario": "uwi-connect", "version": "0.2.0"}


@app.exception_handler(status.HTTP_401_UNAUTHORIZED)
async def unauthorized_redirect_handler(request: Request, exc: Exception):
    return templates.TemplateResponse(
        request=request,
        name="401.html",
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@app.exception_handler(status.HTTP_403_FORBIDDEN)
async def forbidden_handler(request: Request, exc: Exception):
    detail = str(getattr(exc, "detail", ""))
    if request.url.path.startswith("/api"):
        return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": detail})
    if "CSRF" in detail:
        message = "Your security token expired. Reload the page and try again."
    else:
        message = "You do not have permission to access this page."
    return templates.TemplateResponse(
        request=request,
        name="403.html",
        context={"message": message},
        status_code=status.HTTP_403_FORBIDDEN,
    )


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=get_settings().app_host,
        port=get_settings().app_port,
        reload=get_settings().env.lower() != "production",
    )
