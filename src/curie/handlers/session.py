"""The function modules SAP calls on Curie: session open, heartbeat, close,
and the list delivery from the background job.

Interfaces (create in SE37, Remote-Enabled, body empty; see abap/):

    Z_AI_SESSION_OPEN   I_PROMPT CHAR255 opt, T_PROMPT tables ZAI_LINE opt
                        -> E_SESSION_ID CHAR32, E_URL CHAR255, E_HEARTBEAT_SEC I
                        exceptions SERVER_BUSY
    Z_AI_HEARTBEAT      I_SESSION_ID CHAR32
                        -> E_STATE CHAR16, E_STATUS_TEXT CHAR80
                        exceptions UNKNOWN_SESSION
    Z_AI_SESSION_CLOSE  I_SESSION_ID CHAR32, I_REASON CHAR32 opt
    Z_AI_LIST_DELIVER   I_SESSION_ID CHAR32, I_RUN_ID CHAR8, I_RC I,
                        I_MESSAGE CHAR255 opt, T_LIST tables ZAI_LISTLINE
                        exceptions UNKNOWN_RUN

These run on the RFC server threads and must stay fast: they only touch the
SessionManager, which defers anything asyncio-related to the loop.
"""
from __future__ import annotations

import logging

from curie import config
from curie.handlers.registry import ABAPException, caller, handler
from curie.sessions import manager as session_manager

log = logging.getLogger(__name__)


def _lines(rows) -> list[str]:
    """Text of each row of a one-field line table, whatever the field is called."""
    out = []
    for row in rows or []:
        value = row.get("LINE") if "LINE" in row else next(iter(row.values()), "")
        out.append(str(value or "").rstrip())
    return out


@handler("Z_AI_SESSION_OPEN")
def z_ai_session_open(request_context: dict, I_PROMPT: str = "",
                      T_PROMPT: list | None = None, **kwargs) -> dict:
    mgr = session_manager.current()
    prompt = "\n".join(_lines(T_PROMPT)) if T_PROMPT else (I_PROMPT or "")
    try:
        session = mgr.open_from_rfc(request_context.get("connection_attributes", {}),
                                    prompt)
    except session_manager.SessionLimit as exc:
        log.warning("Z_AI_SESSION_OPEN from %s refused: %s", caller(request_context), exc)
        raise ABAPException("SERVER_BUSY") from None
    return {
        "E_SESSION_ID": session.id,
        "E_URL": mgr.url_for(session),
        "E_HEARTBEAT_SEC": config.SESSION_HEARTBEAT_SEC,
    }


@handler("Z_AI_HEARTBEAT")
def z_ai_heartbeat(request_context: dict, I_SESSION_ID: str = "", **kwargs) -> dict:
    mgr = session_manager.current()
    try:
        state, text = mgr.heartbeat(I_SESSION_ID.strip())
    except KeyError:
        raise ABAPException("UNKNOWN_SESSION") from None
    return {"E_STATE": state, "E_STATUS_TEXT": text}


@handler("Z_AI_SESSION_CLOSE")
def z_ai_session_close(request_context: dict, I_SESSION_ID: str = "",
                       I_REASON: str = "", **kwargs) -> dict:
    mgr = session_manager.current()
    mgr.close_from_rfc(I_SESSION_ID.strip(), (I_REASON or "sap_closed").strip())
    return {}


@handler("Z_AI_LIST_DELIVER")
def z_ai_list_deliver(request_context: dict, I_SESSION_ID: str = "",
                      I_RUN_ID: str = "", I_RC: int = 0, I_MESSAGE: str = "",
                      T_LIST: list | None = None, **kwargs) -> dict:
    mgr = session_manager.current()
    lines = _lines(T_LIST)
    log.info("Z_AI_LIST_DELIVER from %s: run %s rc=%s %d line(s)",
             caller(request_context), I_RUN_ID, I_RC, len(lines))
    ok = mgr.deliver_list(I_SESSION_ID.strip(), I_RUN_ID.strip(), int(I_RC or 0),
                          (I_MESSAGE or "").strip(), lines)
    if not ok:
        raise ABAPException("UNKNOWN_RUN")
    # T_LIST is a TABLES parameter: hand back an empty table rather than
    # echoing the list to SAP.
    return {"T_LIST": []}
