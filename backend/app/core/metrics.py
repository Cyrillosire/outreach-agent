"""Prometheus metrics. Labels use route templates (never raw paths) to keep cardinality bounded."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram

registry = CollectorRegistry(auto_describe=True)

http_requests_total = Counter(
    "http_requests_total", "HTTP requests", ["method", "route", "status"], registry=registry
)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=registry,
)
auth_events_total = Counter(
    "auth_events_total", "Authentication outcomes", ["event", "outcome"], registry=registry
)


def observe_http_request(method: str, route: str, status: int, seconds: float) -> None:
    http_requests_total.labels(method, route, str(status)).inc()
    http_request_duration_seconds.labels(method, route).observe(seconds)
