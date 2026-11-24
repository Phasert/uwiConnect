from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Protocol, runtime_checkable

from app.config import get_settings
from app.services.accounts import normalize_email


@runtime_checkable
class EmailSender(Protocol):
    def send(self, *, recipient: str, subject: str, message: str) -> None: ...


class EmailError(RuntimeError):
    pass


class EmailConfigurationError(EmailError):
    pass


class EmailSendError(EmailError):
    pass


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    from_address: str
    timeout: float
    use_implicit_tls: bool
    use_starttls: bool

    def __post_init__(self) -> None:
        host = str(self.host or "").strip()
        from_address = str(self.from_address or "").strip()
        username = str(self.username or "").strip()
        password = str(self.password or "")

        missing = [
            name
            for name, value in (
                ("SMTP_HOST", host),
                ("SMTP_FROM_ADDRESS", from_address),
                ("SMTP_USERNAME", username),
                ("SMTP_PASSWORD", password),
            )
            if not value
        ]
        if missing:
            raise EmailConfigurationError(
                "Missing required email configuration: " + ", ".join(missing)
            )
        if "\r" in from_address or "\n" in from_address:
            raise EmailConfigurationError(
                "SMTP_FROM_ADDRESS must not contain line breaks"
            )

        try:
            port = int(self.port)
            timeout = float(self.timeout)
        except (TypeError, ValueError):
            raise EmailConfigurationError(
                "SMTP_PORT and SMTP_TIMEOUT must be numbers"
            ) from None

        if not 1 <= port <= 65535:
            raise EmailConfigurationError("SMTP_PORT must be between 1 and 65535")
        if not 0 < timeout <= 60:
            raise EmailConfigurationError(
                "SMTP_TIMEOUT must be greater than 0 and at most 60"
            )

        use_implicit_tls = bool(self.use_implicit_tls)
        use_starttls = bool(self.use_starttls)
        if use_implicit_tls == use_starttls:
            raise EmailConfigurationError(
                "Set exactly one of SMTP_USE_IMPLICIT_TLS or SMTP_USE_STARTTLS"
            )

        object.__setattr__(self, "host", host)
        object.__setattr__(self, "from_address", from_address)
        object.__setattr__(self, "username", username)
        object.__setattr__(self, "password", password)
        object.__setattr__(self, "port", port)
        object.__setattr__(self, "timeout", timeout)
        object.__setattr__(self, "use_implicit_tls", use_implicit_tls)
        object.__setattr__(self, "use_starttls", use_starttls)

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> SmtpConfig:
        values = get_settings() if settings is None else settings
        return cls(
            host=getattr(values, "smtp_host", ""),
            port=getattr(values, "smtp_port", 587),
            username=getattr(values, "smtp_username", ""),
            password=getattr(values, "smtp_password", ""),
            from_address=getattr(values, "smtp_from_address", ""),
            timeout=getattr(values, "smtp_timeout", 10.0),
            use_implicit_tls=getattr(values, "smtp_use_implicit_tls", False),
            use_starttls=getattr(values, "smtp_use_starttls", True),
        )


@runtime_checkable
class SmtpTransport(Protocol):
    def ehlo(self) -> Any: ...

    def starttls(self, context: ssl.SSLContext | None = None) -> Any: ...

    def login(self, user: str, password: str) -> Any: ...

    def send_message(self, msg: EmailMessage) -> Any: ...

    def quit(self) -> Any: ...


def _default_transport_factory(config: SmtpConfig) -> SmtpTransport:
    if config.use_implicit_tls:
        return smtplib.SMTP_SSL(
            config.host,
            config.port,
            timeout=config.timeout,
            context=ssl.create_default_context(),
        )
    return smtplib.SMTP(config.host, config.port, timeout=config.timeout)


def _validate_recipient(recipient: str) -> None:
    if not isinstance(recipient, str) or not recipient.strip():
        raise EmailSendError("recipient must be a non-empty string")
    if "\r" in recipient or "\n" in recipient or recipient.count("@") != 1:
        raise EmailSendError("recipient must be a single valid email address")


def _validate_header(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise EmailSendError(f"{label} must be a non-empty string")
    if "\r" in value or "\n" in value:
        raise EmailSendError(f"{label} must not contain line breaks")


class SmtpEmailSender:
    def __init__(
        self,
        config: SmtpConfig | None = None,
        *,
        transport_factory: Callable[[SmtpConfig], SmtpTransport] | None = None,
    ) -> None:
        self._config = config
        self._transport_factory = transport_factory or _default_transport_factory

    def send(self, *, recipient: str, subject: str, message: str) -> None:
        config = self._config or SmtpConfig.from_settings()
        recipient = (
            normalize_email(recipient) if isinstance(recipient, str) else recipient
        )
        subject = subject.strip() if isinstance(subject, str) else subject
        _validate_recipient(recipient)
        _validate_header(subject, "subject")
        if not isinstance(message, str) or not message.strip():
            raise EmailSendError("message must be a non-empty string")

        email = EmailMessage()
        try:
            email["From"] = config.from_address
            email["To"] = recipient
            email["Subject"] = subject
        except ValueError:
            raise EmailSendError("invalid email headers") from None
        email.set_content(message)

        try:
            transport = self._transport_factory(config)
        except (OSError, smtplib.SMTPException):
            raise EmailSendError("SMTP connection failed") from None

        try:
            transport.ehlo()
            if config.use_starttls:
                transport.starttls(context=ssl.create_default_context())
                transport.ehlo()
            transport.login(config.username, config.password)
            transport.send_message(email)
        except (OSError, smtplib.SMTPException):
            raise EmailSendError("SMTP delivery failed") from None
        finally:
            with suppress(Exception):
                transport.quit()


smtp_email_sender = SmtpEmailSender()


def send_email(*, recipient: str, subject: str, message: str) -> None:
    smtp_email_sender.send(recipient=recipient, subject=subject, message=message)
