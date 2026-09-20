"""Connectivity checks -- the function modules SM59 and SE37 use to prove a link.

These three exist in every SAP system, so they need no ABAP development and
are the fastest way to verify that a registration works end to end:

    SM59 > the destination > Connection Test        -> RFC_PING
    SE37 > STFC_CONNECTION, RFC target sys = dest   -> STFC_CONNECTION
    a report calling STFC_STRUCTURE with a table    -> STFC_STRUCTURE

STFC_STRUCTURE is the one worth keeping: it puts a structure and an internal
table through every type conversion the server does.
"""
from __future__ import annotations

import logging
from datetime import datetime

from curie.handlers.registry import caller, handler

log = logging.getLogger(__name__)


@handler("RFC_PING")
def rfc_ping(request_context: dict, **kwargs) -> dict:
    """No parameters at all; SM59 > Connection Test lands here."""
    log.info("RFC_PING from %s", caller(request_context))
    return {}


@handler("STFC_CONNECTION")
def stfc_connection(request_context: dict, REQUTEXT: str = "", **kwargs) -> dict:
    """Echo test. SE37 > STFC_CONNECTION with RFC target sys = the SM59 dest."""
    log.info("STFC_CONNECTION from %s: %r", caller(request_context), REQUTEXT)
    return {
        "ECHOTEXT": REQUTEXT,
        "RESPTEXT": f"Python RFC server (NW RFC 7.20), "
                    f"{datetime.now():%Y-%m-%d %H:%M:%S}",
    }


@handler("STFC_STRUCTURE")
def stfc_structure(
    request_context: dict,
    IMPORTSTRUCT: dict | None = None,
    RFCTABLE: list | None = None,
    **kwargs,
) -> dict:
    """Structure and table round trip -- the counterpart of the ABAP test FM.

    IMPORTSTRUCT arrives as a dict of Python values (RFCDATE a date, RFCTIME a
    time, RFCINT4 an int, RFCHEX3 bytes, ...) and RFCTABLE as a list of such
    dicts. Both go back the same way, so this exercises every conversion the
    server does.
    """
    row = dict(IMPORTSTRUCT or {})
    rows = list(RFCTABLE or [])
    log.info(
        "STFC_STRUCTURE from %s: %d row(s) in, RFCDATA1=%r RFCDATE=%r",
        caller(request_context), len(rows), row.get("RFCDATA1"), row.get("RFCDATE"),
    )

    # Like the ABAP original: echo the structure and append it to the table.
    echo = dict(row)
    echo["RFCDATA2"] = f"served by Python {datetime.now():%H:%M:%S}"
    rows.append(echo)

    return {
        "ECHOSTRUCT": echo,
        "RESPTEXT": f"STFC_STRUCTURE ok, returning {len(rows)} row(s)",
        "RFCTABLE": rows,
    }
