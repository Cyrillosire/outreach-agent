"""Main-content extraction from HTML (trafilatura) plus link discovery. Pure functions over untrusted input."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import lxml.html
import trafilatura

from app.integrations.web.urls import absolute

_WS = re.compile(r"[ \t\r\f\v]+")
_NEWLINES = re.compile(r"\n{3,}")


@dataclass
class ExtractedPage:
    title: str | None
    text: str
    published_at: datetime | None
    links: list[tuple[str, str]] = field(default_factory=list)  # (absolute url, anchor text)


def _clean(text: str) -> str:
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    return _NEWLINES.sub("\n\n", "\n".join(lines)).strip()


def extract_page(html: str, url: str, *, max_chars: int = 60_000) -> ExtractedPage:
    text = trafilatura.extract(html, url=url, output_format="txt", include_comments=False, include_tables=True,
                               favor_precision=True, deduplicate=True) or ""
    meta = trafilatura.extract_metadata(html, default_url=url)
    title = (meta.title if meta and meta.title else None)
    published = None
    if meta and meta.date:
        try:
            published = datetime.fromisoformat(meta.date).replace(tzinfo=UTC)
        except ValueError:
            published = None
    links: list[tuple[str, str]] = []
    try:
        doc = lxml.html.fromstring(html)
        if not title:
            found = doc.findtext(".//title")
            title = found.strip() if found else None
        if not text:
            # Fallback when trafilatura finds no main content (e.g. very short pages).
            for bad in doc.xpath("//script|//style|//noscript|//template"):
                bad.drop_tree()
            text = doc.text_content()
        for a in doc.xpath("//a[@href]")[:500]:
            target = absolute(url, a.get("href", ""))
            if target:
                links.append((target, _WS.sub(" ", a.text_content() or "").strip()[:120]))
    except (lxml.etree.ParserError, ValueError):
        pass
    return ExtractedPage(title=title[:500] if title else None, text=_clean(text)[:max_chars],
                         published_at=published, links=links)


def extract_plain(text: str, *, max_chars: int = 60_000) -> ExtractedPage:
    return ExtractedPage(title=None, text=_clean(text)[:max_chars], published_at=None, links=[])
