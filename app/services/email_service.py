"""Reusable Gmail SMTP email delivery."""

from __future__ import annotations

import logging
import mimetypes
import smtplib
import ssl
from collections.abc import Sequence
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)
Attachment = str | Path | tuple[str, bytes, str]


class SMTPEmailService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings if settings is not None else get_settings()

    def validate_configuration(self) -> None:
        settings = self.settings
        password = settings.smtp_password.get_secret_value()
        if not settings.smtp_host or not settings.smtp_from_email:
            raise ValueError("SMTP host and sender email must be configured")
        if not settings.smtp_username or not password:
            raise ValueError("SMTP username and app password must be configured")
        if not settings.smtp_use_tls:
            raise ValueError("Gmail SMTP requires STARTTLS")
        try:
            Address(addr_spec=settings.smtp_username)
            Address(addr_spec=settings.smtp_from_email)
        except ValueError as exc:
            raise ValueError("SMTP username and sender must be valid email addresses") from exc

    def send_email(
        self,
        recipients: str | Sequence[str],
        subject: str,
        body: str,
        *,
        html_body: str | None = None,
        attachments: Sequence[Attachment] = (),
    ) -> None:
        recipient_list = [recipients] if isinstance(recipients, str) else list(recipients)
        recipient_count = len(recipient_list)
        try:
            self.validate_configuration()
            if not recipient_list:
                raise ValueError("at least one recipient is required")
            for recipient in recipient_list:
                Address(addr_spec=recipient)

            settings = self.settings
            message = EmailMessage()
            message["From"] = formataddr(
                (settings.smtp_from_name, settings.smtp_from_email)
            )
            message["To"] = ", ".join(recipient_list)
            message["Subject"] = subject
            message.set_content(body)
            if html_body is not None:
                message.add_alternative(html_body, subtype="html")

            for attachment in attachments:
                if isinstance(attachment, (str, Path)):
                    path = Path(attachment)
                    content = path.read_bytes()
                    filename = path.name
                    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                else:
                    filename, content, content_type = attachment
                maintype, separator, subtype = content_type.partition("/")
                if not separator or not maintype or not subtype:
                    raise ValueError("attachment content type must be a MIME type")
                message.add_attachment(
                    content,
                    maintype=maintype,
                    subtype=subtype,
                    filename=filename,
                )

            context = ssl.create_default_context()
            with smtplib.SMTP(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout,
            ) as client:
                client.starttls(context=context)
                client.login(
                    settings.smtp_username,
                    settings.smtp_password.get_secret_value(),
                )
                refused = client.send_message(message)
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
        except Exception as exc:
            logger.warning(
                "Email delivery failed (recipient_count=%d, error_type=%s)",
                recipient_count,
                type(exc).__name__,
            )
            raise

        logger.info("Email sent (recipient_count=%d)", recipient_count)
