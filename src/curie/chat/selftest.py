"""The chat plugin's round of `curie --selftest`: from inside SAP, through
the SM59 destination, one streamed answer and one Z_AI_CHAT_COMPLETION.

Runs with RFC_ABAP_INSTALL_AND_RUN like the session round in cli.py and
needs the DDIC structures and the four function modules in the backend.
"""
from __future__ import annotations

import logging

from curie import config

log = logging.getLogger(__name__)

CHAT_ABAP = """\
REPORT zai_chat_selftest.
DATA: lt_msg LIKE zbla_rfc_msg OCCURS 0 WITH HEADER LINE,
      lt_chk LIKE zbla_rfc_chunk OCCURS 0 WITH HEADER LINE,
      lt_hist LIKE zchatmsg OCCURS 0 WITH HEADER LINE,
      lt_resp LIKE tline OCCURS 0 WITH HEADER LINE,
      lv_h TYPE i, lv_done, lv_n TYPE i, lv_words TYPE i,
      lv_calls TYPE i, lv_msg(80), lv_model LIKE zchatmsg-modelid,
      lv_tok TYPE i, lv_first(80).
lt_msg-msgno = 1. lt_msg-role = 'user'. lt_msg-lineno = 1.
lt_msg-text = 'Say hello in two short lines.'.
APPEND lt_msg.
CALL FUNCTION 'Z_BLA_RFC_CHAT_OPEN' DESTINATION '{dest}'
  IMPORTING ev_handle = lv_h
  TABLES it_messages = lt_msg
  EXCEPTIONS communication_failure = 1 MESSAGE lv_msg
             system_failure = 2 MESSAGE lv_msg
             OTHERS = 3.
IF sy-subrc <> 0.
  WRITE: / 'FAIL', 'CHAT OPEN rc', sy-subrc, lv_msg.
ELSE.
  DO 300 TIMES.
    REFRESH lt_chk.
    CALL FUNCTION 'Z_BLA_RFC_CHAT_READ' DESTINATION '{dest}'
      EXPORTING iv_handle = lv_h iv_max_chunks = 50
      IMPORTING ev_done = lv_done
      TABLES et_chunks = lt_chk
      EXCEPTIONS communication_failure = 1 MESSAGE lv_msg
                 system_failure = 2 MESSAGE lv_msg
                 OTHERS = 3.
    IF sy-subrc <> 0.
      WRITE: / 'FAIL', 'CHAT READ rc', sy-subrc, lv_msg.
      EXIT.
    ENDIF.
    lv_calls = lv_calls + 1.
    DESCRIBE TABLE lt_chk LINES lv_n.
    lv_words = lv_words + lv_n.
    IF lv_first IS INITIAL.
      READ TABLE lt_chk INDEX 1.
      IF sy-subrc = 0. lv_first = lt_chk-text. ENDIF.
    ENDIF.
    IF lv_done = 'X'.
      WRITE: / 'OK', 'CHAT stream', lv_words, 'chunks in', lv_calls,
               'reads, first word', lv_first(30).
      EXIT.
    ENDIF.
  ENDDO.
  CALL FUNCTION 'Z_BLA_RFC_CHAT_CLOSE' DESTINATION '{dest}'
    EXPORTING iv_handle = lv_h
    EXCEPTIONS OTHERS = 1.
ENDIF.
CALL FUNCTION 'Z_AI_CHAT_COMPLETION' DESTINATION '{dest}'
  EXPORTING i_prompt = 'Reply with the single word OK.'
  IMPORTING e_model = lv_model e_tokens = lv_tok
  TABLES it_history = lt_hist et_response = lt_resp
  EXCEPTIONS communication_failure = 1 MESSAGE lv_msg
             system_failure = 2 MESSAGE lv_msg
             OTHERS = 3.
IF sy-subrc <> 0.
  WRITE: / 'FAIL', 'CHAT COMPLETION rc', sy-subrc, lv_msg.
ELSE.
  DESCRIBE TABLE lt_resp LINES lv_n.
  READ TABLE lt_resp INDEX 1.
  WRITE: / 'OK', 'CHAT completion', lv_n, 'line(s), model', lv_model,
           'tokens', lv_tok, ':', lt_resp-tdline(40).
ENDIF.
"""


def run(sap, dest: str) -> int:
    """0 when both calls succeeded, 1 on a FAIL line, 2 when it could not run."""
    from curie import sapnwrfc720 as sap_sdk

    source = CHAT_ABAP.replace("{dest}", dest)
    try:
        out = sap.call_known("RFC_ABAP_INSTALL_AND_RUN", MODE="F",
                             PROGRAMNAME="ZAI_CHAT_SELFTEST",
                             PROGRAM=[{"LINE": line} for line in source.splitlines()],
                             WRITES=[])
    except (sap_sdk.RFCError, sap_sdk.ABAPException) as exc:
        log.error("chat selftest could not run: %s", exc)
        return 2
    if str(out.get("ERRORMESSAGE", "")).strip():
        log.error("chat selftest: %s (are ZBLA_RFC_MSG/ZBLA_RFC_CHUNK and the four "
                  "function modules in the backend?)", out["ERRORMESSAGE"])
        return 2
    failed = 0
    for row in out.get("WRITES", []):
        text = str(next(iter(row.values()))).strip()
        if text.startswith("FAIL"):
            failed += 1
            log.error("%s", text)
        elif text:
            log.info("%s", text)
    if failed:
        log.error("chat selftest: %d call(s) failed through %s", failed, dest)
        return 1
    log.info("chat selftest ok through destination %s (models %s)", dest,
             ", ".join(config.chat_models()))
    return 0
