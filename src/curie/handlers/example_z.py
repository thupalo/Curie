"""Template for your own function modules -- copy this file and edit.

The ABAP side needs the *interface* only, not an implementation:

    SE37 > create Z_AI_ECHO
      Attributes  : Processing type = Remote-Enabled Module
      Import      : I_TEXT   TYPE  CHAR255      (or a DDIC type of yours)
                    I_COUNT  TYPE  I            optional, default 1
      Export      : E_TEXT   TYPE  CHAR255
                    E_STAMP  TYPE  SY-DATUM
      Tables      : T_LINES  LIKE  RFCTEST      any flat DDIC structure
      Exceptions  : NO_INPUT
      Source code : leave empty -- the body lives here

Call it from ABAP with DESTINATION pointing at the SM59 registration:

    CALL FUNCTION 'Z_AI_ECHO' DESTINATION 'SAP_AI'
      EXPORTING i_text  = 'hello'
      IMPORTING e_text  = lv_text
      TABLES    t_lines = lt_lines
      EXCEPTIONS no_input = 1
                 system_failure = 2 MESSAGE lv_msg
                 communication_failure = 3 MESSAGE lv_msg.

Nothing here is registered until you drop the "# " from the decorator: an
installed handler whose function module does not exist in the backend makes
the server fail at startup, which is deliberate -- better than discovering it
on the first call.
"""
from __future__ import annotations

import logging
from datetime import date

from curie.handlers.registry import ABAPException, caller, handler

log = logging.getLogger(__name__)


# @handler("Z_AI_ECHO")
def z_ai_echo(
    request_context: dict,
    I_TEXT: str = "",
    I_COUNT: int = 1,
    T_LINES: list | None = None,
    **kwargs,
) -> dict:
    """Uppercase the text, count the rows, hand a stamp back.

    Every IMPORT/CHANGING/TABLES parameter of the interface arrives as a
    keyword argument of the same name. Give each one a default so that a
    caller omitting an optional parameter does not break the signature, and
    keep **kwargs so that adding a parameter in SE37 does not break this
    function before you get round to using it.
    """
    rows = list(T_LINES or [])
    log.info(
        "Z_AI_ECHO from %s: %r x%d, %d row(s)",
        caller(request_context), I_TEXT, I_COUNT, len(rows),
    )

    if not I_TEXT:
        # Declared in the EXCEPTIONS list of the FM -> non-zero SY-SUBRC.
        raise ABAPException("NO_INPUT")

    # Only keys that exist in the interface may be returned; anything else is
    # rejected, which catches typos on the spot rather than silently dropping.
    return {
        "E_TEXT": (I_TEXT * max(I_COUNT, 1)).upper(),
        "E_STAMP": date.today(),
        "T_LINES": rows,
    }
