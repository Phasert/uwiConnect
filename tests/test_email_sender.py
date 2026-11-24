import smtplib
import ssl
import traceback
from types import SimpleNamespace

import pytest

from app.services import email_sender
from app.services.email_sender import (
    EmailConfigurationError,
    EmailSendError,
    SmtpConfig,
    SmtpEmailSender,
    send_email,
)


class FakeTransport:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.calls: list[object] = []
        self.fail_on = fail_on
        self.messages: list[object] = []
        self.starttls_contexts: list[object] = []

    def _maybe_fail(self, name: str) -> None:
        if self.fail_on == name:
            raise smtplib.SMTPException(f"{name} failed")

    def ehlo(self) -> None:
        self.calls.append("ehlo")
        self._maybe_fail("ehlo")

    def starttls(self, context=None) -> None:
        self.calls.append("starttls")
        self.starttls_contexts.append(context)
        self._maybe_fail("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user, password))
        self._maybe_fail("login")

    def send_message(self, msg) -> None:
        self.calls.append("send_message")
        self._maybe_fail("send_message")
        self.messages.append(msg)

    def quit(self) -> None:
        self.calls.append("quit")


def _config(**overrides) -> SmtpConfig:
    values = {
        "host": "smtp.example.test",
        "port": 587,
        "username": "notifications@example.test",
        "password": "not-a-real-password",
        "from_address": "no-reply@example.test",
        "timeout": 5.0,
        "use_implicit_tls": False,
        "use_starttls": True,
    }
    values.update(overrides)
    return SmtpConfig(**values)


def _settings(**overrides):
    values = {
        "smtp_host": "smtp.example.test",
        "smtp_port": 587,
        "smtp_username": "user@example.test",
        "smtp_password": "not-a-real-password",
        "smtp_from_address": "no-reply@example.test",
        "smtp_timeout": 10.0,
        "smtp_use_implicit_tls": False,
        "smtp_use_starttls": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_missing_configuration_raises_before_transport_creation(monkeypatch) -> None:
    monkeypatch.setattr(
        email_sender,
        "get_settings",
        lambda: _settings(
            smtp_host="", smtp_username="", smtp_password="", smtp_from_address=""
        ),
    )
    created: list[object] = []
    sender = SmtpEmailSender(transport_factory=lambda config: created.append(config))

    with pytest.raises(EmailConfigurationError) as exc_info:
        sender.send(recipient="alum@my.uwi.edu", subject="Hello", message="Body")

    message = str(exc_info.value)
    assert "SMTP_HOST" in message
    assert "SMTP_PASSWORD" in message
    assert "not-a-real-password" not in message
    assert created == []


def test_contradictory_tls_settings_raise_configuration_error() -> None:
    with pytest.raises(EmailConfigurationError, match="exactly one"):
        SmtpConfig.from_settings(
            _settings(smtp_use_implicit_tls=True, smtp_use_starttls=True)
        )


def test_direct_config_rejects_both_tls_modes_before_transport_factory() -> None:
    factory_calls: list[SmtpConfig] = []

    def factory(config: SmtpConfig) -> FakeTransport:
        factory_calls.append(config)
        return FakeTransport()

    with pytest.raises(EmailConfigurationError, match="exactly one") as exc_info:
        config = _config(use_implicit_tls=True, use_starttls=True)
        sender = SmtpEmailSender(config, transport_factory=factory)
        sender.send(recipient="alum@my.uwi.edu", subject="Hi", message="Body")

    assert factory_calls == []
    assert "not-a-real-password" not in str(exc_info.value)


def test_direct_config_rejects_no_tls_mode_before_transport_factory() -> None:
    factory_calls: list[SmtpConfig] = []

    def factory(config: SmtpConfig) -> FakeTransport:
        factory_calls.append(config)
        return FakeTransport()

    with pytest.raises(EmailConfigurationError, match="exactly one") as exc_info:
        config = _config(use_implicit_tls=False, use_starttls=False)
        sender = SmtpEmailSender(config, transport_factory=factory)
        sender.send(recipient="alum@my.uwi.edu", subject="Hi", message="Body")

    assert factory_calls == []
    assert "not-a-real-password" not in str(exc_info.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"host": ""},
        {"from_address": ""},
        {"username": ""},
        {"password": ""},
        {"port": 0},
        {"port": "bad"},
        {"timeout": 61},
        {"timeout": "bad"},
        {"from_address": "no-reply@example.test\nBcc: victim@x.test"},
    ],
)
def test_direct_config_enforces_all_rules_before_transport_factory(overrides) -> None:
    factory_calls: list[SmtpConfig] = []

    def factory(config: SmtpConfig) -> FakeTransport:
        factory_calls.append(config)
        return FakeTransport()

    with pytest.raises(EmailConfigurationError) as exc_info:
        config = _config(**overrides)
        sender = SmtpEmailSender(config, transport_factory=factory)
        sender.send(recipient="alum@my.uwi.edu", subject="Hi", message="Body")

    assert factory_calls == []
    assert "not-a-real-password" not in str(exc_info.value)


def test_timeout_is_bounded() -> None:
    with pytest.raises(EmailConfigurationError, match="SMTP_TIMEOUT"):
        SmtpConfig.from_settings(_settings(smtp_timeout=61))


def test_from_address_rejects_header_line_breaks() -> None:
    with pytest.raises(EmailConfigurationError, match="line breaks"):
        SmtpConfig.from_settings(
            _settings(smtp_from_address="no-reply@example.test\nBcc: victim@x.test")
        )


def test_default_transport_factory_uses_explicit_tls_settings_and_timeout(
    monkeypatch,
) -> None:
    created: list[tuple[object, ...]] = []
    context = object()

    class DummyTransport:
        pass

    def fake_smtp(host, port, *, timeout):
        created.append(("SMTP", host, port, timeout))
        return DummyTransport()

    def fake_smtp_ssl(host, port, *, timeout, context):
        created.append(("SMTP_SSL", host, port, timeout, context))
        return DummyTransport()

    monkeypatch.setattr(email_sender.smtplib, "SMTP", fake_smtp)
    monkeypatch.setattr(email_sender.smtplib, "SMTP_SSL", fake_smtp_ssl)
    monkeypatch.setattr(email_sender.ssl, "create_default_context", lambda: context)

    email_sender._default_transport_factory(_config())
    email_sender._default_transport_factory(
        _config(use_implicit_tls=True, use_starttls=False)
    )

    assert created == [
        ("SMTP", "smtp.example.test", 587, 5.0),
        ("SMTP_SSL", "smtp.example.test", 587, 5.0, context),
    ]


def test_successful_starttls_send_uses_fake_transport() -> None:
    transport = FakeTransport()
    sender = SmtpEmailSender(_config(), transport_factory=lambda config: transport)

    sender.send(
        recipient="alum@my.uwi.edu",
        subject="New listing",
        message="A new job is available.",
    )

    assert transport.calls[:5] == [
        "ehlo",
        "starttls",
        "ehlo",
        ("login", "notifications@example.test", "not-a-real-password"),
        "send_message",
    ]
    assert transport.calls[-1] == "quit"
    assert len(transport.starttls_contexts) == 1
    assert isinstance(transport.starttls_contexts[0], ssl.SSLContext)
    message = transport.messages[0]
    assert message["From"] == "no-reply@example.test"
    assert message["To"] == "alum@my.uwi.edu"
    assert message["Subject"] == "New listing"
    assert message.get_content().strip() == "A new job is available."


def test_recipient_uses_existing_email_normalization() -> None:
    transport = FakeTransport()
    sender = SmtpEmailSender(_config(), transport_factory=lambda config: transport)

    sender.send(recipient="  Alum@MY.UWI.EDU ", subject="Hi", message="Body")

    assert transport.messages[0]["To"] == "alum@my.uwi.edu"


def test_implicit_tls_does_not_call_starttls() -> None:
    transport = FakeTransport()
    sender = SmtpEmailSender(
        _config(use_implicit_tls=True, use_starttls=False),
        transport_factory=lambda config: transport,
    )

    sender.send(recipient="alum@my.uwi.edu", subject="Hi", message="Body")

    assert "starttls" not in transport.calls
    assert "send_message" in transport.calls


def test_connection_failure_is_explicit() -> None:
    def failing_factory(config: SmtpConfig):
        raise OSError("network unreachable")

    sender = SmtpEmailSender(_config(), transport_factory=failing_factory)

    with pytest.raises(EmailSendError, match="SMTP connection failed"):
        sender.send(recipient="alum@my.uwi.edu", subject="Hi", message="Body")


def test_transport_failure_is_explicit_and_still_closes_connection() -> None:
    transport = FakeTransport(fail_on="send_message")
    sender = SmtpEmailSender(_config(), transport_factory=lambda config: transport)

    with pytest.raises(EmailSendError, match="SMTP delivery failed"):
        sender.send(recipient="alum@my.uwi.edu", subject="Hi", message="Body")

    assert transport.calls[-1] == "quit"


def test_smtp_failure_traceback_does_not_expose_credentials() -> None:
    class LeakyTransport(FakeTransport):
        def login(self, user: str, password: str) -> None:
            raise smtplib.SMTPAuthenticationError(
                535, b"authentication rejected not-a-real-password"
            )

    transport = LeakyTransport()
    sender = SmtpEmailSender(_config(), transport_factory=lambda config: transport)

    with pytest.raises(EmailSendError) as exc_info:
        sender.send(recipient="alum@my.uwi.edu", subject="Hi", message="Body")

    rendered = "".join(traceback.format_exception(exc_info.value))
    assert "SMTP delivery failed" in rendered
    assert "not-a-real-password" not in rendered


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
