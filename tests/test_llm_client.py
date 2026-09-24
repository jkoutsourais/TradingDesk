import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from desk.config import load_models
from desk.llm.client import OllamaChat
from desk.llm.prompts import PROMPTS_DIR, Prompt, load_prompt

MODELS = load_models()
PROMPT = Prompt(version="test.v1", system="You are a test.", user="Say hi.")


class Reply(BaseModel):
    text: str


def ollama(replies: list[str], seen: list[dict[str, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": replies.pop(0)},
                "prompt_eval_count": 100,
                "eval_count": 20,
                "load_duration": 2_000_000_000,
                "eval_duration": 1_000_000_000,
                "total_duration": 3_500_000_000,
            },
        )

    return httpx.MockTransport(handler)


def test_structured_success_sends_schema_and_role_options() -> None:
    seen: list[dict[str, Any]] = []
    chat = OllamaChat("http://ollama.test", transport=ollama(['{"text": "hi"}'], seen))
    result = asyncio.run(chat.structured(MODELS.small, PROMPT, Reply))
    assert result.ok and result.value == Reply(text="hi") and result.attempts == 1
    body = seen[0]
    assert body["model"] == MODELS.small.model
    assert body["format"]["properties"]["text"]["type"] == "string"
    assert body["keep_alive"] == "10m" and body["stream"] is False
    assert result.usage.tokens_in == 100 and result.usage.load_ms == 2000


def test_bad_json_retries_once_with_the_problem() -> None:
    seen: list[dict[str, Any]] = []
    chat = OllamaChat("http://ollama.test", transport=ollama(["not json", '{"text": "ok"}'], seen))
    result = asyncio.run(chat.structured(MODELS.deep, PROMPT, Reply))
    assert result.ok and result.attempts == 2
    retry_messages = seen[1]["messages"]
    assert retry_messages[-2] == {"role": "assistant", "content": "not json"}
    assert "did not match the schema" in retry_messages[-1]["content"]
    assert result.usage.tokens_out == 40  # summed across attempts


def test_check_rejection_then_second_failure_returns_failed() -> None:
    seen: list[dict[str, Any]] = []
    chat = OllamaChat(
        "http://ollama.test",
        transport=ollama(['{"text": "up 3%"}', '{"text": "up 3 percent"}'], seen),
    )

    def no_digits(reply: Reply) -> list[str]:
        return ["number '3' is not from a fact"] if any(c.isdigit() for c in reply.text) else []

    result = asyncio.run(chat.structured(MODELS.deep, PROMPT, Reply, check=no_digits))
    assert not result.ok and result.value is None and result.attempts == 2
    assert "not from a fact" in (result.error or "")
    assert "not from a fact" in seen[1]["messages"][-1]["content"]


def test_http_error_propagates() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    chat = OllamaChat("http://ollama.test", transport=transport)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(chat.structured(MODELS.small, PROMPT, Reply))


def test_load_prompt_keeps_fact_braces(tmp_path: Path) -> None:
    (tmp_path / "demo.md").write_text(
        "---\nversion: demo.v3\n---\n## system\nWrite {AEP.change_pct} as is.\n"
        "## user\nFacts:\n${facts}\n",
        encoding="utf-8",
    )
    prompt = load_prompt("demo", {"facts": "{AEP.change_pct} = -1.2%"}, prompts_dir=tmp_path)
    assert prompt.version == "demo.v3"
    assert "{AEP.change_pct}" in prompt.system
    assert prompt.user == "Facts:\n{AEP.change_pct} = -1.2%"


def test_every_shipped_prompt_loads() -> None:
    placeholder = re.compile(r"\$\{(\w+)\}")
    for path in sorted(PROMPTS_DIR.glob("*.md")):
        names = set(placeholder.findall(path.read_text(encoding="utf-8")))
        prompt = load_prompt(path.stem, dict.fromkeys(names, "x"))
        assert prompt.version.startswith(f"{path.stem}.v")
        assert "$" + "{" not in prompt.system + prompt.user
