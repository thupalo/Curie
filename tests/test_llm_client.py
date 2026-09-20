"""LLMClient against a scripted SDK: tool-mode step-down on the server's 400s."""
import asyncio

import httpx
import openai

from curie import config
from curie.llm.client import LLMClient, LLMError


def bad_request(message: str) -> openai.BadRequestError:
    response = httpx.Response(400, request=httpx.Request("POST", "http://llm/v1/chat/completions"))
    return openai.BadRequestError(message, response=response, body={"error": {"message": message}})


class Choice:
    def __init__(self, content):
        self.message = type("M", (), {"content": content, "tool_calls": None})()
        self.finish_reason = "stop"


class Completions:
    """Records every request; rejects by rule until the client steps down."""

    def __init__(self, reject):
        self.reject = reject          # callable(kwargs) -> error message or None
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        message = self.reject(kwargs)
        if message:
            raise bad_request(message)
        return type("R", (), {"choices": [Choice("answer")]})()


def client_with(reject):
    llm = LLMClient(base_url="http://llm/v1", api_key="x", model="m")
    completions = Completions(reject)
    llm._client = type("SDK", (), {"chat": type("C", (), {"completions": completions})()})()
    return llm, completions


async def collect(llm, tools):
    return [e async for e in llm.stream([{"role": "user", "content": "hi"}], tools=tools)]


def test_steps_down_to_omit_when_auto_choice_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "LLM_STREAM", False)
    monkeypatch.setattr(config, "LLM_TOOL_CHOICE", "auto")
    tools = [{"type": "function", "function": {"name": "sap_read_table"}}]

    def reject(kwargs):
        if kwargs.get("tool_choice") == "auto":
            return '"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set'
        return None

    llm, sdk = client_with(reject)
    events = asyncio.run(collect(llm, tools))
    assert events[-1][0] == "done" and events[-1][1]["content"] == "answer"
    assert len(sdk.requests) == 2 and "tool_choice" not in sdk.requests[1] and "tools" in sdk.requests[1]
    assert llm.tool_mode == "omit" and llm.tools_enabled
    # the next call does not probe again
    asyncio.run(collect(llm, tools))
    assert len(sdk.requests) == 3 and "tool_choice" not in sdk.requests[2]


def test_switches_tools_off_when_the_server_refuses_them(monkeypatch):
    monkeypatch.setattr(config, "LLM_STREAM", False)
    monkeypatch.setattr(config, "LLM_TOOL_CHOICE", "auto")
    tools = [{"type": "function", "function": {"name": "sap_read_table"}}]

    def reject(kwargs):
        if kwargs.get("tool_choice") == "auto":
            return '"auto" tool choice requires --enable-auto-tool-choice'
        if "tools" in kwargs:
            return "tools are not supported by this model"
        return None

    llm, sdk = client_with(reject)
    events = asyncio.run(collect(llm, tools))
    assert events[-1][1]["content"] == "answer" and llm.tool_mode == "off" and not llm.tools_enabled
    assert [("tools" in r, r.get("tool_choice")) for r in sdk.requests] == [
        (True, "auto"), (True, None), (False, None)]


def test_unrelated_400_is_an_error(monkeypatch):
    monkeypatch.setattr(config, "LLM_STREAM", False)
    monkeypatch.setattr(config, "LLM_TOOL_CHOICE", "auto")
    llm, sdk = client_with(lambda kwargs: "model 'm' does not exist")
    try:
        asyncio.run(collect(llm, [{"type": "function", "function": {"name": "x"}}]))
        assert False, "expected LLMError"
    except LLMError as exc:
        assert "does not exist" in str(exc)
    assert len(sdk.requests) == 1 and llm.tool_mode == "auto"


class Models:
    def __init__(self, ids):
        self.ids = ids

    async def list(self):
        for i in self.ids:
            yield type("Model", (), {"id": i})()


def test_check_lists_served_models_when_the_model_is_unknown(monkeypatch):
    monkeypatch.setattr(config, "LLM_STREAM", False)
    llm, sdk = client_with(lambda kwargs: "The model `deepseek-ai/X` does not exist.")
    llm._client.models = Models(["deepseek-v4-flash", "qwen3"])
    result = asyncio.run(llm.check())
    assert result["ok"] is False and "does not exist" in result["error"]
    assert result["served"] == ["deepseek-v4-flash", "qwen3"] and llm.last_check is result
    assert llm.hint("NotFoundError: does not exist").startswith(" This endpoint serves: deepseek-v4-flash, qwen3.")
    assert llm.hint("RateLimitError") == ""
    # a healthy model: no listing needed
    ok_llm, _ = client_with(lambda kwargs: None)
    result = asyncio.run(ok_llm.check())
    assert result["ok"] and result["reply"] == "answer" and result["served"] == []
    # a server without /models: the check still reports, served stays empty
    class NoModels:
        async def list(self):
            raise RuntimeError("404")
            yield  # pragma: no cover
    llm2, _ = client_with(lambda kwargs: "model does not exist")
    llm2._client.models = NoModels()
    assert asyncio.run(llm2.check())["served"] == []


def test_complete_falls_back_to_reasoning_content_when_content_is_empty():
    """A reasoning model behind a reasoning parser can answer with an empty
    content and the text in reasoning_content (seen as an empty verdict)."""
    llm = LLMClient(base_url="http://llm/v1", api_key="x", model="m")

    class Message:
        content = ""
        reasoning_content = 'thinking... {"verdict": "ok"}'
        tool_calls = None

    class Completions:
        async def create(self, **kwargs):
            return type("R", (), {"choices": [type("Ch", (), {"message": Message(), "finish_reason": "stop"})()]})()

    llm._client = type("SDK", (), {"chat": type("C", (), {"completions": Completions()})()})()
    text = asyncio.run(llm.complete([{"role": "user", "content": "x"}], json_mode=True))
    assert '{"verdict": "ok"}' in text
