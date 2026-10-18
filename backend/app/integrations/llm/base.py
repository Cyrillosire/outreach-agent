from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel


@dataclass(frozen=True)
class LLMUsage:
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float


class LLMProvider(Protocol):
    name: str

    async def complete_json(self, *, model: str, system: str, user: str, schema: dict[str, Any],
                            max_tokens: int) -> tuple[str, LLMUsage]:
        """Return raw JSON text constrained (best-effort) by `schema`. Raise http.Provider* errors."""
        ...


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema with $refs inlined and every object closed (additionalProperties: false).

    Some providers handle $defs poorly; an inlined, closed schema is accepted everywhere.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                target = node["$ref"].rsplit("/", 1)[-1]
                return resolve(copy.deepcopy(defs[target]))
            out = {k: resolve(v) for k, v in node.items() if k != "title"}
            if out.get("type") == "object":
                out.setdefault("additionalProperties", False)
            return out
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)  # type: ignore[no-any-return]


def strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()
