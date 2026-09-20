"""The chat plugin's core: handles, streams, the three operations and the
non-streamed call built on them.

One handle is one answer: OPEN starts the model stream, READ takes the
chunks that have arrived, CLOSE releases the handle and aborts the stream.
The RFC handlers run on Curie's registration threads; the stream runs as a
task on the asyncio loop through the shared LLMClient (or, with
CHAT_STREAM_THREADS=1, in a thread of its own with a client of its own).
The queue between them is a deque under a condition variable, so a READ
can look at the head without taking it: an error marker stays in place
until it is the first thing a READ sees, which is how "buffered chunks
first, then SERVICE_ERROR" works.

Nothing here imports the RFC SDK; tests drive it with a fake LLM.
"""
from __future__ import annotations

import asyncio
import collections
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field

from curie import config
from curie.chat.protocol import Chunk, LineAssembler, WordChunker

log = logging.getLogger(__name__)

_current: "ChatService | None" = None


def current() -> "ChatService | None":
    """The service of this process, or None when the plugin is off."""
    return _current


class ChatError(Exception):
    """Raised by the operations; key is an ABAP exception name."""

    def __init__(self, key: str, message: str = ""):
        super().__init__(message or key)
        self.key = key
        self.message = message[:50]


class _Error:
    """Queue marker: the stream failed after everything before it."""

    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text


@dataclass
class Handle:
    id: int
    caller: str
    model: str
    prompt_chars: int
    created: float = field(default_factory=time.time)
    last_read: float = field(default_factory=time.time)
    queue: collections.deque = field(default_factory=collections.deque)
    cancel: threading.Event = field(default_factory=threading.Event)
    finished: bool = False       # the done sentinel has been delivered
    finish: str = ""             # stop | length | content_filter | error
    error: str = ""
    answer_chars: int = 0
    chunks: int = 0
    future: object = None        # concurrent future of the pump (asyncio mode)
    thread: threading.Thread | None = None

    @property
    def tokens(self) -> int:
        """Rough token count (four characters each) when the server sends no usage."""
        return (self.prompt_chars + self.answer_chars) // 4


class ChatService:
    def __init__(self, llm, loop: asyncio.AbstractEventLoop | None = None):
        global _current
        self.llm = llm
        self.loop = loop
        # Thread mode builds one client per answer; tests substitute a fake.
        self.llm_factory = None
        self._handles: dict[int, Handle] = {}
        self._ids = itertools.count(1)
        self._cond = threading.Condition()
        self._stopping = False
        self._monitor_task = None
        self.counters = {"opened": 0, "completed": 0, "failed": 0, "expired": 0,
                         "refused": 0, "sync_calls": 0}
        _current = self

    # -- configuration read at call time (tests monkeypatch config) ------------ #

    @staticmethod
    def models() -> dict[str, str]:
        """Logical model name -> model name; the first entry is the default."""
        return config.chat_models()

    def resolve_model(self, logical: str) -> tuple[str, str]:
        """(logical, real) for a logical name; blank means the default."""
        models = self.models()
        if not models:
            raise ChatError("MODEL_NOT_AVAILABLE", "no model configured")
        name = (logical or "").strip().upper()
        if not name:
            first = next(iter(models))
            return first, models[first]
        if name not in models:
            raise ChatError("MODEL_NOT_AVAILABLE", f"unknown model {name[:30]}")
        return name, models[name]

    @staticmethod
    def system_prompt(caller_attrs: dict | None = None) -> str:
        text = config.chat_system_prompt()
        attrs = caller_attrs or {}
        return (text.replace("{sysid}", str(attrs.get("sysId", "?")))
                    .replace("{client}", str(attrs.get("client", "?")))
                    .replace("{language}", config.LLM_ANSWER_LANGUAGE))

    # -- Z_BLA_RFC_CHAT_OPEN ---------------------------------------------------- #

    def prepare(self, messages: list[dict], caller_attrs: dict | None = None) -> list[dict]:
        """Validate and complete a history: roles, system prompt, size."""
        msgs = [dict(m) for m in messages if m.get("role") in ("user", "assistant", "system")]
        if not any(m["role"] == "user" for m in msgs):
            raise ChatError("NO_USER_MESSAGE")
        if msgs[-1]["role"] != "user":
            raise ChatError("NO_USER_MESSAGE", "last message is not a user message")
        prompt = self.system_prompt(caller_attrs)
        if prompt and msgs[0]["role"] != "system":
            msgs.insert(0, {"role": "system", "content": prompt})
        limit = config.CHAT_MAX_INPUT_CHARS
        size = sum(len(m["content"]) for m in msgs)
        # Drop the oldest non-system messages; keep the last (user) one.
        while size > limit and len(msgs) > 2:
            index = 1 if msgs[0]["role"] == "system" else 0
            if index >= len(msgs) - 1:
                break
            size -= len(msgs[index]["content"])
            del msgs[index]
        return msgs

    def open(self, messages: list[dict], model: str = "", caller: str = "",
             caller_attrs: dict | None = None) -> int:
        msgs = self.prepare(messages, caller_attrs)
        logical, real = self.resolve_model(model)
        with self._cond:
            if self._stopping:
                raise ChatError("SERVICE_ERROR", "server shutting down")
            if len(self._handles) >= config.CHAT_MAX_HANDLES:
                self.counters["refused"] += 1
                raise ChatError("SERVICE_ERROR", "busy: too many open answers")
            handle = Handle(id=next(self._ids), caller=caller, model=logical,
                            prompt_chars=sum(len(m["content"]) for m in msgs))
            self._handles[handle.id] = handle
            self.counters["opened"] += 1
        self._start(handle, msgs, real)
        log.info("chat open #%d for %s: %d message(s), %d chars, model %s",
                 handle.id, caller, len(msgs), handle.prompt_chars, logical)
        return handle.id

    def _start(self, handle: Handle, messages: list[dict], model: str) -> None:
        if config.CHAT_STREAM_THREADS or self.loop is None:
            # A loop of its own: the shared LLMClient's HTTP client is bound
            # to Curie's loop, so this thread gets a client of its own.
            def run() -> None:
                if self.llm_factory is not None:
                    client = self.llm_factory()
                else:
                    from curie.llm.client import LLMClient
                    client = LLMClient()
                asyncio.run(self._pump(handle, messages, model, client))

            handle.thread = threading.Thread(target=run, name=f"chat-{handle.id}", daemon=True)
            handle.thread.start()
        else:
            handle.future = asyncio.run_coroutine_threadsafe(
                self._pump(handle, messages, model, self.llm), self.loop)

    async def _pump(self, handle: Handle, messages: list[dict], model: str, llm) -> None:
        chunker = WordChunker()
        finish = None
        try:
            async for kind, data in llm.stream(messages, model=model,
                                               max_tokens=config.CHAT_MAX_TOKENS):
                if handle.cancel.is_set():
                    break
                if kind == "delta":
                    handle.answer_chars += len(data)
                    self._put(handle, chunker.feed(data))
                elif kind == "done":
                    finish = (data or {}).get("finish_reason")
            self._put(handle, chunker.flush())
            if handle.cancel.is_set():
                self._fail(handle, "cancelled", count=False)
                return
            if finish in ("length", "content_filter"):
                self._put(handle, [Chunk("[truncated]")])
            handle.finish = finish or "stop"
            self._put(handle, [Chunk(done=True)])
            self.counters["completed"] += 1
        except asyncio.CancelledError:
            self._put(handle, chunker.flush())
            cancelled = handle.cancel.is_set()
            self._fail(handle, "cancelled" if cancelled else "server shutting down",
                       count=not cancelled)
            raise
        except Exception as exc:  # noqa: BLE001 -- reported to the reader
            self._put(handle, chunker.flush())
            text = f"{type(exc).__name__}: {exc}"
            log.warning("chat #%d stream failed: %s", handle.id, text[:200])
            self._fail(handle, text)

    def _put(self, handle: Handle, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        with self._cond:
            handle.queue.extend(chunks)
            handle.chunks += len(chunks)
            self._cond.notify_all()

    def _fail(self, handle: Handle, text: str, count: bool = True) -> None:
        with self._cond:
            handle.error = text[:200]
            handle.finish = "error"
            handle.queue.append(_Error(handle.error))
            if count:
                self.counters["failed"] += 1
            self._cond.notify_all()

    # -- Z_BLA_RFC_CHAT_READ ---------------------------------------------------- #

    def read(self, handle_id: int, max_chunks: int = 1,
             wait: float | None = None) -> tuple[list[Chunk], bool]:
        """Chunks that arrived, and whether the answer is complete.

        Waits up to `wait` seconds (CHAT_READ_WAIT) for the first chunk,
        then takes what is there without waiting, up to max_chunks. Raises
        SERVICE_ERROR only when the failure marker is the first item left.
        """
        max_chunks = max(1, int(max_chunks or 1))
        wait = config.CHAT_READ_WAIT if wait is None else wait
        deadline = time.monotonic() + wait
        with self._cond:
            handle = self._handles.get(int(handle_id))
            if handle is None:
                raise ChatError("INVALID_HANDLE")
            handle.last_read = time.time()
            if handle.finished:
                return [], True
            while not handle.queue and not self._stopping:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(min(1.0, remaining))
            chunks: list[Chunk] = []
            while handle.queue and len(chunks) < max_chunks:
                item = handle.queue[0]
                if isinstance(item, _Error):
                    break
                handle.queue.popleft()
                chunks.append(item)
                if item.done:
                    handle.finished = True
                    break
            if not chunks and handle.queue and isinstance(handle.queue[0], _Error):
                raise ChatError("SERVICE_ERROR", handle.queue[0].text)
            if not chunks and self._stopping:
                raise ChatError("SERVICE_ERROR", "server shutting down")
            return chunks, handle.finished

    # -- Z_BLA_RFC_CHAT_CLOSE --------------------------------------------------- #

    def close(self, handle_id: int) -> None:
        with self._cond:
            handle = self._handles.pop(int(handle_id), None) if handle_id else None
        if handle is None:
            return
        self._cancel(handle)
        log.info("chat close #%d: %d chunk(s), %s", handle.id, handle.chunks,
                 handle.finish or "unfinished")

    @staticmethod
    def _cancel(handle: Handle) -> None:
        handle.cancel.set()
        if handle.future is not None:
            handle.future.cancel()

    # -- Z_AI_CHAT_COMPLETION --------------------------------------------------- #

    def complete(self, messages: list[dict], model: str = "", caller: str = "",
                 caller_attrs: dict | None = None, timeout: float | None = None,
                 width: int = 132) -> dict:
        """The streamed path driven to the end inside one call.

        Returns {"lines": [Line], "finish": str, "error": str, "tokens": int,
        "model": logical}. finish is stop, length, content_filter, timeout
        or error; on timeout or error the lines produced so far are kept.
        Raises NO_USER_MESSAGE, MODEL_NOT_AVAILABLE, SERVICE_ERROR (only
        when nothing at all was produced).
        """
        timeout = config.CHAT_SYNC_TIMEOUT if timeout is None else timeout
        self.counters["sync_calls"] += 1
        handle_id = self.open(messages, model, caller, caller_attrs)
        handle = self._handles[handle_id]
        assembler = LineAssembler(width)
        deadline = time.monotonic() + timeout
        finish, error, produced = "", "", 0
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    finish = "timeout"
                    error = f"no answer within {int(timeout)} s"
                    break
                try:
                    chunks, done = self.read(handle_id, 1000, wait=min(remaining, config.CHAT_READ_WAIT))
                except ChatError as exc:
                    finish, error = "error", exc.message
                    if not produced:
                        raise
                    break
                for chunk in chunks:
                    assembler.feed(chunk)
                produced += len(chunks)
                if done:
                    finish = handle.finish or "stop"
                    break
        finally:
            self.close(handle_id)
        if finish in ("timeout", "error"):
            if not produced:
                raise ChatError("SERVICE_ERROR", error)
            if assembler.current:
                assembler.feed(Chunk("", newline=True))
            assembler.feed(Chunk(f"[{finish}]", newline=True))
        return {"lines": assembler.finish(), "finish": finish, "error": error,
                "tokens": handle.tokens, "model": handle.model}

    # -- housekeeping ----------------------------------------------------------- #

    def expire(self) -> int:
        limit = time.time() - config.CHAT_HANDLE_TTL
        with self._cond:
            stale = [h for h in self._handles.values() if h.last_read < limit]
            for handle in stale:
                del self._handles[handle.id]
                self.counters["expired"] += 1
        for handle in stale:
            self._cancel(handle)
            log.info("chat expire #%d (%s): no READ for %ds", handle.id, handle.caller,
                     int(config.CHAT_HANDLE_TTL))
        return len(stale)

    def start_monitor(self) -> None:
        self._monitor_task = asyncio.ensure_future(self._monitor())

    async def _monitor(self) -> None:
        while True:
            try:
                await asyncio.sleep(30.0)
                self.expire()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                log.exception("chat monitor")

    def shutdown(self) -> None:
        """Abort every stream and wake every waiting READ."""
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            self._monitor_task = None
        with self._cond:
            self._stopping = True
            handles = list(self._handles.values())
            self._cond.notify_all()
        for handle in handles:
            self._cancel(handle)

    def stats(self) -> dict:
        with self._cond:
            open_handles = [{"id": h.id, "caller": h.caller, "model": h.model,
                             "chunks": h.chunks, "age": int(time.time() - h.created)}
                            for h in self._handles.values()]
        return {"open": open_handles, **self.counters,
                "models": list(self.models()), "max_handles": config.CHAT_MAX_HANDLES}
