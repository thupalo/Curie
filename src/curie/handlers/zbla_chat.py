"""The chat plugin's function modules: direct SAP -> model calls.

Installed only with CHAT_PLUGIN=1, and only when the modules exist in the
backend (they are optional handlers: a missing one disables the plugin
with a logged error instead of stopping Curie).

Interfaces (SE37, Remote-Enabled, in function group ZBLA; DDIC structures
ZBLA_RFC_MSG, ZBLA_RFC_CHUNK, ZBLA_RFC_CTL as in CHAT_INTERFACE.md):

    Z_BLA_RFC_CHAT_OPEN    T IT_MESSAGES ZBLA_RFC_MSG (MSGNO, ROLE, LINENO, TEXT)
                           -> EV_HANDLE INT4
                           exceptions NO_USER_MESSAGE, SERVICE_ERROR
    Z_BLA_RFC_CHAT_READ    IV_HANDLE INT4, IV_MAX_CHUNKS INT4 opt
                           -> T ET_CHUNKS ZBLA_RFC_CHUNK (TEXT, NEWLINE, DONE), EV_DONE CHAR1
                           exceptions INVALID_HANDLE, SERVICE_ERROR
    Z_BLA_RFC_CHAT_CLOSE   IV_HANDLE INT4
    Z_AI_CHAT_COMPLETION   I_MODEL CHAR20 opt, I_PROMPT CHAR255, I_SESSID CHAR18 opt,
                           T IT_HISTORY ZCHATMSG opt
                           -> T ET_RESPONSE TLINE, E_MODEL CHAR20, E_TOKENS INT4
                           exceptions EMPTY_PROMPT, MODEL_NOT_AVAILABLE, CONTENT_FILTERED

READ blocks up to CHAT_READ_WAIT seconds on a registration thread, which
is what the protocol asks for; the registration is bound to the calling
ABAP session for the whole conversation anyway (CHAT_PLUGIN_PLAN.md, F1).
"""
from __future__ import annotations

import logging

from curie import config
from curie.chat import ChatError, messages_from_rows, messages_from_zchatmsg
from curie.chat import service as chat_service
from curie.handlers.registry import ABAPException, caller, handler

log = logging.getLogger(__name__)

FUNCTIONS = ("Z_BLA_RFC_CHAT_OPEN", "Z_BLA_RFC_CHAT_READ", "Z_BLA_RFC_CHAT_CLOSE",
             "Z_AI_CHAT_COMPLETION")


def _service():
    svc = chat_service.current()
    if svc is None:
        raise ABAPException("SERVICE_ERROR", "chat plugin is off")
    return svc


def _abap(exc: ChatError) -> ABAPException:
    return ABAPException(exc.key, exc.message)


def z_bla_rfc_chat_open(request_context: dict, IT_MESSAGES: list | None = None,
                        **kwargs) -> dict:
    svc = _service()
    attrs = request_context.get("connection_attributes", {})
    try:
        handle = svc.open(messages_from_rows(IT_MESSAGES), caller=caller(request_context),
                          caller_attrs=attrs)
    except ChatError as exc:
        log.warning("Z_BLA_RFC_CHAT_OPEN from %s refused: %s %s", caller(request_context),
                    exc.key, exc.message)
        raise _abap(exc) from None
    return {"EV_HANDLE": handle}


def z_bla_rfc_chat_read(request_context: dict, IV_HANDLE: int = 0, IV_MAX_CHUNKS: int = 1,
                        **kwargs) -> dict:
    svc = _service()
    try:
        chunks, done = svc.read(IV_HANDLE, IV_MAX_CHUNKS)
    except ChatError as exc:
        raise _abap(exc) from None
    return {"ET_CHUNKS": [c.row() for c in chunks], "EV_DONE": "X" if done else " "}


def z_bla_rfc_chat_close(request_context: dict, IV_HANDLE: int = 0, **kwargs) -> dict:
    svc = chat_service.current()
    if svc is not None:
        try:
            svc.close(IV_HANDLE)
        except Exception:  # noqa: BLE001 -- CLOSE never raises
            log.exception("Z_BLA_RFC_CHAT_CLOSE #%s", IV_HANDLE)
    return {}


def z_ai_chat_completion(request_context: dict, I_MODEL: str = "", I_PROMPT: str = "",
                         I_SESSID: str = "", IT_HISTORY: list | None = None,
                         **kwargs) -> dict:
    svc = _service()
    who = caller(request_context)
    prompt = (I_PROMPT or "").rstrip()
    if not prompt.strip():
        raise ABAPException("EMPTY_PROMPT")
    messages = messages_from_zchatmsg(IT_HISTORY, prompt)
    try:
        result = svc.complete(messages, model=I_MODEL, caller=f"{who} sess {I_SESSID.strip()}",
                              caller_attrs=request_context.get("connection_attributes", {}),
                              width=132)
    except ChatError as exc:
        log.warning("Z_AI_CHAT_COMPLETION from %s failed: %s %s", who, exc.key, exc.message)
        if exc.key in ("MODEL_NOT_AVAILABLE", "EMPTY_PROMPT", "CONTENT_FILTERED"):
            raise _abap(exc) from None
        # The interface has no SERVICE_ERROR; the nearest declared one.
        raise ABAPException("MODEL_NOT_AVAILABLE", exc.message) from None
    if result["finish"] == "content_filter" and not result["lines"]:
        raise ABAPException("CONTENT_FILTERED")
    log.info("Z_AI_CHAT_COMPLETION for %s: %d line(s), %s, ~%d tokens", who,
             len(result["lines"]), result["finish"], result["tokens"])
    return {"ET_RESPONSE": [line.tline() for line in result["lines"]],
            "E_MODEL": result["model"], "E_TOKENS": int(result["tokens"])}


if config.CHAT_PLUGIN:
    handler("Z_BLA_RFC_CHAT_OPEN", optional=True)(z_bla_rfc_chat_open)
    handler("Z_BLA_RFC_CHAT_READ", optional=True)(z_bla_rfc_chat_read)
    handler("Z_BLA_RFC_CHAT_CLOSE", optional=True)(z_bla_rfc_chat_close)
    handler("Z_AI_CHAT_COMPLETION", optional=True)(z_ai_chat_completion)
