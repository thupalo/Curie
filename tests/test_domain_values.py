import pytest

from curie.sap_tools.domain_values import DomainValues
from curie.sap_tools.read_table import ReadTableError


class FakeSap:
    """Answers read_rows() by table name from canned rows."""

    def __init__(self, data):
        self.data = data
        self.calls = []

    def call_known(self, fm, **params):
        table = params["QUERY_TABLE"]
        names = [f["FIELDNAME"] for f in params["FIELDS"]]
        where = " ".join(o["TEXT"] for o in params["OPTIONS"])
        self.calls.append((table, where))
        rows = [r for r in self.data.get(table, []) if all(f"'{v}'" in where for v in r.get("_match", []))]
        width = 40
        return {
            "FIELDS": [{"FIELDNAME": n, "OFFSET": str(i * width).zfill(6), "LENGTH": str(width).zfill(6),
                        "TYPE": "C"} for i, n in enumerate(names)],
            "DATA": [{"WA": "".join(str(r.get(n, "")).ljust(width) for n in names)} for r in rows],
        }


DATA = {
    "DD03L": [{"_match": ["ZUMOWYL", "STATUS"], "FIELDNAME": "STATUS", "ROLLNAME": "ZSTATUS",
               "DOMNAME": "ZSTATUS", "CHECKTABLE": "", "DATATYPE": "NUMC", "LENG": "2"}],
    "DD01L": [{"_match": ["ZSTATUS"], "DOMNAME": "ZSTATUS", "DATATYPE": "NUMC", "LENG": "2", "ENTITYTAB": ""}],
    "DD07L": [{"_match": ["ZSTATUS"], "VALPOS": "0002", "DOMVALUE_L": "14", "DOMVALUE_H": "19"},
              {"_match": ["ZSTATUS"], "VALPOS": "0001", "DOMVALUE_L": "10", "DOMVALUE_H": ""}],
    # 4.5B shape: DD07T has positions and texts only
    "DD07T": [{"_match": ["ZSTATUS", "L"], "VALPOS": "0002", "DDTEXT": "aktywna"},
              {"_match": ["ZSTATUS", "L"], "VALPOS": "0001", "DDTEXT": "nowa"}],
}


def test_field_to_domain_values_with_texts():
    sap = FakeSap(DATA)
    tool = DomainValues(sap, langu="L")
    out = tool.values("zumowyl", "status")
    assert out["domain"] == "ZSTATUS" and out["data_element"] == "ZSTATUS" and out["type"] == "NUMC"
    assert out["values"] == [{"low": "10", "high": "", "text": "nowa"},
                             {"low": "14", "high": "19", "text": "aktywna"}]
    assert out["note"] == ""
    # cached: no further calls for the same key
    n = len(sap.calls)
    tool.values("ZUMOWYL", "STATUS")
    assert len(sap.calls) == n


def test_domain_without_values_and_bad_input():
    sap = FakeSap({"DD01L": [{"_match": ["KUNNR"], "DOMNAME": "KUNNR", "DATATYPE": "CHAR", "LENG": "10",
                              "ENTITYTAB": "KNA1"}]})
    out = DomainValues(sap, langu="L").values(domain="KUNNR")
    assert out["values"] == [] and out["value_table"] == "KNA1"
    assert "no fixed values" in out["note"] and "KNA1" in out["note"]
    with pytest.raises(ReadTableError):
        DomainValues(sap, langu="L").values()
    with pytest.raises(ReadTableError, match="not found"):
        DomainValues(FakeSap({}), langu="L").values("MARA", "NOPE")
