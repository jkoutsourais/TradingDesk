"""Ollama chat with structured output (native /api/chat).

Every call sends the target Pydantic model's JSON schema as `format`. A reply that does
not parse, or that the caller's check rejects (for example a number that did not come
from the fact table), gets one retry with the exact problems listed. A second failure is
returned as a failed result so the caller writes a `failed` artifact and the shift goes
on. Usage (tokens, load time, generation time) is summed over attempts for job metrics.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from desk.config import ChatModel
from desk.llm.prompts import Prompt

MAX_ATTEMPTS = 2
NS_PER_MS = 1_000_000
REQUEST_TIMEOUT = httpx.Timeout(600.0, connect=10.0)


@dataclass
class Usage:
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    load_ms: int = 0
    generation_ms: int = 0
    total_ms: int = 0

    def add(self, reply: dict[str, Any]) -> None:
        self.tokens_in += int(reply.get("prompt_eval_count") or 0)
        self.tokens_out += int(reply.get("eval_count") or 0)
        self.load_ms += int(reply.get("load_duration") or 0) // NS_PER_MS
        self.generation_ms += int(reply.get("eval_duration") or 0) // NS_PER_MS
        self.total_ms += int(reply.get("total_duration") or 0) // NS_PER_MS


@dataclass
class StructuredResult[T: BaseModel]:
    value: T | None
    usage: Usage
    attempts: int
    error: str | None = None
    problems: list[str] = field(default_factory=list)
    # The last reply that parsed but failed the checks, for callers that can keep its
    # valid parts.
    rejected: T | None = None

    @property
    def ok(self) -> bool:
        return self.value is not None


def request_failure(exc: httpx.HTTPError) -> str:
    """A readable reason for a failed request; httpx timeouts often carry an empty message."""
    detail = str(exc).strip()
    return f"model request failed: {type(exc).__name__}" + (f" ({detail})" if detail else "")


class OllamaChat:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=REQUEST_TIMEOUT, transport=transport
        )

    async def structured[T: BaseModel](
        self,
        model: ChatModel,
        prompt: Prompt,
        schema: type[T],
        check: Callable[[T], list[str]] | None = None,
    ) -> StructuredResult[T]:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user},
        ]
        usage = Usage(model=model.model)
        problems: list[str] = []
        rejected: T | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            body: dict[str, Any] = {
                "model": model.model,
                "messages": messages,
                "stream": False,
                "format": schema.model_json_schema(),
                "options": {"num_ctx": model.num_ctx, "temperature": model.temperature},
                "keep_alive": model.keep_alive,
                "think": model.think,
            }
            response = await self._http.post("/api/chat", json=body)
            response.raise_for_status()
            reply = response.json()
            usage.add(reply)
            content = reply.get("message", {}).get("content", "")
            try:
                value = schema.model_validate_json(content)
            except ValidationError as exc:
                problems = [f"reply did not match the schema: {exc.errors()[:5]}"]
            else:
                problems = check(value) if check else []
                if not problems:
                    return StructuredResult(value, usage, attempt)
                rejected = value
            if attempt < MAX_ATTEMPTS:
                messages += [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": _retry_instruction(problems)},
                ]
        return StructuredResult(
            None,
            usage,
            MAX_ATTEMPTS,
            error="; ".join(problems)[:1000],
            problems=problems,
            rejected=rejected,
        )

    async def unload(self, model: str) -> None:
        """Release a model from memory now (keep_alive 0)."""
        response = await self._http.post("/api/generate", json={"model": model, "keep_alive": 0})
        response.raise_for_status()

    async def aclose(self) -> None:
        await self._http.aclose()


def _retry_instruction(problems: list[str]) -> str:
    listed = "\n".join(f"- {problem}" for problem in problems[:20])
    return (
        "Your reply had these problems:\n"
        f"{listed}\n"
        "Return the corrected reply as JSON matching the same schema, following the "
        "rules in the system message."
    )
