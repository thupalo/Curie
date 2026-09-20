"""The handler registry: what every handler module imports.

Kept separate from handlers/__init__.py so that importing it can never pull in
the handler modules themselves -- __init__ imports this first, then discovers
the modules, and each of them imports back from here without a cycle.
"""
from __future__ import annotations

import logging
from typing import Callable

from curie.sapnwrfc720 import ABAPException  # re-exported: handlers raise it

__all__ = ["HANDLERS", "OPTIONAL", "handler", "caller", "ABAPException"]

log = logging.getLogger(__name__)

# func_name (as known in SAP) -> Python callback
HANDLERS: dict[str, Callable] = {}
# Names whose absence in the backend disables a plugin instead of the start.
OPTIONAL: set[str] = set()


def handler(func_name: str, optional: bool = False) -> Callable[[Callable], Callable]:
    """Register a callback under the name of an ABAP function module.

    The name has to be a remote-enabled function module that exists in the
    backend: the server reads its interface over the client connection when
    installing the handler. With optional=True a missing module is logged
    and skipped at startup rather than aborting it (plugins).
    """

    def decorate(func: Callable) -> Callable:
        name = func_name.upper()
        if name in HANDLERS:
            existing = HANDLERS[name]
            raise ValueError(
                f"handler for {name} already registered by "
                f"{existing.__module__}.{existing.__name__}"
            )
        HANDLERS[name] = func
        if optional:
            OPTIONAL.add(name)
        return func

    return decorate


def caller(request_context: dict) -> str:
    """'SID/client user@program' of the ABAP side, for log lines."""
    attr = request_context.get("connection_attributes", {})
    return (
        f"{attr.get('sysId', '?')}/{attr.get('client', '?')} "
        f"{attr.get('user', '?')}@{attr.get('progName', '?')}"
    )
