"""Reusable, faithful provider test doubles for `shape`."""

from .base import EffectInfo, HttpFake, MailFake, MailRequest, Request, Response
from .discord import DiscordFake
from .github import GitHubFake
from .gmail import GmailFake
from .google_calendar import GoogleCalendarFake
from .imap import ImapFake
from .jira import JiraFake
from .langgraph import LangGraphFake
from .linear import LinearFake
from .sendgrid import SendGridFake
from .slack import SlackFake
from .smtp import SmtpFake
from .stripe import StripeFake
from .telegram import TelegramFake
from .twilio import TwilioFake

__all__ = [
    "DiscordFake",
    "EffectInfo",
    "GitHubFake",
    "GmailFake",
    "GoogleCalendarFake",
    "HttpFake",
    "ImapFake",
    "JiraFake",
    "LangGraphFake",
    "LinearFake",
    "MailFake",
    "MailRequest",
    "Request",
    "Response",
    "SendGridFake",
    "SlackFake",
    "SmtpFake",
    "StripeFake",
    "TelegramFake",
    "TwilioFake",
]


def _load_metadata():
    import json
    from importlib.resources import files

    providers = {
        "discord": DiscordFake, "github": GitHubFake, "gmail": GmailFake,
        "google-calendar": GoogleCalendarFake, "imap": ImapFake, "jira": JiraFake,
        "langgraph": LangGraphFake, "linear": LinearFake, "sendgrid": SendGridFake,
        "slack": SlackFake, "smtp": SmtpFake, "stripe": StripeFake,
        "telegram": TelegramFake, "twilio": TwilioFake,
    }
    results = json.loads(files(__name__).joinpath("contract_results.json").read_text())
    for name, fake in providers.items():
        result = results[name]
        fake.verified_on = result["verified_on"] if result else None
        fake.verification_status = (
            f"verified against live on {fake.verified_on}" if fake.verified_on else "unverified"
        )
    return providers


_PROVIDERS = _load_metadata()
