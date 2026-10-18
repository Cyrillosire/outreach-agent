"""Local models via Ollama. Private and unlimited, but slow on CPU-only machines (minutes per extraction)."""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.integrations import http
from app.integrations.llm.base import LLMUsage


class OllamaProvider:
    name = "ollama"

    def __init__(self, *, base_url: str, timeout: float, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def complete_json(self, *, model: str, system: str, user: str, schema: dict[str, Any],
                            max_tokens: int) -> tuple[str, LLMUsage]:
        payload = {
            "model": model,
            "stream": False,
            "think": False,
            "format": schema,
            "keep_alive": "5m",
            "options": {"temperature": 0, "num_ctx": 4096, "num_predict": max_tokens},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        start = time.perf_counter()
        response = await http.request(self._client, "POST", "/api/chat", provider=self.name, json=payload,
                                      max_attempts=2)
        body = response.json()
        return (body.get("message") or {}).get("content") or "", LLMUsage(
            self.name, model, int(body.get("prompt_eval_count", 0)), int(body.get("eval_count", 0)),
            (time.perf_counter() - start) * 1000)
