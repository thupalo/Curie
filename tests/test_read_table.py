import pytest

from curie import config
from curie.sap_tools.read_table import (FieldInfo, ReadTableError, ReadTableTool, convert_value,
                                  denied, pack_options, parse_rows, plan_fields,
                                  unknown_in_where)
from curie.sapnwrfc720 import ABAPException


def fields(*specs):
    out = []
    for pos, (name, key, inttype, intlen, dec) in enumerate(specs, 1):
        out.append(FieldInfo(name=name, position=pos, key=key, inttype=inttype,
                             intlen=intlen, decimals=dec))
    return out


def test_plan_requested_fields_validates_and_measures():
    fs = fields(("MANDT", True, "C", 3, 0), ("MATNR", True, "C", 18, 0),
                ("MTART", False, "C", 4, 0), ("NTGEW", False, "P", 7, 3))
    plan = plan_fields(fs, ["matnr", "ntgew"])
    assert plan.fields == ["MATNR", "NTGEW"]
    assert plan.width == 18 + (2 * 7 + 1 + 1)
    # a guessed name next to real ones is dropped with a hint, not fatal
    plan = plan_fields(fs, ["MATNR", "MATNRX", "NTGEV"])
    assert plan.fields == ["MATNR"]
    reasons = dict(plan.omitted)
    assert "did you mean MATNR" in reasons["MATNRX"] and "NTGEW" in reasons["NTGEV"]
    # only guessed names: an error that lists the table
    with pytest.raises(ReadTableError, match="unknown field.*MTART"):
        plan_fields(fs, ["NOPE"])
    assert unknown_in_where("MTART = 'FERT' AND matnr LIKE 'A%'", fs) == []
    assert unknown_in_where("GJAHR = '2005' AND MATNR = 'X' OR NRFAK IN ('1')", fs) == ["GJAHR", "NRFAK"]


def test_plan_requested_fields_overflow_is_explained():
    fs = fields(("A", True, "C", 300, 0), ("B", False, "C", 300, 0))
    with pytest.raises(ReadTableError, match="512"):
        plan_fields(fs, ["A", "B"])


def test_plan_auto_keys_first_then_position_until_full():
    fs = fields(("MANDT", True, "C", 3, 0), ("X1", False, "C", 200, 0),
                ("KEY2", True, "C", 10, 0), ("X2", False, "C", 200, 0),
                ("X3", False, "C", 200, 0), ("FL", False, "F", 8, 0),
                ("RAW", False, "X", 255, 0))
    plan = plan_fields(fs, None)
    # keys always in, then X1 and X2 (3+10+200+200 = 413), X3 no room.
    assert plan.fields == ["MANDT", "X1", "KEY2", "X2"]
    reasons = dict(plan.omitted)
    assert "X3" in reasons and "no room" in reasons["X3"]
    assert "floating" in reasons["FL"]
    assert "RAW" in reasons["RAW"]


def test_pack_options_never_splits_tokens_or_literals():
    where = "MATNR LIKE 'A%' AND MTART = 'FERT' AND MAKTX = 'a literal with spaces inside it' AND " * 3
    rows = pack_options(where.strip().rstrip("AND").strip())
    for r in rows:
        assert len(r["TEXT"]) <= 72
        assert r["TEXT"].count("'") % 2 == 0, "a literal was split"
    joined = " ".join(r["TEXT"] for r in rows)
    assert "'a literal with spaces inside it'" in joined


def test_pack_options_list_is_anded():
    rows = pack_options(["MTART = 'FERT'", "WERKS = '1000'"])
    assert " ".join(r["TEXT"] for r in rows) == "( MTART = 'FERT' ) AND ( WERKS = '1000' )"
    assert pack_options(None) == []
    with pytest.raises(ReadTableError):
        pack_options("X = '" + "a" * 80 + "'")


def test_parse_rows_uses_backend_offsets_and_types():
    fields_out = [
        {"FIELDNAME": "MATNR", "OFFSET": "000000", "LENGTH": "000018", "TYPE": "C"},
        {"FIELDNAME": "ERSDA", "OFFSET": "000018", "LENGTH": "000008", "TYPE": "D"},
        {"FIELDNAME": "NTGEW", "OFFSET": "000026", "LENGTH": "000010", "TYPE": "P"},
        {"FIELDNAME": "CNT", "OFFSET": "000036", "LENGTH": "000011", "TYPE": "I"},
    ]
    data = [{"WA": "MAT-1".ljust(18) + "20240115" + "   12,500-" + "        42 "}]
    cols, rows = parse_rows(data, fields_out)
    assert [c["name"] for c in cols] == ["MATNR", "ERSDA", "NTGEW", "CNT"]
    assert rows == [["MAT-1", "2024-01-15", "-12.500", 42]]


def test_convert_value_edge_cases():
    assert convert_value("00000000", "D") == ""
    assert convert_value("123456", "T") == "12:34:56"
    assert convert_value("  1.234,56 ", "P") == "1234.56"
    assert convert_value("       12-", "I") == -12
    assert convert_value("0042", "N") == "0042"


def test_denied_patterns():
    assert denied("USR02", ["USR02"])
    assert denied("PA0002", ["PA0*"])
    assert not denied("MARA", ["PA0*", "USR*"])


class FakeSap:
    """Replays RFC_READ_TABLE: raises DATA_BUFFER_EXCEEDED while 3+ fields are asked."""

    def __init__(self):
        self.calls = []

    def call_known(self, fm, **params):
        self.calls.append((fm, params))
        names = [f["FIELDNAME"] for f in params["FIELDS"]]
        if len(names) > 2:
            raise ABAPException("DATA_BUFFER_EXCEEDED")
        return {
            "FIELDS": [{"FIELDNAME": n, "OFFSET": str(i * 10).zfill(6), "LENGTH": "000010",
                        "TYPE": "C"} for i, n in enumerate(names)],
            "DATA": [{"WA": "".join(f"{n}-{r}".ljust(10) for n in names)}
                     for r in range(params["ROWCOUNT"])],
        }

    def run(self, fn, *args):
        return fn(*args)


class FakeDdic:
    def structure(self, name):
        return {"name": name, "fields": fields(("K", True, "C", 10, 0), ("A", False, "C", 10, 0),
                                              ("B", False, "C", 10, 0))}


def test_read_table_tool_retries_on_buffer_exceeded_and_pages(monkeypatch):
    monkeypatch.setattr(config, "READ_TABLE_DENY", [])
    sap = FakeSap()
    tool = ReadTableTool(sap, FakeDdic(), ttl=0)
    result = tool.read("T1", None, "K = '1'", max_rows=2)
    assert [c["name"] for c in result["columns"]] == ["K", "A"]
    assert result["row_count"] == 2 and result["truncated"] is True
    assert "omitted B" in result["note"] and "skip=2" in result["note"]
    assert len(sap.calls) == 2
    assert sap.calls[-1][1]["OPTIONS"] == [{"TEXT": "K = '1'"}]


def test_read_table_tool_policy(monkeypatch):
    monkeypatch.setattr(config, "READ_TABLE_DENY", ["T1"])
    with pytest.raises(ReadTableError, match="policy"):
        ReadTableTool(FakeSap(), FakeDdic(), ttl=0).read("T1")


def test_read_table_tool_drops_guessed_fields_and_rejects_guessed_where(monkeypatch):
    monkeypatch.setattr(config, "READ_TABLE_DENY", [])
    tool = ReadTableTool(FakeSap(), FakeDdic(), ttl=0)
    result = tool.read("T1", ["K", "AA", "ZZZZ"], "K = '1'", max_rows=2)
    assert [c["name"] for c in result["columns"]] == ["K"]
    assert "omitted AA: AA is not a field (did you mean A" in result["note"]
    assert "fields of the table: K, A, B" in result["note"]
    with pytest.raises(ReadTableError, match="WHERE clause uses GJAHR is not a field.*Fields of the table"):
        tool.read("T1", ["K"], "GJAHR = '2005' AND K = '1'", max_rows=2)
