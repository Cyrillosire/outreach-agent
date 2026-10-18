"""Provider adapters against mocked HTTP (httpx.MockTransport). No network access."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from app.integrations import http
from app.integrations.llm.base import json_schema_for, strip_code_fences
from app.integrations.llm.groq import GroqProvider
from app.integrations.llm.ollama import OllamaProvider
from app.integrations.llm.router import LLMRouter, LLMUnavailable
from app.integrations.search.providers import BraveSearch, SearchRouter, SearchUnavailable, TavilySearch
from app.integrations.web.extract import extract_page
from app.integrations.web.fetcher import WebFetcher
from app.integrations.web.urls import BlockedURL, check_url_shape, is_public_ip, normalize_url, resolve_public


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_: float) -> None:
        return None
    monkeypatch.setattr(http.asyncio, "sleep", instant)


def client(handler, base: str = "https://api.test") -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base)


# --- http helper -------------------------------------------------------------------------------

def test_retries_5xx_then_succeeds() -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"ok": True})

    r = run(http.request(client(handler), "GET", "/x", provider="p"))
    assert r.json() == {"ok": True} and len(calls) == 3


def test_short_429_waits_long_429_raises() -> None:
    seq = iter([httpx.Response(429, headers={"retry-after": "2"}), httpx.Response(200, json={})])
    assert run(http.request(client(lambda r: next(seq)), "GET", "/x", provider="p")).status_code == 200
    with pytest.raises(http.ProviderRateLimited) as exc:
        run(http.request(client(lambda r: httpx.Response(429, headers={"retry-after": "3600"})), "GET", "/x",
                         provider="p"))
    assert exc.value.retry_after == 3600


def test_auth_and_quota_statuses() -> None:
    with pytest.raises(http.ProviderAuthError):
        run(http.request(client(lambda r: httpx.Response(401)), "GET", "/x", provider="p"))
    with pytest.raises(http.ProviderRateLimited):
        run(http.request(client(lambda r: httpx.Response(432)), "GET", "/x", provider="p",
                         quota_statuses=frozenset({432})))
    with pytest.raises(http.ProviderBadRequest):
        run(http.request(client(lambda r: httpx.Response(400, text="bad")), "GET", "/x", provider="p"))


def test_network_errors_become_unavailable() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")
    with pytest.raises(http.ProviderUnavailable):
        run(http.request(client(handler), "GET", "/x", provider="p"))


# --- LLM ---------------------------------------------------------------------------------------

class Out(BaseModel):
    name: str
    score: float


class Nested(BaseModel):
    items: list[Out]


def test_json_schema_inlines_refs_and_closes_objects() -> None:
    schema = json_schema_for(Nested)
    assert "$defs" not in json.dumps(schema)
    assert schema["properties"]["items"]["items"]["additionalProperties"] is False
    assert strip_code_fences('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_groq_request_shape_and_reasoning_options() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"name":"a","score":1}'}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    p = GroqProvider(api_key="k", base_url="https://api.test", timeout=5, client=client(handler))
    text, usage = run(p.complete_json(model="openai/gpt-oss-20b", system="s", user="u",
                                      schema=json_schema_for(Out), max_tokens=100))
    assert json.loads(text)["name"] == "a" and usage.prompt_tokens == 10
    assert seen["response_format"]["type"] == "json_schema" and seen["reasoning_effort"] == "low"
    assert seen["include_reasoning"] is False and seen["auth"] == "Bearer k" and seen["temperature"] == 0


def test_ollama_request_shape() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"message": {"content": '{"name":"b","score":0.5}'},
                                         "prompt_eval_count": 7, "eval_count": 3})

    p = OllamaProvider(base_url="http://ollama.test", timeout=5, client=client(handler, "http://ollama.test"))
    _text, usage = run(p.complete_json(model="qwen3:4b", system="s", user="u", schema={"type": "object"},
                                      max_tokens=50))
    assert seen["think"] is False and seen["format"] == {"type": "object"} and usage.completion_tokens == 3


class FakeProvider:
    def __init__(self, name: str, responses: list[Any]) -> None:
        self.name = name
        self.responses = responses
        self.calls: list[str] = []

    async def complete_json(self, *, model: str, system: str, user: str, schema: dict[str, Any],
                            max_tokens: int):  # type: ignore[no-untyped-def]
        from app.integrations.llm.base import LLMUsage
        self.calls.append(model)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, LLMUsage(self.name, model, 1, 1, 1.0)


def test_router_falls_back_on_quota_and_remembers_exhaustion() -> None:
    groq = FakeProvider("groq", [http.ProviderRateLimited("groq", "tpd", retry_after=900), '{"name":"x","score":1}'])
    local = FakeProvider("ollama", ['{"name":"local","score":0.2}', '{"name":"local2","score":0.3}'])
    router = LLMRouter(providers={"groq": groq, "ollama": local},
                       chains={"extract": [("groq", "m1"), ("ollama", "q")]})
    first, usage = run(router.complete(task="extract", system="s", user="u", output=Out))
    assert first.name == "local" and usage.provider == "ollama"
    second, _ = run(router.complete(task="extract", system="s", user="u", output=Out))
    assert second.name == "local2" and groq.calls == ["m1"]  # exhausted model skipped, not retried


def test_router_repairs_invalid_output_once() -> None:
    p = FakeProvider("groq", ["not json", '{"name":"fixed","score":0.9}'])
    router = LLMRouter(providers={"groq": p}, chains={"t": [("groq", "m")]})
    out, _ = run(router.complete(task="t", system="s", user="u", output=Out))
    assert out.name == "fixed" and len(p.calls) == 2


def test_router_raises_with_earliest_retry_when_all_exhausted() -> None:
    a = FakeProvider("groq", [http.ProviderRateLimited("groq", "x", retry_after=500)])
    b = FakeProvider("groq2", [http.ProviderRateLimited("groq2", "x", retry_after=120)])
    router = LLMRouter(providers={"groq": a, "groq2": b}, chains={"t": [("groq", "m"), ("groq2", "m")]})
    with pytest.raises(LLMUnavailable) as exc:
        run(router.complete(task="t", system="s", user="u", output=Out))
    assert exc.value.retry_after == 120


def test_router_skips_unconfigured_providers() -> None:
    router = LLMRouter(providers={}, chains={"t": [("groq", "m")]})
    with pytest.raises(LLMUnavailable, match="no LLM provider configured"):
        run(router.complete(task="t", system="s", user="u", output=Out))


# --- Search ------------------------------------------------------------------------------------

def test_tavily_and_brave_parsing() -> None:
    tav = TavilySearch(api_key="t", client=client(lambda r: httpx.Response(200, json={"results": [
        {"url": "https://a.example/x", "title": "A", "content": "snippet", "published_date": "2026-01-01"}]})))
    brave = BraveSearch(api_key="b", client=client(lambda r: httpx.Response(200, json={"web": {"results": [
        {"url": "https://b.example/y", "title": "B", "description": "desc", "page_age": "2026-02-02"}]}})))
    assert run(tav.search("q", max_results=5))[0].url == "https://a.example/x"
    assert run(brave.search("q", max_results=5))[0].snippet == "desc"


def test_search_router_falls_back_when_tavily_credits_run_out() -> None:
    tav = TavilySearch(api_key="t", client=client(lambda r: httpx.Response(432)))
    brave = BraveSearch(api_key="b", client=client(lambda r: httpx.Response(200, json={"web": {"results": [
        {"url": "https://b.example/y", "title": "B", "description": "d"}]}})))
    router = SearchRouter([tav, brave], cache_seconds=0)
    assert run(router.search("q"))[0].provider == "brave"
    with pytest.raises(SearchUnavailable, match="no search provider"):
        run(SearchRouter([]).search("q"))


# --- URLs / SSRF -------------------------------------------------------------------------------

def test_normalize_url() -> None:
    assert normalize_url("HTTPS://WWW.Acme.com:443/News/?utm_source=x&id=3#top") == "https://www.acme.com/News?id=3"
    assert normalize_url("http://acme.com") == "http://acme.com/"


@pytest.mark.parametrize("url", ["ftp://acme.com/", "http://user:pw@acme.com/", "http://acme.com:8080/",
                                 "https://www.linkedin.com/in/jane", "https://m.facebook.com/acme"])
def test_url_shape_rejections(url: str) -> None:
    with pytest.raises(BlockedURL):
        check_url_shape(url)


@pytest.mark.parametrize("ip,public", [("8.8.8.8", True), ("10.0.0.5", False), ("127.0.0.1", False),
                                       ("169.254.169.254", False), ("::1", False), ("::ffff:10.0.0.1", False),
                                       ("192.168.1.1", False), ("100.64.0.1", False), ("2606:4700::1111", True)])
def test_is_public_ip(ip: str, public: bool) -> None:
    assert is_public_ip(ip) is public


def test_resolve_rejects_private_literal_and_mixed_records(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(BlockedURL):
        run(resolve_public("169.254.169.254", 80))

    async def fake_getaddrinfo(host, port, type=0):  # type: ignore[no-untyped-def]
        return [(2, 1, 6, "", ("93.184.216.34", port)), (2, 1, 6, "", ("10.0.0.7", port))]

    async def go() -> None:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
        await resolve_public("rebind.example", 443)

    with pytest.raises(BlockedURL, match="non-public"):
        run(go())


# --- Fetcher -----------------------------------------------------------------------------------

async def allow_all(host: str, port: int) -> list[str]:
    if host.endswith(".internal"):
        raise BlockedURL(f"{host} resolves to a non-public address")
    return ["93.184.216.34"]


HTML = """<html><head><title>Acme Health | News</title></head><body><nav>Menu Home About</nav>
<article><h1>Acme opens new cardiac centre</h1><p>Acme Health today announced a 120-bed cardiac centre in Pune,
opening in March 2026.</p><p>The investment totals INR 300 crore.</p>
<a href="/about-us">About us</a><a href="https://other.example/x">Partner</a></article></body></html>"""


def make_fetcher(routes: dict[str, httpx.Response]) -> WebFetcher:
    def handler(req: httpx.Request) -> httpx.Response:
        return routes.get(str(req.url), httpx.Response(404))
    return WebFetcher(user_agent="TestBot", timeout=5, max_bytes=50_000, per_host_delay=0,
                      client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolve=allow_all,
                      check_peer=False)


def test_fetch_ok_and_extract() -> None:
    f = make_fetcher({"https://acme.example/robots.txt": httpx.Response(404),
                      "https://acme.example/news": httpx.Response(
                          200, text=HTML, headers={"content-type": "text/html; charset=utf-8"})})
    result = run(f.fetch("https://acme.example/news"))
    assert result.ok
    page = extract_page(result.body or "", result.final_url)
    assert "120-bed cardiac centre" in page.text and "Menu Home" not in page.text
    assert page.title and "Acme" in page.title
    assert ("https://acme.example/about-us", "About us") in page.links


def test_fetch_respects_robots() -> None:
    f = make_fetcher({"https://acme.example/robots.txt": httpx.Response(
        200, text="User-agent: *\nDisallow: /private", headers={"content-type": "text/plain"}),
        "https://acme.example/private/x": httpx.Response(200, text=HTML, headers={"content-type": "text/html"})})
    assert run(f.fetch("https://acme.example/private/x")).error == "disallowed by robots.txt"


def test_redirect_to_internal_host_is_blocked() -> None:
    f = make_fetcher({"https://acme.example/robots.txt": httpx.Response(404),
                      "https://acme.example/go": httpx.Response(
                          302, headers={"location": "http://metadata.internal/"})})
    result = run(f.fetch("https://acme.example/go"))
    assert not result.ok and "blocked" in (result.error or "")


def test_non_html_and_oversized_handling() -> None:
    f = make_fetcher({"https://acme.example/robots.txt": httpx.Response(404),
                      "https://acme.example/file.pdf": httpx.Response(200, content=b"%PDF",
                                                                      headers={"content-type": "application/pdf"}),
                      "https://acme.example/big": httpx.Response(200, text="<p>" + "x" * 200_000 + "</p>",
                                                                 headers={"content-type": "text/html"})})
    assert not run(f.fetch("https://acme.example/file.pdf")).ok
    big = run(f.fetch("https://acme.example/big"))
    assert big.ok and len(big.body or "") <= 50_000


def test_fetch_refuses_blocked_domains_without_network() -> None:
    f = make_fetcher({})
    assert "do-not-fetch" in (run(f.fetch("https://www.linkedin.com/in/someone")).error or "")
