# ruff: noqa: I001
from fastapi import APIRouter, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

from app.config import get_settings
from app.dependencies.csrf import get_csrf_protection, get_csrf_token
from app.utilities.flash import get_flashed_messages


template_env = Environment(loader = FileSystemLoader("app/templates",), )
template_env.globals['get_flashed_messages'] = get_flashed_messages
template_env.globals['get_csrf_token'] = get_csrf_token
templates = Jinja2Templates(env=template_env)
static_files = StaticFiles(directory="app/static")

router = APIRouter(
    tags=["Jinja Based Endpoints"],
    include_in_schema=get_settings().env.lower() in ["dev", "development"],
    dependencies=[Depends(get_csrf_protection)],
)
api_router = APIRouter(tags=["API Endpoints"], prefix="/api")

from . import (
    index,
    login,
    register,
    admin_home,
    user_home,
    users,
    logout,
    server_config,
    board,
    csrf,
)

api_router.include_router(board.router)
api_router.include_router(csrf.router)

__all__ = [
    "admin_home",
    "api_router",
    "board",
    "csrf",
    "index",
    "login",
    "logout",
    "register",
    "router",
    "server_config",
    "static_files",
    "templates",
    "user_home",
    "users",
]
