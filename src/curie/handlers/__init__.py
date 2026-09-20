"""Functions this process serves to the SAP system, one module per topic.

Drop a new module in this package, decorate its functions with @handler("FM"),
and the server picks them up at startup -- no registration list to maintain,
because importing this package imports every module in it.

    handlers/
        registry.py        the @handler decorator and HANDLERS (never edit)
        connectivity.py    RFC_PING / STFC_CONNECTION / STFC_STRUCTURE
        session.py         Z_AI_SESSION_OPEN / HEARTBEAT / SESSION_CLOSE / LIST_DELIVER
        zbla_chat.py       the chat plugin (CHAT_PLUGIN=1): Z_BLA_RFC_CHAT_* and
                           Z_AI_CHAT_COMPLETION, registered as optional handlers
        example_z.py       template for your own function modules
        <your module>.py   one file per area: materials, orders, ...

Modules whose name starts with "_" are skipped, which is how you park one
without deleting it.

Callback contract:

    def callback(request_context, **abap_import_params) -> dict

IMPORT, CHANGING and TABLES parameters arrive as keyword arguments named
exactly like in ABAP (upper case), converted to Python types (structures
become dicts, tables lists of dicts). The returned dict carries EXPORT,
CHANGING and TABLES parameters back; keys that are not in the interface are
rejected, missing ones stay initial.

request_context holds "function_name" and "connection_attributes" -- who is
calling: sysId, client, user, progName, partnerRel and so on.

To report a failure to ABAP, raise ABAPException("NAME"), where NAME is
declared in the function module's EXCEPTIONS list; the caller sees the
matching non-zero SY-SUBRC. Any other exception is logged and reported as a
generic external failure (SYSTEM_FAILURE).

With more than one gateway registration (RFC_REG_COUNT) handlers run in
parallel threads, so keep them thread-safe: no module-level mutable state
without a lock.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil

from curie.handlers.registry import ABAPException, HANDLERS, OPTIONAL, caller, handler

__all__ = ["HANDLERS", "OPTIONAL", "handler", "caller", "ABAPException", "discover"]

log = logging.getLogger(__name__)


def discover() -> dict:
    """Import every handler module in this package; returns HANDLERS."""
    for module in pkgutil.iter_modules(__path__):
        if module.name.startswith("_") or module.name == "registry":
            continue
        importlib.import_module(f"{__name__}.{module.name}")
        log.debug("loaded handler module %s", module.name)
    return HANDLERS


discover()
