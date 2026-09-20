"""Tool schemas for the model and the dispatcher that runs them in SAP."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from curie import config
from curie.sap_tools.ddic import DdicError
from curie.sap_tools.function_interface import FunctionInterfaceError
from curie.sap_tools.read_table import ReadTableError

log = logging.getLogger(__name__)

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "sap_read_table",
            "description": (
                "Read rows from an SAP table with RFC_READ_TABLE. Returns column "
                "names, typed values and a note about omitted fields or paging. "
                "Ask for specific fields on wide tables (a row is limited to 512 "
                "characters)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {"type": "string", "description": "Table name, e.g. MARA"},
                    "fields": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Field names to return; omit for an automatic choice",
                    },
                    "where": {
                        "type": "string",
                        "description": "Open SQL WHERE clause without the word WHERE, "
                                       "e.g. \"MTART = 'FERT' AND MATNR LIKE 'A%'\"",
                    },
                    "max_rows": {"type": "integer", "description": "Rows to return (default 50)"},
                    "skip": {"type": "integer", "description": "Rows to skip, for paging"},
                },
                "required": ["table"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sap_get_structure",
            "description": "Field list of a table or structure: name, key flag, type, "
                           "length, decimals, text, data element, check table.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Table or structure name"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sap_get_domain_values",
            "description": "Fixed values of a field's domain with their texts (e.g. what "
                           "STATUS '14' means), or the check table that holds the allowed "
                           "values. Give table and field, or a domain name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {"type": "string", "description": "Table name, e.g. T001"},
                    "field": {"type": "string", "description": "Field name, e.g. STATUS"},
                    "domain": {"type": "string", "description": "Domain name instead of table+field"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sap_knowledge",
            "description": "Search Curie's notes about this SAP system: what tables hold, "
                           "business terms, hints from users, programs confirmed earlier.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Terms or table names"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sap_find",
            "description": "Find tables and fields by words (Polish or English) in Curie's "
                           "local copy of the data dictionary: 'polisa', 'numer rejestracyjny', "
                           "'invoice date'. Returns the tables with their texts and the matching "
                           "fields with exact names and types. Use it instead of reading DD02T "
                           "or DD03L.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Words, a table name or a field name"},
                    "kind": {"type": "string", "enum": ["any", "table", "field"],
                             "description": "What to look for (default any)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sap_get_function_interface",
            "description": "Interface of a function module: parameters with class "
                           "(IMPORT/EXPORT/TABLES/CHANGING), type, default, text; exceptions.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Function module name"}},
                "required": ["name"],
            },
        },
    },
]


class ToolBox:
    def __init__(self, read_table, ddic, function_interface, sap, domains=None, knowledge=None):
        self.read_table = read_table
        self.ddic = ddic
        self.fi = function_interface
        self.sap = sap
        self.domains = domains
        self.knowledge = knowledge

    @property
    def schemas(self) -> list[dict]:
        out = []
        for s in TOOL_SCHEMAS:
            name = s["function"]["name"]
            if name == "sap_knowledge" and self.knowledge is None:
                continue
            if name == "sap_find" and not self.has_dictionary:
                continue
            if name == "sap_get_domain_values" and self.domains is None:
                continue
            out.append(s)
        return out

    @property
    def has_dictionary(self) -> bool:
        """sap_find is offered when the knowledge facade has a built index."""
        knowledge = self.knowledge
        if knowledge is None or not hasattr(knowledge, "find"):
            return False
        try:
            return len(knowledge.dictionary) > 0
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def tables_in(name: str, args: dict) -> list[str]:
        """Table names a call refers to, for the session's table memory."""
        if name in ("sap_read_table", "sap_get_domain_values"):
            return [str(args.get("table", "")).upper()] if args.get("table") else []
        if name == "sap_get_structure":
            return [str(args.get("name", "")).upper()] if args.get("name") else []
        return []

    def describe(self, name: str, args: dict) -> str:
        """Short human text for the UI and the SAP status line."""
        if name == "sap_read_table":
            where = args.get("where")
            return f"Reading table {args.get('table', '?')}" + (f" where {where}" if where else "")
        if name == "sap_get_structure":
            return f"Reading structure {args.get('name', '?')}"
        if name == "sap_get_function_interface":
            return f"Reading interface of {args.get('name', '?')}"
        if name == "sap_get_domain_values":
            what = args.get("domain") or f"{args.get('table', '?')}-{args.get('field', '?')}"
            return f"Reading domain values of {what}"
        if name == "sap_knowledge":
            return f"Searching notes for {args.get('query', '?')}"
        if name == "sap_find":
            return f"Searching the dictionary for {args.get('query', '?')}"
        return f"Calling {name}"

    async def dispatch(self, name: str, arguments: str | dict) -> tuple[bool, dict, str]:
        """Run a tool; returns (ok, result_dict, summary_for_ui)."""
        args = arguments if isinstance(arguments, dict) else _parse_args(arguments)
        started = time.monotonic()
        try:
            if name == "sap_read_table":
                result = await self.sap.run(
                    self.read_table.read, args.get("table", ""), args.get("fields"),
                    args.get("where"), args.get("max_rows"), args.get("skip", 0))
                summary = (f"{result['row_count']} row(s), "
                           f"{len(result['columns'])} column(s)"
                           + (f"; {result['note']}" if result.get("note") else ""))
            elif name == "sap_get_structure":
                result = await self.sap.run(self.ddic.structure_as_dict, args.get("name", ""))
                summary = f"{len(result['fields'])} field(s)"
            elif name == "sap_get_function_interface":
                result = await self.sap.run(self.fi.interface, args.get("name", ""))
                summary = (f"{len(result['parameters'])} parameter(s), "
                           f"{'remote-enabled' if result['remote_enabled'] else 'not remote-enabled'}")
            elif name == "sap_get_domain_values" and self.domains is not None:
                result = await self.sap.run(self.domains.values, args.get("table", ""),
                                            args.get("field", ""), args.get("domain", ""))
                summary = (f"domain {result['domain'] or '-'}: {len(result['values'])} value(s)"
                           + (f"; {result['note']}" if result.get("note") else ""))
            elif name == "sap_knowledge" and self.knowledge is not None:
                notes = self.knowledge.search(str(args.get("query", "")))
                result = {"notes": [
                    {"id": n.id, "type": n.type, "name": n.name, "status": n.status,
                     "source": n.source, "tables": n.tables, "text": n.body[:1500]}
                    for n in notes]}
                summary = f"{len(notes)} note(s)"
            elif name == "sap_find" and self.knowledge is not None and hasattr(self.knowledge, "find"):
                result = self.knowledge.find(str(args.get("query", "")), kind=str(args.get("kind") or "any"))
                summary = (f"{len(result['tables'])} table(s)"
                           + (f"; {result['note']}" if result.get("note") else ""))
            else:
                return False, {"error": f"unknown tool {name}"}, "unknown tool"
        except (ReadTableError, DdicError, FunctionInterfaceError) as exc:
            return False, {"error": str(exc)}, f"error: {exc}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- the model gets the text, we get the trace
            log.exception("tool %s failed", name)
            return False, {"error": f"{type(exc).__name__}: {exc}"}, f"error: {exc}"
        finally:
            log.debug("tool %s %.0f ms", name, (time.monotonic() - started) * 1000)
        return True, result, summary


def _parse_args(text: str) -> dict:
    if not text or not text.strip():
        return {}
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def tool_message(result: dict, limit: int | None = None) -> str:
    """JSON for the model, cut to TOOL_RESULT_MAX_CHARS without breaking rows."""
    limit = limit or config.TOOL_RESULT_MAX_CHARS
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    if "rows" in result and isinstance(result["rows"], list):
        rows = list(result["rows"])
        while rows and len(text) > limit:
            rows = rows[: max(len(rows) // 2, 1) if len(rows) > 1 else 0]
            trimmed = dict(result, rows=rows, row_count=len(rows), truncated=True,
                           note=(result.get("note", "") + "; result cut to fit the "
                                 "context, ask for fewer fields or rows").strip("; "))
            text = json.dumps(trimmed, ensure_ascii=False, default=str)
        return text
    return text[:limit] + "... (truncated)"
