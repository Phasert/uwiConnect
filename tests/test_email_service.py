import smtplib
from email.message import EmailMessage
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.services.email_service import SMTPEmailService


def smtp_settings(**overrides):
    values = {
        "database_uri": "sqlite://",
        "secret_key": "test-key",
        "env": "test",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_username": "sender@gmail.com",
        "smtp_password": "test-app-password",
        "smtp_from_email": "sender@gmail.com",
        "smtp_from_name": "UWI Connect",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def smtp_client(monkeypatch, *, send_result=None, send_error=None):
    client = MagicMock()
    client.__enter__.return_value = client
    client.send_message.return_value = send_result or {}
    client.send_message.side_effect = send_error
    factory = MagicMock(return_value=client)
    monkeypatch.setattr("app.services.email_service.smtplib.SMTP", factory)
    return client, factory


def test_settings_load_named_smtp_environment_variables(monkeypatch):
    variables = {
        "SMTP_HOST": "smtp.gmail.com",
        "SMTP_PORT": "587",
        "SMTP_USERNAME": "sender@gmail.com",
        "SMTP_PASSWORD": "google-app-password",
        "SMTP_FROM_EMAIL": "sender@gmail.com",
        "SMTP_FROM_NAME": "UWI Connect",
        "SMTP_USE_TLS": "true",
        "SMTP_TIMEOUT": "12",
        "NOTIFICATION_EMAIL_ENABLED": "true",
    }
    for name, value in variables.items():
        monkeypatch.setenv(name, value)

    settings = Settings(
        _env_file=None,
        database_uri="sqlite://",
        secret_key="test-key",
        env="test",
    )

    assert settings.smtp_host == "smtp.gmail.com"
    assert settings.smtp_port == 587
    assert settings.smtp_username == "sender@gmail.com"
    assert settings.smtp_password.get_secret_value() == "google-app-password"
    assert settings.smtp_from_email == "sender@gmail.com"
    assert settings.smtp_from_name == "UWI Connect"
    assert settings.smtp_use_tls is True
    assert settings.smtp_timeout == 12
    assert settings.notification_email_enabled is True


def test_starttls_delivery_supports_recipients_html_and_attachments(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "app.services.email_service.ssl.create_default_context", lambda: "tls-context"
    )
    client, factory = smtp_client(monkeypatch)
    attachment_path = tmp_path / "receipt.txt"
    attachment_path.write_text("receipt contents", encoding="utf-8")
    settings = smtp_settings(smtp_timeout=7.5)

    SMTPEmailService(settings).send_email(
        ["first@example.test", "second@example.test"],
        "Subject",
        "Plain body",
        html_body="<p>HTML body</p>",
        attachments=[
            attachment_path,
            ("report.csv", b"a,b\n1,2\n", "text/csv"),
        ],
    )

    factory.assert_called_once_with("smtp.gmail.com", 587, timeout=7.5)
    client.starttls.assert_called_once_with(context="tls-context")
    client.login.assert_called_once_with("sender@gmail.com", "test-app-password")
    message = client.send_message.call_args.args[0]
    assert isinstance(message, EmailMessage)
    assert message["From"] == "UWI Connect <sender@gmail.com>"
    assert message["To"] == "first@example.test, second@example.test"
    assert message["Subject"] == "Subject"
    assert message.get_body(preferencelist=("plain",)).get_content() == "Plain body\n"
    assert (
        message.get_body(preferencelist=("html",)).get_content() == "<p>HTML body</p>\n"
    )
    assert [part.get_filename() for part in message.iter_attachments()] == [
        "receipt.txt",
        "report.csv",
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"smtp_host": ""},
        {"smtp_from_email": ""},
        {"smtp_username": ""},
        {"smtp_password": ""},
        {"smtp_use_tls": False},
    ],
)
def test_invalid_or_missing_configuration_fails_before_connecting(
    monkeypatch, overrides
):
    factory = MagicMock()
    monkeypatch.setattr("app.services.email_service.smtplib.SMTP", factory)

    with pytest.raises(ValueError):
        SMTPEmailService(smtp_settings(**overrides)).send_email(
            "recipient@example.test", "Subject", "Body"
        )

    factory.assert_not_called()


def test_timeout_is_bounded() -> None:
    with pytest.raises(ValueError):
        smtp_settings(smtp_timeout=61)


def test_invalid_credentials_are_logged_without_secrets(monkeypatch, caplog):
    client, _ = smtp_client(monkeypatch)
    client.login.side_effect = smtplib.SMTPAuthenticationError(
        535, b"authentication failed"
    )
    service = SMTPEmailService(smtp_settings())

    with pytest.raises(smtplib.SMTPAuthenticationError):
        service.send_email("recipient@example.test", "Subject", "Body")

    assert "SMTPAuthenticationError" in caplog.text
    assert "test-app-password" not in caplog.text
    client.login.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("connection timed out"),
        ConnectionError("connection failed"),
        smtplib.SMTPServerDisconnected("connection closed"),
    ],
)
def test_connection_and_delivery_failures_are_reported(monkeypatch, caplog, error):
    factory = MagicMock(side_effect=error)
    monkeypatch.setattr("app.services.email_service.smtplib.SMTP", factory)

    with pytest.raises(type(error)):
        SMTPEmailService(smtp_settings()).send_email(
            "recipient@example.test", "Subject", "Body"
        )

    assert type(error).__name__ in caplog.text
    assert "test-app-password" not in caplog.text


def test_refused_recipient_is_reported(monkeypatch):
    client, _ = smtp_client(
        monkeypatch,
        send_result={"recipient@example.test": (550, b"recipient rejected")},
    )

    with pytest.raises(smtplib.SMTPRecipientsRefused):
        SMTPEmailService(smtp_settings()).send_email(
            "recipient@example.test", "Subject", "Body"
        )

    client.send_message.assert_called_once()


def test_rejects_empty_recipients_and_invalid_mime_types(monkeypatch, tmp_path):
    client, _ = smtp_client(monkeypatch)
    service = SMTPEmailService(smtp_settings())

    with pytest.raises(ValueError, match="at least one recipient"):
        service.send_email([], "Subject", "Body")
    with pytest.raises(ValueError, match="MIME type"):
        service.send_email(
            "recipient@example.test",
            "Subject",
            "Body",
            attachments=[("file.bin", b"data", "invalid")],
        )

    client.send_message.assert_not_called()
