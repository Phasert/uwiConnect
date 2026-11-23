import importlib
import logging
import threading
import time
from contextlib import contextmanager

from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

logger = logging.getLogger(__name__)

# Deployment assumption: render.yaml runs `manage.py init --no-drop` and then a
# single-worker Uvicorn server sequentially, with production reload disabled;
# the Dockerfile CMD also starts Uvicorn without workers. Lifespan startup and
# request-time recovery therefore share one process, and this lock serializes
# their schema setup. Concurrent upgrades from multiple processes are outside
# this configuration and are not supported.
_schema_lock = threading.Lock()


def sqlalchemy_uri(uri: str) -> str:
    """Accept Render's postgres:// URLs in the sync SQLAlchemy engine."""
    if uri.startswith("postgres://"):
        return "postgresql+psycopg2://" + uri[len("postgres://") :]
    if uri.startswith("postgresql://"):
        return "postgresql+psycopg2://" + uri[len("postgresql://") :]
    return uri


engine = create_engine(
    sqlalchemy_uri(get_settings().database_uri),
    echo=False,
    pool_size=get_settings().db_pool_size,
    max_overflow=get_settings().db_additional_overflow,
    pool_timeout=get_settings().db_pool_timeout,
    pool_recycle=get_settings().db_pool_recycle,
)


_NOTIFICATION_EVENT_INDEX = "uq_notification_user_event"


def _ensure_notification_event_id_column() -> None:
    inspector = inspect(engine)
    if "notification" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("notification")}
    if "event_id" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE notification ADD COLUMN event_id VARCHAR"))
        inspector = inspect(engine)
    indexes = {index["name"] for index in inspector.get_indexes("notification")}
    if _NOTIFICATION_EVENT_INDEX not in indexes:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_notification_user_event "
                    "ON notification (user_id, event_id)"
                )
            )


def create_db_and_tables() -> None:
    importlib.import_module("app.models")

    SQLModel.metadata.create_all(engine)
    _ensure_notification_event_id_column()


def drop_all() -> None:
    SQLModel.metadata.drop_all(bind=engine)


def is_db_not_ready_error(exc: BaseException) -> bool:
    """True when Postgres is unreachable or the schema/tables are missing."""
    if isinstance(exc, OperationalError):
        return True
    if isinstance(exc, ProgrammingError):
        return True
    if isinstance(exc, DBAPIError) and getattr(exc, "connection_invalidated", False):
        return True

    text = str(exc).lower()
    markers = (
        "does not exist",
        "undefinedtable",
        "undefined table",
        "no such table",
        "relation ",
        "could not connect",
        "connection refused",
        "connection timed out",
        "server closed the connection",
        "the database system is starting up",
        "remaining connection slots",
        "too many connections",
        "ssl connection has been closed",
    )
    return any(m in text for m in markers)


def ensure_db_and_tables(
    *,
    retries: int = 8,
    delay_seconds: float = 2.0,
) -> None:
    """Create tables, retrying while the prod DB is still coming up."""
    last: BaseException | None = None
    for attempt in range(1, retries + 1):
        try:
            with _schema_lock:
                create_db_and_tables()
            if attempt > 1:
                logger.info("Database schema ready after %s attempt(s)", attempt)
            return
        except Exception as exc:
            last = exc
            if not is_db_not_ready_error(exc) or attempt >= retries:
                raise
            logger.warning(
                "Database not ready (attempt %s/%s): %s",
                attempt,
                retries,
                exc,
            )
            time.sleep(delay_seconds * attempt)
    if last is not None:
        raise last


def recover_if_uninitialized(exc: BaseException) -> bool:
    """If *exc* looks like a missing schema, create tables and return True."""
    if not is_db_not_ready_error(exc):
        return False
    logger.warning("Uninitialized database detected; creating tables: %s", exc)
    ensure_db_and_tables()
    return True


def _session_generator():
    with Session(engine) as session:
        try:
            yield session
        except Exception as e:
            logger.error(f"Database session error: {e}")
            raise
        finally:
            session.close()


def get_session():
    yield from _session_generator()


@contextmanager
def get_cli_session():
    yield from _session_generator()
