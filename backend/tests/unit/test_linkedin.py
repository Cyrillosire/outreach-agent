import pytest

from app.services.research.linkedin import chunks, classify_result, clean_capture, profile_slug, search_queries


@pytest.mark.parametrize("url,slug", [
    ("https://www.linkedin.com/in/lena-okafor-12ab/", "lena-okafor-12ab"),
    ("linkedin.com/in/Lena-Okafor", "lena-okafor"),
    ("https://in.linkedin.com/in/arjun-mehta/recent-activity/all/", "arjun-mehta"),
    ("https://www.linkedin.com/in/j%C3%B6rg-m/", "jörg-m"),
    ("https://www.linkedin.com/company/northwind/", None),
    ("https://notlinkedin.com/in/lena", None),
    (None, None),
])
def test_profile_slug(url: str | None, slug: str | None) -> None:
    assert profile_slug(url) == slug


@pytest.mark.parametrize("url,kind", [
    ("https://www.linkedin.com/in/lena-okafor", "profile"),
    ("https://uk.linkedin.com/in/lena-okafor/details/experience/", "profile"),
    ("https://www.linkedin.com/in/lena-okafor/recent-activity/comments/", "activity"),
    ("https://www.linkedin.com/posts/lena-okafor_his-rollout-activity-7312-abcd", "post"),
    ("https://www.linkedin.com/posts/lena-okafor-99_other-person-activity-1", None),  # namesake with longer slug
    ("https://www.linkedin.com/in/lena-okafor-99", None),
    ("https://www.linkedin.com/pulse/some-article", None),  # author not provable from the URL
    ("https://example.com/in/lena-okafor", None),
])
def test_classify_result_only_accepts_this_persons_pages(url: str, kind: str | None) -> None:
    assert classify_result(url, "lena-okafor") == kind


def test_queries_target_the_profile() -> None:
    assert search_queries("Lena Okafor", "lena-okafor")[:2] == [
        "site:linkedin.com/in/lena-okafor", "site:linkedin.com/posts/lena-okafor"]


def test_clean_capture_drops_ui_chrome_and_caps_size() -> None:
    text = "Lena Okafor\n\nLike\nComment\nRepost\nSend\n  Posted about clinician time  \n" + "x" * 70_000
    out = clean_capture(text)
    assert out.startswith("Lena Okafor\nPosted about clinician time\n") and len(out) == 60_000


def test_chunks_split_on_lines() -> None:
    parts = chunks("\n".join(["a" * 40] * 10), size=100)
    assert all(len(p) <= 100 for p in parts) and "".join(parts).count("a") == 400
