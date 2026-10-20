"""RFC 5322 message construction for outbound email (plain text, UTF-8).

We build the MIME ourselves for both providers so headers are identical everywhere: our own Message-ID (used to
find the message again during reconciliation), a List-Unsubscribe header, and protection against header injection.
"""

from __future__ import annotations

import re
import uuid
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate

_CONTROL = re.compile(r"[\r\n\x00]")


class HeaderInjection(ValueError):
    pass


def _clean_header(value: str, name: str) -> str:
    if _CONTROL.search(value):
        raise HeaderInjection(f"{name} contains a line break or control character")
    return value.strip()


def new_message_id(from_address: str) -> str:
    domain = from_address.rpartition("@")[2] or "localhost"
    return f"<{uuid.uuid4().hex}@{domain}>"


def build_message(*, from_address: str, from_name: str | None, to_address: str, to_name: str | None, subject: str,
                  body: str, message_id: str, in_reply_to: str | None = None, references: str | None = None,
                  unsubscribe_mailto: str | None = None) -> bytes:
    msg = EmailMessage(policy=SMTP)
    msg["From"] = Address(display_name=_clean_header(from_name or "", "From name"),
                          addr_spec=_clean_header(from_address, "From"))
    msg["To"] = Address(display_name=_clean_header(to_name or "", "To name"), addr_spec=_clean_header(to_address, "To"))
    msg["Subject"] = _clean_header(subject, "Subject")
    msg["Date"] = formatdate(localtime=False, usegmt=True)
    msg["Message-ID"] = _clean_header(message_id, "Message-ID")
    if in_reply_to:
        msg["In-Reply-To"] = _clean_header(in_reply_to, "In-Reply-To")
        msg["References"] = _clean_header(references or in_reply_to, "References")
    if unsubscribe_mailto:
        mailto = _clean_header(unsubscribe_mailto, "List-Unsubscribe")
        msg["List-Unsubscribe"] = f"<mailto:{mailto}?subject=unsubscribe>"
    # quoted-printable keeps plain ASCII readable and survives long lines and non-ASCII names.
    msg.set_content(body.replace("\r\n", "\n"), subtype="plain", charset="utf-8", cte="quoted-printable")
    return msg.as_bytes()
