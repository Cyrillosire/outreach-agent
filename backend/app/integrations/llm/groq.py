"""Groq (OpenAI-compatible) chat completions with structured outputs.

Data handling: Groq's terms bar training on API inputs/outputs; enable Zero Data Retention in the Groq console.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.integrations import http
from app.integrations.llm.base import LLMUsage


class GroqProvider:
    name = "groq"

    def __init__(self, *, api_key: str, base_url: str, timeout: float, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._headers = {"Authorization": f"Bearer {api_key}"}

    @staticmethod
    def _reasoning_options(model: str) -> dict[str, Any]:
        # Keep reasoning minimal and out of the response: lower latency and token use within free quotas.
        if model.startswith("openai/gpt-oss"):
            return {"reasoning_effort": "low", "include_reasoning": False}
        if model.startswith("qwen/"):
            return {"reasoning_effort": "none", "reasoning_format": "hidden"}
        return {}

    async def complete_json(self, *, model: str, system: str, user: str, schema: dict[str, Any],
                            max_tokens: int) -> tuple[str, LLMUsage]:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "max_completion_tokens": max_tokens,
            "response_format": {"type": "json_schema", "json_schema": {"name": "result", "schema": schema,
                                                                       "strict": False}},
            **self._reasoning_options(model),
        }
        start = time.perf_counter()
        response = await http.request(self._client, "POST", "/chat/completions", provider=self.name,
                                      json=payload, headers=self._headers, max_inline_wait=15.0)
        body = response.json()
        usage = body.get("usage") or {}
        content = (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return content, LLMUsage(self.name, model, int(usage.get("prompt_tokens", 0)),
                                 int(usage.get("completion_tokens", 0)), (time.perf_counter() - start) * 1000)
