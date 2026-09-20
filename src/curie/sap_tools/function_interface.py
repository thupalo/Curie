"""RFC_GET_FUNCTION_INTERFACE_P: the interface of a function module, with
texts in the one-character SY-LANGU (config.SAP_LANGU)."""
from __future__ import annotations

import logging

from curie import config
from curie.caches import TTLCache
from curie.sapnwrfc720 import ABAPException, RFCError

log = logging.getLogger(__name__)

PARAMCLASS = {"I": "IMPORT", "E": "EXPORT", "T": "TABLES", "C": "CHANGING",
              "X": "EXCEPTION"}


class FunctionInterfaceError(Exception):
    pass


class FunctionInterfaceCache:
    def __init__(self, sap, langu: str | None = None, ttl: float | None = None):
        self.sap = sap
        self.langu = (langu or config.SAP_LANGU)[:1]
        self.ttl = config.DDIC_CACHE_TTL if ttl is None else ttl
        self._cache = TTLCache("function_interfaces", self.ttl, config.CACHE_MAX_ENTRIES, persist=True)

    def interface(self, funcname: str) -> dict:
        funcname = (funcname or "").strip().upper()
        if not funcname:
            raise FunctionInterfaceError("function module name is required")
        hit = self._cache.get(funcname)
        if hit is not None:
            return hit
        info = self._load(funcname)
        self._cache.put(funcname, info)
        return info

    def _load(self, funcname: str) -> dict:
        try:
            # The parameter table is PARAMS_P on 4.5B and PARAMS on newer
            # releases; call_known() keeps the one that exists.
            out = self.sap.call_known(
                "RFC_GET_FUNCTION_INTERFACE_P",
                FUNCNAME=funcname, LANGUAGE=self.langu, LANGU=self.langu,
                PARAMS=[], PARAMS_P=[],
            )
        except ABAPException as exc:
            raise FunctionInterfaceError(
                f"{funcname}: {exc.key} {exc.message}".strip()) from None
        except RFCError as exc:
            raise FunctionInterfaceError(f"{funcname}: {exc}") from None

        params = []
        for row in out.get("PARAMS_P") or out.get("PARAMS") or []:
            cls = str(row.get("PARAMCLASS", "")).strip().upper()
            params.append({
                "name": str(row.get("PARAMETER", "")).strip(),
                "class": PARAMCLASS.get(cls, cls),
                "structure": str(row.get("TABNAME", "")).strip(),
                "field": str(row.get("FIELDNAME", "")).strip(),
                "type": str(row.get("EXID", "")).strip(),
                "length": _int(row.get("INTLENGTH")),
                "decimals": _int(row.get("DECIMALS")),
                "default": str(row.get("DEFAULT", "")).strip(),
                "optional": str(row.get("OPTIONAL", "")).strip() == "X",
                "text": str(row.get("PARAMTEXT", "")).strip(),
            })
        return {
            "name": funcname,
            "remote_enabled": str(out.get("REMOTE_CALL", "")).strip() in ("X", "R"),
            "update_task": str(out.get("UPDATE_TASK", "")).strip() == "X",
            "short_text": str(out.get("SHORT_TEXT", "")).strip(),
            "parameters": [p for p in params if p["class"] != "EXCEPTION"],
            "exceptions": [p["name"] for p in params if p["class"] == "EXCEPTION"],
        }


def _int(value) -> int:
    try:
        return int(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0
