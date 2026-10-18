from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.middleware import OriginCheckMiddleware, SecurityHeadersMiddleware
from app.core.errors import install_error_handlers
from app.core.request_context import RequestContextMiddleware


def _app() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(OriginCheckMiddleware, allowed_origins=["http://localhost:3000"])
    app.add_middleware(RequestContextMiddleware)

    @app.post("/thing")
    def thing() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internal detail")

    return TestClient(app, raise_server_exceptions=False)


def test_same_origin_post_allowed() -> None:
    assert _app().post("/thing", headers={"Origin": "http://localhost:3000"}).status_code == 200


def test_cross_origin_post_rejected() -> None:
    r = _app().post("/thing", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "origin_rejected"


def test_cross_site_fetch_without_origin_rejected() -> None:
    assert _app().post("/thing", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_non_browser_post_without_origin_allowed() -> None:
    assert _app().post("/thing").status_code == 200


def test_security_headers_and_request_id() -> None:
    r = _app().post("/thing")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert len(r.headers["x-request-id"]) == 32


def test_valid_incoming_request_id_is_propagated_and_invalid_replaced() -> None:
    client = _app()
    assert client.post("/thing", headers={"X-Request-ID": "abc12345-req"}).headers["x-request-id"] == \
        "abc12345-req"
    assert client.post("/thing", headers={"X-Request-ID": "bad id\n"}).headers["x-request-id"] != "bad id\n"


def test_unhandled_errors_do_not_leak_details() -> None:
    r = _app().get("/boom")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "internal_error"
    assert "secret internal detail" not in r.text
    assert body["error"]["request_id"] == r.headers["x-request-id"]
