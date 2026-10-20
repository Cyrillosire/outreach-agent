from email import message_from_bytes
from email.policy import default

import pytest

from app.integrations.email.mime import HeaderInjection, build_message, new_message_id


def _parse(raw: bytes):  # type: ignore[no-untyped-def]
    return message_from_bytes(raw, policy=default)


def test_headers_body_and_unsubscribe() -> None:
    mid = new_message_id("asha@penthus.example")
    raw = build_message(from_address="asha@penthus.example", from_name="Asha Rao", to_address="lena@northwind.example",
                        to_name="Dr. Lena Okafor", subject="Northwind's HIS rollout", body="Hi Lena,\n\nLine two.",
                        message_id=mid, unsubscribe_mailto="asha@penthus.example")
    msg = _parse(raw)
    assert msg["Message-ID"] == mid and mid.endswith("@penthus.example>")
    assert msg["From"].addresses[0].display_name == "Asha Rao"
    assert msg["To"].addresses[0].addr_spec == "lena@northwind.example"
    assert msg["List-Unsubscribe"] == "<mailto:asha@penthus.example?subject=unsubscribe>"
    assert msg.get_content_type() == "text/plain"
    assert msg.get_content().replace("\r\n", "\n").strip() == "Hi Lena,\n\nLine two."
    assert b"\r\n" in raw  # RFC 5322 line endings


def test_non_ascii_names_and_long_lines_survive() -> None:
    body = "Grüße aus München. " * 40
    raw = build_message(from_address="a@x.example", from_name="Jörg Müller", to_address="b@y.example",
                        to_name="Zoë", subject="Café ☕ plans", body=body, message_id="<1@x.example>")
    msg = _parse(raw)
    assert msg["Subject"] == "Café ☕ plans" and msg["From"].addresses[0].display_name == "Jörg Müller"
    assert msg.get_content().replace("\r\n", "\n").strip() == body.strip()
    assert raw.isascii() and all(len(line) <= 78 for line in raw.split(b"\r\n"))  # 7-bit, wrapped (RFC 5322)


@pytest.mark.parametrize("field,value", [
    ("subject", "Hello\r\nBcc: victim@example.com"),
    ("to_address", "a@b.example\nBcc: x@y.example"),
    ("from_name", "Asha\rBcc: x@y.example"),
])
def test_header_injection_is_refused(field: str, value: str) -> None:
    args = {"from_address": "a@x.example", "from_name": "A", "to_address": "b@y.example", "to_name": None,
            "subject": "s", "body": "b", "message_id": "<1@x.example>", field: value}
    with pytest.raises(HeaderInjection):
        build_message(**args)  # type: ignore[arg-type]


def test_reply_threading_headers() -> None:
    msg = _parse(build_message(from_address="a@x.example", from_name=None, to_address="b@y.example", to_name=None,
                               subject="Re: s", body="b", message_id="<2@x.example>", in_reply_to="<1@x.example>"))
    assert msg["In-Reply-To"] == "<1@x.example>" and msg["References"] == "<1@x.example>"
