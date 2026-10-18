"""The prospect's LinkedIn presence as evidence, without fetching linkedin.com.

LinkedIn's terms forbid automated access, so pages are never fetched or scraped. Two sources are used instead:
- search indexes (Tavily/Brave): result titles and snippets of their public profile and posts;
- captures: text a user copies from their own browser (profile, posts, activity) and pastes into the prospect.

Identity comes from the profile URL on the prospect's record: a result is theirs only if its URL carries that
profile's slug, so namesakes are excluded without needing the company name on the page.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import unquote, urlsplit

from app.domain.states import SourceType

LinkedInKind = Literal["profile", "post", "activity"]
SOURCE_TYPES: dict[LinkedInKind, SourceType] = {
    "profile": SourceType.LINKEDIN_PROFILE, "post": SourceType.LINKEDIN_POST, "activity": SourceType.LINKEDIN_ACTIVITY,
}
_SLUG = re.compile(r"^[\w\-.%]{3,100}$")  # \w is Unicode-aware: LinkedIn allows non-ASCII slugs
MAX_CAPTURE_CHARS = 60_000
CHUNK_CHARS = 6_000


def profile_slug(url: str | None) -> str | None:
    """`https://www.linkedin.com/in/lena-okafor-12ab/` -> `lena-okafor-12ab` (None if not a personal profile)."""
    if not url:
        return None
    parts = urlsplit(url if "://" in url else f"https://{url}")
    host = (parts.hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2 or segments[0].lower() != "in":
        return None
    slug = unquote(segments[1]).lower()
    return slug if _SLUG.match(slug) else None


def search_queries(full_name: str, slug: str) -> list[str]:
    return [f"site:linkedin.com/in/{slug}", f"site:linkedin.com/posts/{slug}", f'"{full_name}" linkedin posts']


def classify_result(url: str, slug: str) -> LinkedInKind | None:
    """Which part of THIS person's LinkedIn a search result is, or None if it isn't provably theirs."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    path = unquote(parts.path).lower().rstrip("/")
    if path == f"/in/{slug}" or path.startswith(f"/in/{slug}/details/"):
        return "profile"
    if path.startswith(f"/in/{slug}/recent-activity"):
        return "activity"
    # Post URLs are /posts/<author-slug>_<title>-activity-<id>; "_" ends the slug, so "lena-okafor-99" can't match.
    if path.startswith(f"/posts/{slug}_"):
        return "post"
    return None


def result_text(title: str, snippet: str) -> str:
    return "\n".join(x.strip() for x in (title, snippet) if x and x.strip())


def clean_capture(text: str) -> str:
    """Normalize pasted page text: drop blank runs and UI chrome lines, cap the size."""
    chrome = {"like", "comment", "repost", "send", "follow", "message", "more", "see more", "…see more", "show all"}
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n")]
    kept = [ln for ln in lines if ln and ln.lower() not in chrome]
    return "\n".join(kept)[:MAX_CAPTURE_CHARS]


def chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split on line boundaries into pieces of at most `size` characters (long lines are cut)."""
    out: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.split("\n"):
        line = line[:size]
        if length + len(line) + 1 > size and current:
            out.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        out.append("\n".join(current))
    return out
