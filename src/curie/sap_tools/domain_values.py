"""Fixed values of a domain (DD07T/DD07L) for a table field or a domain name.

This is how Curie learns the technical structure from SAP itself: the
meaning of STATUS = '14' is in the dictionary, not in a document. For a
field with a check table the tool says so, so the model can read that
table instead.

    DD03L  table, field -> ROLLNAME (data element), DOMNAME
    DD04L  data element -> DOMNAME (when DD03L has none)
    DD01L  domain -> DATATYPE, LENG, ENTITYTAB (value table)
    DD07T  domain, language -> DOMVALUE_L/H, DDTEXT (texts; language SAP_LANGU, then E)
    DD07L  domain -> DOMVALUE_L/H (values without texts)
"""
from __future__ import annotations

import logging

from curie import config
from curie.caches import TTLCache
from curie.sap_tools.read_table import ReadTableError, read_rows, rows_as_dicts

log = logging.getLogger(__name__)


class DomainValues:
    def __init__(self, sap, langu: str | None = None):
        self.sap = sap
        self.langu = (langu or config.SAP_LANGU)[:1]
        self._cache = TTLCache("domain_values", config.DDIC_CACHE_TTL, config.CACHE_MAX_ENTRIES,
                               persist=True)

    def values(self, table: str = "", field: str = "", domain: str = "") -> dict:
        table, field, domain = (table or "").strip().upper(), (field or "").strip().upper(), \
            (domain or "").strip().upper()
        if not domain and not (table and field):
            raise ReadTableError("give either a domain name or a table and a field")
        key = (table, field, domain)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        result = self._load(table, field, domain)
        self._cache.put(key, result)
        return result

    def _load(self, table: str, field: str, domain: str) -> dict:
        out = {"table": table, "field": field, "data_element": "", "domain": domain,
               "type": "", "length": 0, "check_table": "", "value_table": "",
               "values": [], "note": ""}
        if table and field:
            cols, rows = read_rows(self.sap, "DD03L",
                                   ["FIELDNAME", "ROLLNAME", "DOMNAME", "CHECKTABLE", "DATATYPE", "LENG"],
                                   f"TABNAME = '{table}' AND FIELDNAME = '{field}'", 1)
            recs = rows_as_dicts(cols, rows)
            if not recs:
                raise ReadTableError(f"field {field} not found in table {table}")
            r = recs[0]
            out["data_element"] = str(r.get("ROLLNAME", "")).strip()
            out["domain"] = str(r.get("DOMNAME", "")).strip()
            out["check_table"] = str(r.get("CHECKTABLE", "")).strip()
            out["type"] = str(r.get("DATATYPE", "")).strip()
            out["length"] = _int(r.get("LENG"))
            if not out["domain"] and out["data_element"]:
                cols, rows = read_rows(self.sap, "DD04L", ["ROLLNAME", "DOMNAME"],
                                       f"ROLLNAME = '{out['data_element']}'", 1)
                for rr in rows_as_dicts(cols, rows):
                    out["domain"] = str(rr.get("DOMNAME", "")).strip()
        domain = out["domain"]
        if not domain:
            out["note"] = "the field has no domain (typed directly); no fixed values"
            return out
        cols, rows = read_rows(self.sap, "DD01L", ["DOMNAME", "DATATYPE", "LENG", "ENTITYTAB"],
                               f"DOMNAME = '{domain}'", 1)
        for r in rows_as_dicts(cols, rows):
            out["type"] = out["type"] or str(r.get("DATATYPE", "")).strip()
            out["length"] = out["length"] or _int(r.get("LENG"))
            out["value_table"] = str(r.get("ENTITYTAB", "")).strip()

        # Values come from DD07L; DD07T holds only position and text on 4.5B
        # (newer releases repeat the values there). Join by VALPOS.
        cols, rows = read_rows(self.sap, "DD07L", ["VALPOS", "DOMVALUE_L", "DOMVALUE_H"],
                               f"DOMNAME = '{domain}'", 500)
        values = [{"pos": _int(r.get("VALPOS")),
                   "low": str(r.get("DOMVALUE_L", "")).strip(),
                   "high": str(r.get("DOMVALUE_H", "")).strip(), "text": ""}
                  for r in rows_as_dicts(cols, rows)]
        if values:
            texts: dict[int, str] = {}
            for langu in dict.fromkeys([self.langu, "E"]):
                cols, rows = read_rows(self.sap, "DD07T", ["VALPOS", "DDTEXT"],
                                       f"DOMNAME = '{domain}' AND DDLANGUAGE = '{langu}'", 500)
                for r in rows_as_dicts(cols, rows):
                    texts.setdefault(_int(r.get("VALPOS")), str(r.get("DDTEXT", "")).strip())
                if texts:
                    break
            for v in values:
                v["text"] = texts.get(v["pos"], "")
        values.sort(key=lambda v: v["pos"])
        for v in values:
            v.pop("pos", None)
        out["values"] = values
        notes = []
        if not values:
            notes.append("the domain has no fixed values")
        if out["value_table"]:
            notes.append(f"allowed values are the keys of table {out['value_table']}")
        if out["check_table"] and out["check_table"] != "*":
            notes.append(f"the field is checked against table {out['check_table']}")
        out["note"] = "; ".join(notes)
        return out


def _int(value) -> int:
    try:
        return int(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0
