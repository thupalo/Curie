"""Structure definitions: RFC_GET_STRUCTURE_DEFINITION_P plus DD03L/DD04T.

RFC_GET_STRUCTURE_DEFINITION_P gives the fields as the RFC layer sees them
(name, position, offset, internal length, decimals, ABAP type). It does not
say which fields are keys, and older releases give no texts, so the cache
adds KEYFLAG/DATATYPE/ROLLNAME from DD03L and texts from DD04T when it can.
Every enrichment is optional: a failure is logged and the plain definition
is returned.

The language parameter of the _P function is the one-character SY-LANGU
(config.SAP_LANGU); its name differs between releases, so both LANGU and
LANGUAGE are offered and call_known() keeps the one the backend has.
"""
from __future__ import annotations

import logging
from dataclasses import asdict

from curie import config
from curie.caches import TTLCache
from curie.sap_tools.read_table import FieldInfo, read_rows, rows_as_dicts
from curie.sapnwrfc720 import ABAPException, RFCError

log = logging.getLogger(__name__)


class DdicError(Exception):
    pass


def encode_structure(info: dict) -> dict:
    """The cached structure as JSON-native data (FieldInfo -> dict)."""
    fields = info.get("fields") or []
    if not all(isinstance(f, FieldInfo) for f in fields):
        raise TypeError("structure fields are not FieldInfo objects")
    return {**info, "fields": [asdict(f) for f in fields]}


def decode_structure(data: dict) -> dict:
    """Back from disk: dicts -> FieldInfo; anything else is refused."""
    fields = data.get("fields") or []
    if not fields or not all(isinstance(f, dict) for f in fields):
        raise TypeError("structure fields on disk are not field records")
    return {**data, "fields": [FieldInfo(**f) for f in fields]}


class DdicCache:
    def __init__(self, sap, langu: str | None = None, ttl: float | None = None):
        self.sap = sap
        self.langu = (langu or config.SAP_LANGU)[:1]
        self.ttl = config.DDIC_CACHE_TTL if ttl is None else ttl
        self._cache = TTLCache("ddic_structures", self.ttl, config.CACHE_MAX_ENTRIES, persist=True,
                               encode=encode_structure, decode=decode_structure)

    # -- public --------------------------------------------------------------- #

    def structure(self, name: str) -> dict:
        """{"name", "text", "fields": [FieldInfo...]}; blocking, cached."""
        name = (name or "").strip().upper()
        if not name:
            raise DdicError("structure name is required")
        hit = self._cache.get(name)
        if hit is not None:
            return hit
        info = self._load(name)
        self._cache.put(name, info)
        return info

    def exists(self, name: str) -> bool:
        """Is `name` a table or structure in the dictionary? A cached
        structure answers without SAP; otherwise one DD02L row is read."""
        name = (name or "").strip().upper()
        if not name:
            return False
        if self._cache.get(name) is not None:
            return True
        from curie.sap_tools.read_table import read_rows
        _cols, rows = read_rows(self.sap, "DD02L", ["TABNAME"], f"TABNAME = '{name}'", 1)
        return bool(rows)

    def structure_as_dict(self, name: str) -> dict:
        """The LLM-facing form (plain dicts)."""
        info = self.structure(name)
        return {
            "name": info["name"],
            "text": info.get("text", ""),
            "fields": [
                {
                    "name": f.name, "key": f.key, "type": f.datatype or f.inttype,
                    "length": f.intlen, "decimals": f.decimals, "text": f.text,
                    "data_element": f.rollname, "check_table": f.checktable,
                }
                for f in info["fields"]
            ],
        }

    # -- loading -------------------------------------------------------------- #

    def _load(self, name: str) -> dict:
        try:
            out = self.sap.call_known(
                "RFC_GET_STRUCTURE_DEFINITION_P",
                TABNAME=name, LANGU=self.langu, LANGUAGE=self.langu, UCLEN=1,
                FIELDS=[],
            )
        except ABAPException as exc:
            raise DdicError(f"{name}: {exc.key} {exc.message}".strip()) from None
        except RFCError as exc:
            raise DdicError(f"{name}: {exc}") from None
        fields = []
        for row in out.get("FIELDS", []):
            fields.append(FieldInfo(
                name=str(row.get("FIELDNAME", "")).strip(),
                position=_int(row.get("POSITION")),
                inttype=str(row.get("EXID", "") or row.get("INTTYPE", "") or "C").strip(),
                intlen=_int(row.get("INTLENGTH")) or _int(row.get("LENGTH")),
                decimals=_int(row.get("DECIMALS")),
                text=str(row.get("FIELDTEXT", "") or "").strip(),
            ))
        fields = [f for f in fields if f.name and not f.name.startswith(".")]
        if not fields:
            raise DdicError(f"{name}: no fields returned")
        info = {"name": name, "text": "", "fields": fields}
        self._enrich_dd03l(info)
        self._enrich_texts(info)
        return info

    def _enrich_dd03l(self, info: dict) -> None:
        try:
            cols, rows = read_rows(
                self.sap, "DD03L",
                ["FIELDNAME", "POSITION", "KEYFLAG", "ROLLNAME", "DATATYPE",
                 "CHECKTABLE", "INTTYPE", "INTLEN", "DECIMALS"],
                f"TABNAME = '{info['name']}'", rowcount=2000)
        except Exception as exc:  # noqa: BLE001 -- enrichment only
            log.debug("DD03L for %s: %s", info["name"], exc)
            return
        by_name = {r["FIELDNAME"]: r for r in rows_as_dicts(cols, rows)}
        for f in info["fields"]:
            r = by_name.get(f.name)
            if not r:
                continue
            f.key = str(r.get("KEYFLAG", "")).strip() == "X"
            f.rollname = str(r.get("ROLLNAME", "")).strip()
            f.datatype = str(r.get("DATATYPE", "")).strip()
            f.checktable = str(r.get("CHECKTABLE", "")).strip()
            if not f.inttype:
                f.inttype = str(r.get("INTTYPE", "C")).strip()

    def _enrich_texts(self, info: dict) -> None:
        missing = [f for f in info["fields"] if not f.text and f.rollname]
        if not missing:
            return
        rollnames = sorted({f.rollname for f in missing})
        texts: dict[str, str] = {}
        for langu in dict.fromkeys([self.langu, "E"]):
            for chunk in _chunks(rollnames, 40):
                names = " OR ".join(f"ROLLNAME = '{r}'" for r in chunk)
                where = f"DDLANGUAGE = '{langu}' AND ( {names} )"
                try:
                    cols, rows = read_rows(self.sap, "DD04T",
                                           ["ROLLNAME", "DDTEXT", "SCRTEXT_M"],
                                           where, rowcount=len(chunk))
                except Exception as exc:  # noqa: BLE001
                    log.debug("DD04T texts: %s", exc)
                    return
                for r in rows_as_dicts(cols, rows):
                    texts.setdefault(r["ROLLNAME"], r.get("DDTEXT") or r.get("SCRTEXT_M") or "")
            rollnames = [r for r in rollnames if r not in texts]
            if not rollnames:
                break
        for f in missing:
            f.text = texts.get(f.rollname, "")


def _int(value) -> int:
    try:
        return int(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]
