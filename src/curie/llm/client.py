"""OpenAI-compatible chat client with streaming and tool calls.

Works with OpenAI, Azure (through base_url), vLLM, Ollama, LM Studio and
the like. stream() yields a small event vocabulary so that the orchestrator
does not depend on the SDK's types, and tests can substitute a fake:

    ("delta", "text")                       streamed content
    ("tool_calls", [{"id", "name", "arguments"}])
    ("done", {"content", "finish_reason"})

Cancelling the task that iterates stream() closes the HTTP stream.
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from curie import config

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: float | None = None):
        self.base_url = base_url or config.LLM_BASE_URL
        self.api_key = api_key or config.LLM_API_KEY
        self.model = model or config.LLM_MODEL
        self.timeout = timeout or config.LLM_TIMEOUT
        self._client = None
        # How tools are offered to this server: "auto" sends tools with
        # tool_choice=auto, "omit" sends tools without the hint (vLLM without
        # --enable-auto-tool-choice rejects the hint), "off" sends no tools
        # at all (the server refuses them). Starts from LLM_TOOL_CHOICE and
        # steps down on the server's 400s, once per process.
        self.tool_mode = config.LLM_TOOL_CHOICE
        self._served: list[str] | None = None   # served model names, once asked
        self.last_check: dict | None = None
        self.stream_usage = config.LLM_STREAM_USAGE   # off after a server rejects stream_options

    @property
    def tools_enabled(self) -> bool:
        return self.tool_mode != "off"

    def _tool_kwargs(self, tools: list[dict] | None) -> dict:
        if not tools or self.tool_mode == "off":
            return {}
        out = {"tools": tools}
        if self.tool_mode == "auto":
            out["tool_choice"] = "auto"
        return out

    def _step_down_tools(self, exc: Exception) -> bool:
        """After a 400 about tools: pick the next tool mode; False if unrelated."""
        text = str(exc).lower()
        if self.tool_mode == "auto" and "tool" in text and "choice" in text:
            self.tool_mode = "omit"
            log.warning("model server rejects tool_choice=auto (%s); sending tools without the "
                        "hint from now on. For vLLM start it with --enable-auto-tool-choice "
                        "--tool-call-parser <parser>.", str(exc)[:160])
            return True
        if self.tool_mode in ("auto", "omit") and "tool" in text:
            self.tool_mode = "off"
            log.warning("model server rejects tools (%s); Curie continues without SAP tools "
                        "until restart. Set LLM_TOOL_CHOICE=off to skip this probe.", str(exc)[:160])
            return True
        return False

    def _sdk(self):
        if self._client is None:
            from openai import AsyncOpenAI  # imported here: tests use a fake client

            self._client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key,
                                       timeout=self.timeout, max_retries=1)
        return self._client

    async def stream(self, messages: list[dict], tools: list[dict] | None = None,
                     temperature: float | None = None, max_tokens: int | None = None,
                     model: str | None = None) -> AsyncIterator[tuple]:
        from openai import APIError

        base = {
            "model": model or self.model,
            "messages": messages,
            "temperature": config.LLM_TEMPERATURE if temperature is None else temperature,
            "max_tokens": max_tokens or config.LLM_MAX_OUTPUT_TOKENS,
        }
        for _attempt in range(4):          # auto -> omit -> off, plus one usage retry
            kwargs = {**base, **self._tool_kwargs(tools)}
            yielded = False
            try:
                source = self._stream(kwargs) if config.LLM_STREAM else self._complete_as_stream(kwargs)
                async for event in source:
                    yielded = True
                    yield event
                return
            except APIError as exc:
                # A 400 about tools or stream_options arrives before any
                # token; step down and ask again. Anything else, or a failure
                # mid-stream, is final.
                status = getattr(exc, "status_code", None)
                if not yielded and status == 400 and self.stream_usage and "stream_options" in str(exc):
                    self.stream_usage = False
                    log.info("model server rejects stream_options; token usage is not recorded")
                    continue
                if yielded or status != 400 or not tools or not self._step_down_tools(exc):
                    raise LLMError(f"{type(exc).__name__}: {exc}") from exc

    async def _stream(self, kwargs: dict) -> AsyncIterator[tuple]:
        if self.stream_usage:
            kwargs = {**kwargs, "stream_options": {"include_usage": True}}
        stream = await self._sdk().chat.completions.create(stream=True, **kwargs)
        content: list[str] = []
        calls: dict[int, dict] = {}
        finish = None
        usage: dict = {}
        async with stream:
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = _usage(chunk.usage)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if delta is not None and delta.content:
                    content.append(delta.content)
                    yield ("delta", delta.content)
                if delta is not None and delta.tool_calls:
                    for tc in delta.tool_calls:
                        acc = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            acc["id"] = tc.id
                        if tc.function is not None:
                            if tc.function.name:
                                acc["name"] += tc.function.name
                            if tc.function.arguments:
                                acc["arguments"] += tc.function.arguments
                if choice.finish_reason:
                    finish = choice.finish_reason
        if calls:
            yield ("tool_calls", [calls[i] for i in sorted(calls)])
        yield ("done", {"content": "".join(content), "finish_reason": finish, "usage": usage})

    async def _complete_as_stream(self, kwargs: dict) -> AsyncIterator[tuple]:
        resp = await self._sdk().chat.completions.create(stream=False, **kwargs)
        msg = resp.choices[0].message
        if msg.content:
            yield ("delta", msg.content)
        if msg.tool_calls:
            yield ("tool_calls", [
                {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments or ""}
                for tc in msg.tool_calls
            ])
        yield ("done", {"content": msg.content or "", "finish_reason": resp.choices[0].finish_reason,
                        "usage": _usage(getattr(resp, "usage", None))})

    async def complete(self, messages: list[dict], model: str | None = None,
                       temperature: float = 0.0, json_mode: bool = False,
                       max_tokens: int | None = None) -> str:
        """One non-streamed answer; json_mode asks for a JSON object when supported."""
        from openai import APIError

        kwargs = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or config.LLM_MAX_OUTPUT_TOKENS,
        }
        try:
            if json_mode:
                try:
                    resp = await self._sdk().chat.completions.create(
                        response_format={"type": "json_object"}, **kwargs)
                    text = _message_text(resp)
                    if text:
                        return text
                    log.debug("json_object answer empty (finish %s); retrying plain",
                              resp.choices[0].finish_reason if resp.choices else "?")
                except APIError as exc:
                    log.debug("json_object response_format refused (%s), retrying plain", exc)
            resp = await self._sdk().chat.completions.create(**kwargs)
            return _message_text(resp)
        except APIError as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc

    async def ping(self) -> str:
        """A one-token round trip for --check."""
        # Reasoning models spend tokens on hidden thinking first; give them room.
        return await self.complete([{"role": "user", "content": "Reply with OK."}],
                                   max_tokens=256)

    async def served_models(self) -> list[str]:
        """The model names the endpoint answers to (GET /models), cached."""
        if self._served is None:
            try:
                self._served = [m.id async for m in self._sdk().models.list()]
            except Exception as exc:  # noqa: BLE001 -- not every server lists models
                log.debug("models.list failed: %s", exc)
                self._served = []
        return list(self._served)

    @staticmethod
    def is_model_not_found(error: Exception | str) -> bool:
        text = str(error)
        return "NotFound" in text or "does not exist" in text or "model_not_found" in text

    async def check(self) -> dict:
        """Connection and model check for startup, --check and /healthz.

        {"ok": bool, "model": str, "base_url": str, "reply": str, "error": str,
         "served": [names]}: served is filled when the model is not found, so
        the operator sees what to put into LLM_MODEL."""
        result = {"ok": False, "model": self.model, "base_url": self.base_url, "reply": "",
                  "error": "", "served": []}
        try:
            result["reply"] = (await self.ping()).strip()[:40]
            result["ok"] = True
        except LLMError as exc:
            result["error"] = str(exc)
            if self.is_model_not_found(exc):
                result["served"] = await self.served_models()
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"
        self.last_check = result
        return result

    def hint(self, error: Exception | str) -> str:
        """What to tell a person next to a model error, when we know more."""
        if self.is_model_not_found(error) and self._served:
            return (f" This endpoint serves: {', '.join(self._served)}. Set LLM_MODEL in .env to "
                    "one of them and restart Curie.")
        return ""


def _message_text(resp) -> str:
    """The answer text of a non-streamed response. A reasoning model served
    with a reasoning parser can put everything into reasoning_content and
    leave content empty (seen with an empty JSON verdict); the reasoning
    text is then the best there is, and parse_json_object finds the object
    in it when one exists."""
    if not resp.choices:
        return ""
    msg = resp.choices[0].message
    text = msg.content or ""
    if text.strip():
        return text
    reasoning = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None) or ""
    if reasoning and isinstance(reasoning, str):
        log.debug("answer content empty; using reasoning_content (%d chars)", len(reasoning))
        return reasoning
    return text


def _usage(obj) -> dict:
    """{"tokens_in", "tokens_out"} from an SDK usage object, or {}."""
    if obj is None:
        return {}
    tokens_in = getattr(obj, "prompt_tokens", None)
    tokens_out = getattr(obj, "completion_tokens", None)
    if tokens_in is None and tokens_out is None:
        return {}
    return {"tokens_in": int(tokens_in or 0), "tokens_out": int(tokens_out or 0)}


def parse_json_object(text: str) -> dict | None:
    """The first JSON object in a model answer, tolerant of fences and prose."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None
