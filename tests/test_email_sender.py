import smtplib
import traceback

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.services import email_sender
from app.services.email_sender import (
    EmailConfigurationError,
    EmailSendError,
    SmtpEmailSender,
    send_email,
)
from app.services.email_service import SMTPEmailService


class FakeSmtpService:
    def __init__(
        self,
        *,
        config_error: Exception | None = None,
        send_error: Exception | None = None,
    ) -> None:
        self.config_error = config_error
        self.send_error = send_error
        self.validation_calls = 0
        self.calls: list[tuple[object, ...]] = []

    def validate_configuration(self) -> None:
        self.validation_calls += 1
        if self.config_error is not None:
            raise self.config_error

    def send_email(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))
        if self.send_error is not None:
            raise self.send_error


def _settings(**overrides):
    values = {
        "database_uri": "sqlite://",
        "secret_key": "test-key",
        "env": "test",
        "smtp_host": "smtp.example.test",
        "smtp_port": 587,
        "smtp_username": "sender@example.test",
        "smtp_password": SecretStr("not-a-real-password"),
        "smtp_from_email": "no-reply@example.test",
        "smtp_from_name": "UWI Connect",
        "smtp_use_tls": True,
        "smtp_timeout": 10.0,
        "notification_email_enabled": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_adapter_delegates_one_recipient_per_call_to_shared_service() -> None:
    service = FakeSmtpService()
    sender = SmtpEmailSender(service)

    sender.send(
        recipient="  Alum@MY.UWI.EDU ",
        subject="  New listing  ",
        message="A new job is available.",
    )

    assert service.validation_calls == 1
    assert service.calls == [
        (
            ("alum@my.uwi.edu", "New listing", "A new job is available."),
            {},
        )
    ]


@pytest.mark.parametrize(
    ("recipient", "subject", "message"),
    [
        ("", "Hi", "Body"),
        ("one@x.test\ntwo@x.test", "Hi", "Body"),
        ("one@x.test", "", "Body"),
        ("one@x.test", "Hi\r\nBcc: victim@x.test", "Body"),
        ("one@x.test", "Hi", "   "),
    ],
)
def test_invalid_inputs_fail_before_the_shared_service_is_called(
    recipient: str, subject: str, message: str
) -> None:
    service = FakeSmtpService()
    sender = SmtpEmailSender(service)

    with pytest.raises(EmailSendError):
        sender.send(recipient=recipient, subject=subject, message=message)

    assert service.validation_calls == 0
    assert service.calls == []


def test_configuration_error_is_typed_and_does_not_expose_credentials() -> None:
    service = FakeSmtpService(
        config_error=ValueError("SMTP password is not configured")
    )
    sender = SmtpEmailSender(service)

    with pytest.raises(
        EmailConfigurationError, match="SMTP password is not configured"
    ) as exc_info:
        sender.send(recipient="alum@my.uwi.edu", subject="Hi", message="Body")

    assert service.calls == []
    rendered = "".join(traceback.format_exception(exc_info.value))
    assert "not-a-real-password" not in rendered


@pytest.mark.parametrize(
    "error",
    [
        OSError("network unreachable"),
        smtplib.SMTPAuthenticationError(535, b"authentication rejected"),
        smtplib.SMTPRecipientsRefused({"alum@my.uwi.edu": (550, b"rejected")}),
    ],
)
def test_transport_failures_are_typed_without_chained_credential_leaks(
    error: Exception,
) -> None:
    service = FakeSmtpService(send_error=error)
    sender = SmtpEmailSender(service)

    with pytest.raises(EmailSendError, match="Email delivery failed") as exc_info:
        sender.send(recipient="alum@my.uwi.edu", subject="Hi", message="Body")

    rendered = "".join(traceback.format_exception(exc_info.value))
    assert "not-a-real-password" not in rendered
    assert "authentication rejected" not in rendered


def test_adapter_uses_shared_smtp_email_service_by_default() -> None:
    sender = SmtpEmailSender()

    assert isinstance(sender._service, SMTPEmailService)


def test_adapter_preserves_secret_str_handling_without_sending() -> None:
    sender = SmtpEmailSender(SMTPEmailService(_settings()))

    sender._service.validate_configuration()

    assert sender._service.settings.smtp_password.get_secret_value() == (
        "not-a-real-password"
    )


def test_send_email_convenience_uses_configured_sender(monkeypatch) -> None:
    calls: list[dict[str, str]] = []

    class StubSender:
        def send(self, **kwargs) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(email_sender, "smtp_email_sender", StubSender())

    send_email(recipient="alum@my.uwi.edu", subject="Hi", message="Body")

    assert calls == [
        {
            "recipient": "alum@my.uwi.edu",
            "subject": "Hi",
            "message": "Body",
        }
    ]


def test_canonical_settings_have_no_legacy_smtp_names() -> None:
    settings = _settings()

    assert not hasattr(settings, "smtp_from_address")
    assert not hasattr(settings, "smtp_use_implicit_tls")
    assert not hasattr(settings, "smtp_use_starttls")
    assert isinstance(settings.smtp_password, SecretStr)
    assert settings.notification_email_enabled is True
