#!/usr/bin/env python3
"""Starter project CLI — stdlib argparse (no extra CLI library).

From the project root (venv active, deps installed; ``.env`` optional — falls back to ``.env.example``):

    python manage.py init
    python manage.py run
    python manage.py users
    python manage.py report --name "Student Name" --id "816000000"
    python manage.py usecase
    python manage.py skills-verify
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _ensure_models_loaded() -> None:
    import app.models  # noqa: F401


def cmd_init(args: argparse.Namespace) -> None:
    """Create database tables (drops existing by default) and seed demo users."""
    from app.config import get_settings
    from app.database import drop_all, ensure_db_and_tables

    _ensure_models_loaded()
    if args.drop:
        print("Dropping all tables…")
        # Drop can fail on a brand-new empty DB; create path still retries.
        try:
            drop_all()
        except Exception as exc:
            from app.database import is_db_not_ready_error

            if not is_db_not_ready_error(exc):
                raise
            print(f"Database not ready yet while dropping ({exc}); continuing…")
    print("Creating tables…")
    ensure_db_and_tables()
    print(f"Database ready ({get_settings().database_uri}).")
    if getattr(args, "seed", True):
        cmd_seed(args)


def cmd_seed(args: argparse.Namespace) -> None:
    """Insert uwi-connect demo users and sample listings."""
    from app.seed import seed

    _ensure_models_loaded()
    seed(reset=False)
    print("Seed done — scenario users and sample listings.")
    print("API login: admin@uwi.edu / admin-dev (and other seeded emails)")


def cmd_run(args: argparse.Namespace) -> None:
    """Start the FastAPI app with Uvicorn."""
    import uvicorn

    from app.config import get_settings

    settings = get_settings()
    bind_host = args.host or settings.app_host
    bind_port = args.port or settings.app_port
    if args.reload is None:
        use_reload = settings.env.lower() != "production"
    else:
        use_reload = args.reload
    browser_host = "127.0.0.1" if bind_host in {"0.0.0.0", "::"} else bind_host
    print(f"Starting app on http://{browser_host}:{bind_port} (reload={use_reload})")
    uvicorn.run(
        "app.main:app",
        host=bind_host,
        port=bind_port,
        reload=use_reload,
    )


def cmd_report(args: argparse.Namespace) -> None:
    """Export docs/report.md to PDF; merges judge + exports Guide transcripts."""
    from app.report_pdf import export_report
    from app.skill_integrity import format_report, verify

    export_report(
        name=args.name,
        student_id=args.student_id,
        source=None if args.src is None else Path(args.src),
        output=None if args.output is None else Path(args.output),
    )
    print(format_report(verify()))


def cmd_transcripts(args: argparse.Namespace) -> None:
    """Dump all native Guide project transcripts into docs/transcripts/ for submission."""
    from app.transcript_export import export_project_transcripts

    result = export_project_transcripts(make_zip=not args.no_zip)
    if result.found == 0:
        print(
            "Warning: no Guide transcripts found. "
            "Set Starter_TRANSCRIPTS_DIR if chats live outside Cursor's agent-transcripts folder."
        )
        raise SystemExit(2)
    print(f"Submission dump ready: {result.out_dir}")
    if result.zip_path:
        print(f"Zip for submission: {result.zip_path}")


def cmd_skills_verify(args: argparse.Namespace) -> None:
    """Check course skill files against .agents/skills.lock.json."""
    from app.skill_integrity import format_report, verify

    result = verify()
    print(format_report(result))
    if not result.ok:
        raise SystemExit(1)


def cmd_usecase(args: argparse.Namespace) -> None:
    """Render docs/diagrams/use-case.json to a UML use-case PNG."""
    from app.usecase_diagram import render_usecase_png

    dest = render_usecase_png(
        spec_path=None if args.spec is None else Path(args.spec),
        output=None if args.output is None else Path(args.output),
    )
    print(f"Wrote {dest}")


def cmd_skills_lock(args: argparse.Namespace) -> None:
    """Rewrite the skill lockfile (course authors only)."""
    from app.skill_integrity import write_lock

    dest = write_lock()
    print(f"Wrote {dest}")


def cmd_users(args: argparse.Namespace) -> None:
    """List users currently in the database."""
    from sqlmodel import select

    from app.database import get_cli_session
    from app.models.user import User

    _ensure_models_loaded()
    with get_cli_session() as session:
        users = session.exec(select(User)).all()
        if not users:
            print("No users found. Run: python manage.py init")
            return
        for user in users:
            print(
                f"  id={user.id}  username={user.username}  "
                f"role={user.role}  email={user.email}"
            )


def cmd_send_test_email(args: argparse.Namespace) -> None:
    """Send one manually requested SMTP test message."""
    import smtplib

    from app.services.email_service import SMTPEmailService

    try:
        SMTPEmailService().send_email(
            args.to,
            "UWI Connect SMTP test",
            "This is a test email from UWI Connect.",
        )
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        print(f"Test email failed ({type(exc).__name__}).", file=sys.stderr)
        raise SystemExit(1) from None
    print("Test email sent.")


@contextmanager
def _campaign_processor_lock(lock_path: Path | None = None) -> Iterator[bool]:
    """Return whether this process acquired the campaign processor lock."""
    path = lock_path or (
        Path(__file__).resolve().parent.parent / ".campaign-process.lock"
    )
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a+b") as lock_file:
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)

        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def cmd_process_campaigns(args: argparse.Namespace) -> None:
    """Process due campaigns and recover campaigns left in sending state."""
    from app.config import get_settings
    from app.database import ensure_db_and_tables
    from app.services import campaigns
    from app.services.email_notifications import register_email_observer
    from app.services.email_sender import EmailConfigurationError
    from app.services.website_notifications import register_website_observer

    with _campaign_processor_lock() as acquired:
        if not acquired:
            print("Campaign processor is already running.")
            return
        ensure_db_and_tables()
        register_website_observer()
        try:
            register_email_observer(get_settings())
        except EmailConfigurationError as exc:
            print(f"Notification email configuration error: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        result = campaigns.process_due_campaigns()
        print(
            f"Campaign processor: {result['processed']} processed, "
            f"{result['retained']} retained in sending state."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python manage.py",
        description="Starter Python CLI — init database, seed demo data, run the app.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser(
        "init",
        help="Create DB tables and seed demo users (drops existing tables by default)",
    )
    p_init.add_argument(
        "--no-drop",
        dest="drop",
        action="store_false",
        help="Create tables without dropping existing ones",
    )
    p_init.add_argument(
        "--no-seed",
        dest="seed",
        action="store_false",
        help="Skip demo user seed after creating tables",
    )
    p_init.set_defaults(drop=True, seed=True, func=cmd_init)

    p_seed = sub.add_parser(
        "seed",
        help="Insert demo users only (idempotent; also runs as part of init)",
    )
    p_seed.set_defaults(func=cmd_seed)

    p_run = sub.add_parser("run", help="Start the web app (uvicorn)")
    p_run.add_argument("--host", default=None, help="Bind host")
    p_run.add_argument("--port", type=int, default=None, help="Bind port")
    reload_group = p_run.add_mutually_exclusive_group()
    reload_group.add_argument(
        "--reload",
        dest="reload",
        action="store_true",
        default=None,
        help="Enable auto-reload",
    )
    reload_group.add_argument(
        "--no-reload", dest="reload", action="store_false", help="Disable auto-reload"
    )
    p_run.set_defaults(func=cmd_run, reload=None)

    p_users = sub.add_parser("users", help="List users in the database")
    p_users.set_defaults(func=cmd_users)

    p_test_email = sub.add_parser(
        "send-test-email",
        help="Send a test email using the configured SMTP account",
    )
    p_test_email.add_argument("--to", required=True, help="Recipient email address")
    p_test_email.set_defaults(func=cmd_send_test_email)

    p_process_campaigns = sub.add_parser(
        "process-campaigns",
        help="Process due scheduled campaigns",
    )
    p_process_campaigns.set_defaults(func=cmd_process_campaigns)

    p_report = sub.add_parser(
        "report",
        help="Export docs/report.md to PDF; merges judge.md and exports Guide transcripts",
    )
    p_report.add_argument(
        "--name", required=True, help="Student name (printed on the PDF cover)"
    )
    p_report.add_argument(
        "--id", dest="student_id", required=True, help="Student ID (PDF only)"
    )
    p_report.add_argument(
        "--src", default=None, help="Markdown path (default: docs/report.md)"
    )
    p_report.add_argument(
        "--output", default=None, help="PDF path (default: docs/report.pdf)"
    )
    p_report.set_defaults(func=cmd_report)

    p_transcripts = sub.add_parser(
        "transcripts",
        help="Dump all native Guide project chats to docs/transcripts/ (+ zip) for submission",
    )
    p_transcripts.add_argument(
        "--no-zip",
        action="store_true",
        help="Skip writing docs/transcripts.zip",
    )
    p_transcripts.set_defaults(func=cmd_transcripts)

    p_usecase = sub.add_parser(
        "usecase",
        help="Render docs/diagrams/use-case.json to a UML use-case PNG",
    )
    p_usecase.add_argument(
        "--spec", default=None, help="JSON spec (default: docs/diagrams/use-case.json)"
    )
    p_usecase.add_argument(
        "--output", default=None, help="PNG path (default: docs/diagrams/use-case.png)"
    )
    p_usecase.set_defaults(func=cmd_usecase)

    p_skills_verify = sub.add_parser(
        "skills-verify",
        help="Check course skills against .agents/skills.lock.json",
    )
    p_skills_verify.set_defaults(func=cmd_skills_verify)

    p_skills_lock = sub.add_parser(
        "skills-lock",
        help="Rewrite .agents/skills.lock.json (course authors; needs Starter_SKILLS_LOCK=1)",
    )
    p_skills_lock.set_defaults(func=cmd_skills_lock)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
