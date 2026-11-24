from __future__ import annotations

import smtplib
from typing import Protocol, runtime_checkable

from app.services.accounts import normalize_email
from app.services.email_service import SMTPEmailService


@runtime_checkable
class EmailSender(Protocol):
    def send(self, *, recipient: str, subject: str, message: str) -> None: ...


class EmailError(RuntimeError):
    pass


class EmailConfigurationError(EmailError):
    pass


class EmailSendError(EmailError):
    pass


class SmtpEmailSender:
    def __init__(self, service: SMTPEmailService | None = None) -> None:
        self._service = service or SMTPEmailService()

    def send(self, *, recipient: str, subject: str, message: str) -> None:
        normalized_recipient = (
            normalize_email(recipient) if isinstance(recipient, str) else recipient
        )
        if (
            not isinstance(normalized_recipient, str)
            or not normalized_recipient.strip()
            or normalized_recipient.count("@") != 1
            or "\r" in normalized_recipient
            or "\n" in normalized_recipient
        ):
            raise EmailSendError("recipient must be a single valid email address")
        if not isinstance(subject, str) or not subject.strip():
            raise EmailSendError("subject must be a non-empty string")
        if "\r" in subject or "\n" in subject:
            raise EmailSendError("subject must not contain line breaks")
        if not isinstance(message, str) or not message.strip():
            raise EmailSendError("message must be a non-empty string")

        try:
            self._service.validate_configuration()
        except ValueError as exc:
            raise EmailConfigurationError(
                f"Invalid email configuration: {exc}"
            ) from None

        try:
            self._service.send_email(normalized_recipient, subject.strip(), message)
        except ValueError as exc:
            raise EmailSendError(f"Email delivery failed: {exc}") from None
        except (OSError, smtplib.SMTPException):
            raise EmailSendError("Email delivery failed") from None


smtp_email_sender = SmtpEmailSender()


def send_email(*, recipient: str, subject: str, message: str) -> None:
    smtp_email_sender.send(recipient=recipient, subject=subject, message=message)
