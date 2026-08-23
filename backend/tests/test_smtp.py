from __future__ import annotations

import ssl

from pydantic import SecretStr

from app.outbox import SmtpEmailSender


class RecordingSmtpConnection:
    def __init__(self) -> None:
        self.events: list[object] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def ehlo(self) -> None:
        self.events.append("ehlo")

    def starttls(self, *, context: ssl.SSLContext) -> None:
        self.events.append(("starttls", context))

    def login(self, username: str, password: str) -> None:
        self.events.append(("login", username, password))

    def send_message(self, message) -> None:
        self.events.append(("send", message["To"], message["Subject"]))


class RecordingFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.connection = RecordingSmtpConnection()

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.connection


def test_smtp_sender_uses_ssl_connection_for_implicit_tls(settings):
    plain_factory = RecordingFactory()
    ssl_factory = RecordingFactory()
    smtp_settings = settings.model_copy(
        update={
            "smtp_host": "smtp.example.org",
            "smtp_port": 465,
            "smtp_username": "mailer@example.org",
            "smtp_password": SecretStr("smtp-password"),
            "smtp_starttls": False,
            "smtp_implicit_tls": True,
        }
    )

    SmtpEmailSender(
        smtp_settings,
        smtp_factory=plain_factory,
        smtp_ssl_factory=ssl_factory,
    ).send(
        "recipient@example.org",
        {"subject": "TLS test", "text": "Body"},
    )

    assert plain_factory.calls == []
    assert len(ssl_factory.calls) == 1
    args, kwargs = ssl_factory.calls[0]
    assert args == ("smtp.example.org", 465)
    assert kwargs["timeout"] == smtp_settings.smtp_timeout_seconds
    assert isinstance(kwargs["context"], ssl.SSLContext)
    assert ssl_factory.connection.events == [
        "ehlo",
        ("login", "mailer@example.org", "smtp-password"),
        ("send", "recipient@example.org", "TLS test"),
    ]


def test_smtp_sender_keeps_starttls_transport_separate(settings):
    plain_factory = RecordingFactory()
    ssl_factory = RecordingFactory()
    smtp_settings = settings.model_copy(
        update={
            "smtp_host": "smtp.example.org",
            "smtp_port": 587,
            "smtp_starttls": True,
            "smtp_implicit_tls": False,
        }
    )

    SmtpEmailSender(
        smtp_settings,
        smtp_factory=plain_factory,
        smtp_ssl_factory=ssl_factory,
    ).send(
        "recipient@example.org",
        {"subject": "STARTTLS test", "text": "Body"},
    )

    assert ssl_factory.calls == []
    assert len(plain_factory.calls) == 1
    assert plain_factory.connection.events[0] == "ehlo"
    assert plain_factory.connection.events[1][0] == "starttls"
    assert isinstance(plain_factory.connection.events[1][1], ssl.SSLContext)
    assert plain_factory.connection.events[2:] == [
        "ehlo",
        ("send", "recipient@example.org", "STARTTLS test"),
    ]
