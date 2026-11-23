"""Intercept mail submission and mailbox commands while using stdlib client APIs."""

from __future__ import annotations

import functools
import imaplib
import re
import smtplib
from urllib.parse import urlsplit

from ..fakes.base import MailRequest
from . import _patch

_IMAP_WRITES = {
    "STORE", "APPEND", "EXPUNGE", "COPY", "MOVE", "CLOSE", "CREATE", "DELETE", "RENAME",
    "SUBSCRIBE", "UNSUBSCRIBE", "SETACL", "DELETEACL", "SETQUOTA", "SETANNOTATION",
}
_IMAP_READS = {
    "SELECT", "EXAMINE", "SEARCH", "FETCH", "LIST", "LSUB", "STATUS", "SORT", "THREAD",
}
_IMAP_SETUP = {"LOGIN", "AUTHENTICATE", "CAPABILITY", "ENABLE", "LOGOUT", "NOOP", "UNSELECT"}
_CAPABILITIES = ("IMAP4REV1", "UIDPLUS", "MOVE", "ENABLE", "UTF8=ACCEPT")


def _smtp(stack, capture):
    init = smtplib.SMTP.__init__
    close = smtplib.SMTP.close

    def initialize(client, host="", port=0, local_hostname=None, *args, **kwargs):
        init(client, host, port, local_hostname or "[127.0.0.1]", *args, **kwargs)

    def connect(client, host="localhost", port=0, source_address=None):
        hostname = urlsplit(f"//{host}").hostname if host.count(":") <= 1 else host.strip("[]")
        capture.provider(hostname or "", "send")
        client._host = hostname
        client._shape_connected = True
        client.helo_resp = client.ehlo_resp = None
        client.esmtp_features = {}
        return 220, b"shape SMTP ready"

    def ensure_connected(client):
        if not getattr(client, "_shape_connected", False):
            raise smtplib.SMTPServerDisconnected("please run connect() first")

    def ehlo(client, name=""):
        ensure_connected(client)
        client.does_esmtp = True
        client.ehlo_resp = b"shape SMTP"
        client.esmtp_features = {"auth": "PLAIN", "starttls": "", "smtputf8": ""}
        return 250, client.ehlo_resp

    def helo(client, name=""):
        ensure_connected(client)
        client.helo_resp = b"shape SMTP"
        return 250, client.helo_resp

    def docmd(client, cmd, args=""):
        ensure_connected(client)
        if cmd.upper() == "QUIT":
            return 221, b"Bye"
        if cmd.upper() == "AUTH":
            return 235, b"Authenticated"
        return 250, b"OK"

    def starttls(client, *, context=None):
        ensure_connected(client)
        client.helo_resp = client.ehlo_resp = None
        client.does_esmtp = False
        client.esmtp_features = {}
        return 220, b"Ready to start TLS"

    def disconnect(client):
        client._shape_connected = False
        close(client)

    def sendmail(client, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
        ensure_connected(client)
        host = client._host
        provider = capture.provider(host, "send")
        recipients = (to_addrs,) if isinstance(to_addrs, str) else tuple(to_addrs)
        data = re.sub(r"\r\n|\n|\r", "\r\n", msg).encode("ascii") if isinstance(msg, str) else msg
        req = MailRequest("SEND", host, (from_addr, recipients, data))

        def submit(fake, state):
            return fake.send(*req.args, state)

        def dropped():
            return smtplib.SMTPServerDisconnected("shape: injected disconnect")

        failures = {
            "timeout": dropped,
            "connection_reset_after_commit": dropped,
            **{name: functools.partial(smtplib.SMTPDataError, code, b"shape: injected SMTP error")
               for name, code in (("rate_limit", 452), ("server_error", 451), ("client_error", 550))},
        }
        try:
            refused = capture.perform(req, f"SMTP SEND {host}",
                                      {"from_addr": from_addr, "to_addrs": recipients},
                                      provider, submit, failures, safe=False)
        except smtplib.SMTPServerDisconnected:
            disconnect(client)
            raise
        if not recipients or all(addr in refused for addr in recipients):
            raise smtplib.SMTPRecipientsRefused(refused)
        return refused

    # send_message keeps its real envelope extraction and MIME serialization and calls sendmail.
    for name, fn in (
        ("__init__", initialize), ("connect", connect), ("helo", helo), ("ehlo", ehlo),
        ("docmd", docmd), ("starttls", starttls), ("close", disconnect), ("sendmail", sendmail),
    ):
        _patch(stack, smtplib.SMTP, name, fn)


def _imap(stack, capture):
    def open_connection(client, host="", port=imaplib.IMAP4_PORT, timeout=None):
        capture.provider(host, "command")
        client.host, client.port = host, port

    def connect(client):
        client.state = "NONAUTH"
        client.welcome = b"* OK shape IMAP ready"
        client.capabilities = _CAPABILITIES
        client.PROTOCOL_VERSION = "IMAP4REV1"

    def shutdown(client):
        client.state = "LOGOUT"

    def secure(client, ssl_context=None):
        if client._tls_established or isinstance(client, imaplib.IMAP4_SSL):
            raise client.abort("TLS session already established")
        client._tls_established = True
        return "OK", [b"Begin TLS"]

    def command(client, name, *args):
        literal, client.literal = client.literal, None
        command_name = name
        if name == "UID":
            first, *args = args
            command_name = first.decode("ascii") if isinstance(first, bytes) else first
        if client.state not in imaplib.Commands[name]:
            raise client.error(f"command {name} illegal in state {client.state}")
        if command_name in _IMAP_SETUP:
            if command_name == "CAPABILITY":
                client.untagged_responses["CAPABILITY"] = [" ".join(_CAPABILITIES).encode()]
            if command_name == "LOGOUT":
                return "BYE", [b"Logged out"]
            return "OK", [b"OK"]

        host = client.host
        provider = capture.provider(host, "command")
        args = tuple(arg.decode(client._encoding, errors="surrogateescape")
                     if isinstance(arg, bytes) else arg for arg in args)
        if literal is not None and command_name == "APPEND":
            args += (literal,)
        method = f"UID {command_name}" if name == "UID" else command_name
        req = MailRequest(name, host, (command_name, *args) if name == "UID" else args)

        def abort():
            return client.abort("shape: injected IMAP disconnect")

        failures = {"timeout": abort, "connection_reset_after_commit": abort}
        failures.update({name: functools.partial(client.error, f"shape: injected IMAP {name}")
                         for name in ("rate_limit", "server_error", "client_error")})
        try:
            status, data = capture.perform(
                req, f"IMAP {method} {host}",
                {"args": args[:-1] if command_name == "APPEND" else args},
                provider, lambda fake, state: fake.command(req.method, req.args, state), failures,
                safe=command_name not in _IMAP_WRITES,
            )
        except client.abort:
            shutdown(client)
            raise
        if status == "BAD":
            raise client.error(f"{method} command error: {data!r}")
        if status == "OK":
            if command_name in {"SELECT", "EXAMINE"}:
                client.untagged_responses["EXISTS"] = data
            elif command_name in _IMAP_READS or command_name in {"STORE", "EXPUNGE"} or name == "UID":
                response = command_name
                if command_name == "STORE" or (name == "UID"
                                              and command_name not in {"SEARCH", "SORT", "THREAD"}):
                    response = "FETCH"
                client.untagged_responses[response] = data
        return status, data

    for name, fn in (
        ("open", open_connection), ("_connect", connect), ("shutdown", shutdown),
        ("starttls", secure), ("_simple_command", command),
    ):
        _patch(stack, imaplib.IMAP4, name, fn)
    # IMAP4_SSL.open delegates to IMAP4.open. Some releases perform their own shutdown.
    if "shutdown" in vars(imaplib.IMAP4_SSL):
        _patch(stack, imaplib.IMAP4_SSL, "shutdown", shutdown)
    if "starttls" in vars(imaplib.IMAP4_SSL):
        _patch(stack, imaplib.IMAP4_SSL, "starttls", secure)


def install(stack, capture):
    _smtp(stack, capture)
    _imap(stack, capture)
