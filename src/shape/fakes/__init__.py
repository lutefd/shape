"""Reusable, faithful provider test doubles for `shape`."""

from .base import EffectInfo, HttpFake, MailFake, MailRequest, Request, Response
from .github import GitHubFake
from .imap import ImapFake
from .sendgrid import SendGridFake
from .slack import SlackFake
from .smtp import SmtpFake
from .stripe import StripeFake
from .twilio import TwilioFake

__all__ = [
    "EffectInfo",
    "GitHubFake",
    "HttpFake",
    "ImapFake",
    "MailFake",
    "MailRequest",
    "Request",
    "Response",
    "SendGridFake",
    "SlackFake",
    "SmtpFake",
    "StripeFake",
    "TwilioFake",
]
