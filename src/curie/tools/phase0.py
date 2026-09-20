"""Phase 0 spikes against the real backend, without the LLM or the browser.

    python -m curie.tools.phase0            # read T000, MARA structure, an FM interface,
                                # syntax-check and run ZAI_TEST_HELLO (sync)
    python -m curie.tools.phase0 --job      # also schedule it as a job (needs curie.py
                                # running to receive the list; otherwise the
                                # job's delivery call fails and TBTCO is read)
    python -m curie.tools.phase0 --table MARA --where "MTART = 'FERT'"

Prints what the backend actually returns so that interface differences show
up here and not in a chat session.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time

from curie import config
from curie.rfc.client import SapClient
from curie.sap_tools.abap_exec import AbapExecutor
from curie.sap_tools.abap_source import prepare_source
from curie.sap_tools.ddic import DdicCache
from curie.sap_tools.function_interface import FunctionInterfaceCache
from curie.sap_tools.read_table import ReadTableTool, read_rows, rows_as_dicts
from curie.sessions.model import AbapRun, SapIdentity, Session

log = logging.getLogger("phase0")

HELLO = """\
REPORT zai_test_hello.
DATA: lt_t000 LIKE t000 OCCURS 0 WITH HEADER LINE.
WRITE: / 'Hello from Curie'.
SELECT * FROM t000 INTO TABLE lt_t000 UP TO 5 ROWS.
LOOP AT lt_t000.
  WRITE: / lt_t000-mandt, lt_t000-mtext.
ENDLOOP.
"""


def show(title: str, obj) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str)[:4000])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--table", default="T000")
    parser.add_argument("--where", default=None)
    parser.add_argument("--fields", default=None, help="comma separated")
    parser.add_argument("--structure", default="MARA")
    parser.add_argument("--fm", default="RFC_READ_TABLE")
    parser.add_argument("--job", action="store_true", help="also run the hello program as a job")
    parser.add_argument("--no-run", action="store_true", help="skip the ABAP execution spikes")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    sap = SapClient(config.SAP_RFC, size=1)
    info = sap.system_info()
    if not config.SAP_RELEASE:
        config.SAP_RELEASE = config.release_from_info(info)
    show("RFC_SYSTEM_INFO", {k: info[k] for k in ("RFCSYSID", "RFCSAPRL", "RFCKERNRL",
                                                   "RFCCHARTYP", "RFCHOST", "RFCDBSYS")})
    print("profile", config.SAP_RELEASE, "SAP_LANGU", config.SAP_LANGU)

    # -- raw interfaces, as the backend has them ------------------------------ #
    for fm in ("RFC_GET_STRUCTURE_DEFINITION_P", "RFC_GET_FUNCTION_INTERFACE_P"):
        show(f"interface {fm}", sap.interface(fm))

    # -- structure ------------------------------------------------------------- #
    ddic = DdicCache(sap)
    t0 = time.monotonic()
    struct = ddic.structure_as_dict(args.structure)
    print(f"\n{args.structure}: {len(struct['fields'])} fields in {time.monotonic() - t0:.2f}s")
    for f in struct["fields"][:12]:
        print(f"  {f['name']:<12} key={f['key']!s:<5} {f['type']:<6} {f['length']:>4} {f['decimals']:>2}  {f['text']}")
    texts = sum(1 for f in struct["fields"] if f["text"])
    keys = [f["name"] for f in struct["fields"] if f["key"]]
    print(f"  ... texts for {texts}/{len(struct['fields'])} fields; keys {keys}")

    # -- function interface ------------------------------------------------------ #
    fi = FunctionInterfaceCache(sap)
    iface = fi.interface(args.fm)
    print(f"\n{args.fm}: remote={iface['remote_enabled']} {iface['short_text']!r}")
    for p in iface["parameters"]:
        print(f"  {p['class']:<8} {p['name']:<16} {p['structure']:<12} {p['field']:<12} {p['type']:<2} "
              f"{p['length']:>4} opt={p['optional']!s:<5} {p['text']}")
    print("  exceptions:", iface["exceptions"])

    # -- table read ---------------------------------------------------------------- #
    tool = ReadTableTool(sap, ddic, ttl=0)
    fields = [f.strip() for f in args.fields.split(",")] if args.fields else None
    t0 = time.monotonic()
    result = tool.read(args.table, fields, args.where, max_rows=5)
    print(f"\nread {args.table}: {result['row_count']} rows, {len(result['columns'])} columns "
          f"in {time.monotonic() - t0:.2f}s; note: {result['note']!r}")
    names = [c["name"] for c in result["columns"]]
    print("  " + " | ".join(names[:10]))
    for row in result["rows"]:
        print("  " + " | ".join(str(v) for v in row[:10]))

    if args.no_run:
        sap.close()
        return 0

    # -- syntax check and synchronous run ------------------------------------------ #
    asyncio.run(run_spikes(sap, args.job))
    sap.close()
    return 0


async def run_spikes(sap: SapClient, job: bool) -> None:
    from curie.sessions.manager import SessionManager

    manager = SessionManager()
    manager.bind_loop(asyncio.get_running_loop())
    executor = AbapExecutor(sap, manager)
    print("\nT_SOURCE width", executor.source_line_length(), "effective", executor.effective_width())
    session = Session(id="PHASE0", sap=SapIdentity(sysid="?", client=config.SAP_RFC["client"],
                                                    user=config.SAP_RFC["user"]))
    lines, problems = prepare_source(HELLO, "ZAI_PHASE0", executor.effective_width())
    print("prepared", len(lines), "lines; problems:", problems)

    run = AbapRun(run_id="P0", progname="ZAI_PHASE0", source="\n".join(lines))
    syntax = await executor.check(session, run, lines)
    show("mode C (syntax check)", syntax)

    broken = lines + ["WRITE lv_undefined."]
    show("mode C with an error", await executor.check(session, run, broken))

    old_mode = config.ABAP_EXEC_MODE
    config.ABAP_EXEC_MODE = "sync"
    t0 = time.monotonic()
    result = await executor.execute(session, run, lines)
    print(f"mode S took {time.monotonic() - t0:.2f}s")
    show("mode S (synchronous run)", result)

    if job:
        config.ABAP_EXEC_MODE = "job"
        config.ABAP_RUN_TIMEOUT = 40
        run2 = AbapRun(run_id="P0J", progname="ZAI_PHASE0J", source="\n".join(lines))
        lines2, _ = prepare_source(HELLO, run2.progname, executor.effective_width())
        t0 = time.monotonic()
        result = await executor.execute(session, run2, lines2,
                                        on_started=lambda r: print("job scheduled:", r.job))
        print(f"mode X took {time.monotonic() - t0:.2f}s")
        show("mode X (job)", result)
        if run2.job:
            name, count = run2.job.split("/")
            cols, rows = read_rows(sap, "TBTCO", ["JOBNAME", "JOBCOUNT", "STATUS", "SDLUNAME",
                                                  "AUTHCKNAM", "STRTDATE", "STRTTIME", "ENDDATE", "ENDTIME"],
                                   f"JOBNAME = '{name}' AND JOBCOUNT = '{count}'", 1)
            show("TBTCO", rows_as_dicts(cols, rows))
    config.ABAP_EXEC_MODE = old_mode

    cols, rows = read_rows(sap, "TRDIR", ["NAME", "CNAM", "CDAT", "SUBC"],
                           f"NAME LIKE '{config.ABAP_PROG_PREFIX}%'", 50)
    show("TRDIR leftovers", rows_as_dicts(cols, rows))


if __name__ == "__main__":
    sys.exit(main())
