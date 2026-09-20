"""Checking and running generated ABAP through Z_AI_PROG_CHECK_AND_EXECUTE.

Modes of the function module (see abap/Z_AI_PROG_CHECK_AND_EXECUTE.abap):
    C  syntax check only
    X  syntax check, INSERT REPORT, background job with ZAI_RUNNER as the
       end user; the list arrives later through Z_AI_LIST_DELIVER
    S  syntax check, INSERT REPORT, synchronous run, list in T_LIST
    D  DELETE REPORT

The executor keeps no state of its own; the AbapRun on the session does.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date

from curie import config
from curie.sap_tools.read_table import read_rows, rows_as_dicts
from curie.sapnwrfc720 import ABAPException, RFCError
from curie.sessions.model import AbapRun, Session

log = logging.getLogger(__name__)

JOB_STATUS_TEXT = {
    "P": "scheduled", "S": "released", "Y": "ready", "R": "running",
    "F": "finished", "A": "cancelled (runtime error, see ST22)", "Z": "put active",
}


class AbapExecutor:
    def __init__(self, sap, manager, fm: str | None = None):
        self.sap = sap
        self.manager = manager
        self.fm = (fm or config.ABAP_CHECK_FM).upper()
        self._line_length: int | None = None

    # -- interface facts ------------------------------------------------------- #

    def source_line_length(self) -> int:
        """Line width of T_SOURCE in the backend (72 on 4.5B, 255 on ECC)."""
        if self._line_length is None:
            try:
                self._line_length = self.sap.table_line_length(self.fm, "T_SOURCE") or 72
            except Exception as exc:  # noqa: BLE001
                log.warning("cannot read %s interface: %s", self.fm, exc)
                self._line_length = 72
        return self._line_length

    def effective_width(self) -> int:
        return min(config.profile()["line_width"], self.source_line_length())

    # -- calls ------------------------------------------------------------------- #

    def _call(self, mode: str, run: AbapRun, lines: list[str], user: str = "",
              session_id: str = "") -> dict:
        return self.sap.call(
            self.fm,
            I_SESSION_ID=session_id, I_RUN_ID=run.run_id, I_PROGNAME=run.progname,
            I_USER=user, I_MODE=mode,
            T_SOURCE=[{"LINE": line} for line in lines], T_LIST=[],
        )

    async def check(self, session: Session, run: AbapRun, lines: list[str]) -> dict:
        """Syntax check; returns {"ok", "message", "line", "word"}."""
        try:
            out = await self.sap.run(self._call, "C", run, lines, "", session.id)
        except ABAPException as exc:
            # A declared exception of the check FM (PROGNAME_INVALID, ...):
            # not a fault of the generated program.
            return {"ok": False, "message": f"{self.fm} raised {exc.key} {exc.message}".strip(),
                    "line": 0, "word": "", "infra": True}
        except RFCError as exc:
            # The FM dumped or the connection broke: the program was not checked.
            return {"ok": False, "message": f"{self.fm} failed: {exc.key} {exc.message}".strip(),
                    "line": 0, "word": "", "infra": True}
        return _syntax(out)

    async def execute(self, session: Session, run: AbapRun, lines: list[str],
                      on_started=None) -> dict:
        """Run the program; returns {"ok", "rc", "message", "lines"}.

        on_started(run) is called once the program is on its way (job
        scheduled, or the synchronous call sent), so the UI can show it.
        """
        mode = config.ABAP_EXEC_MODE
        run.mode = mode
        if mode == "sync":
            if on_started:
                on_started(run)
            return await self._execute_sync(session, run, lines)
        return await self._execute_job(session, run, lines, on_started)

    async def _execute_sync(self, session: Session, run: AbapRun, lines: list[str]) -> dict:
        try:
            out = await self.sap.run(self._call, "S", run, lines, session.sap.user, session.id)
        except ABAPException as exc:
            return _failed(f"{exc.key} {exc.message}".strip())
        except RFCError as exc:
            return _failed(f"runtime error {exc.key}: {exc.message}".strip())
        syntax = _syntax(out)
        if not syntax["ok"]:
            return _failed(f"syntax error at line {syntax['line']}: {syntax['message']}")
        list_lines = [_line(r) for r in out.get("T_LIST", [])]
        return {"ok": True, "rc": 0, "message": "", "lines": list_lines}

    async def _execute_job(self, session: Session, run: AbapRun, lines: list[str],
                           on_started=None) -> dict:
        loop = asyncio.get_running_loop()
        run.delivery = loop.create_future()
        try:
            out = await self.sap.run(self._call, "X", run, lines, session.sap.user, session.id)
        except ABAPException as exc:
            run.delivery = None
            return _failed(f"{exc.key} {exc.message}".strip())
        except RFCError as exc:
            run.delivery = None
            return _failed(f"RFC error: {exc.key} {exc.message}".strip())
        syntax = _syntax(out)
        if not syntax["ok"]:
            run.delivery = None
            return _failed(f"syntax error at line {syntax['line']}: {syntax['message']}")
        jobname = str(out.get("E_JOBNAME", "")).strip()
        jobcount = str(out.get("E_JOBCOUNT", "")).strip()
        run.job = f"{jobname}/{jobcount}" if jobname else ""
        if on_started:
            on_started(run)
        try:
            result = await asyncio.wait_for(run.delivery, timeout=config.ABAP_RUN_TIMEOUT)
        except asyncio.TimeoutError:
            status = await self.job_status(jobname, jobcount)
            text = JOB_STATUS_TEXT.get(status or "", "unknown")
            if status in ("A",):
                run.delivery = None
                await self.delete(run.progname)
                return _failed(f"job {run.job} was cancelled: {text}")
            if status in ("F",):
                run.delivery = None
                await self.delete(run.progname)
                return _failed(f"job {run.job} finished but delivered no list "
                               "(check the RFC destination SAP_AI from the job log)")
            # Still on its way: leave the future so a late delivery is shown.
            return {"ok": False, "rc": -1, "lines": [], "pending": True,
                    "message": f"no result after {int(config.ABAP_RUN_TIMEOUT)} s; "
                               f"job {run.job} is {text}"}
        except asyncio.CancelledError:
            raise
        result = dict(result)
        result.setdefault("lines", [])
        await self.delete(run.progname)
        return result

    async def abort(self, run: AbapRun) -> str:
        """Stop a running job (BP_JOB_ABORT) and remove the program.

        Returns a short text for the user. A backend without a remote-enabled
        BP_JOB_ABORT (possible on 4.5B) leaves the job to finish on its own;
        its late delivery is then ignored because the run is no longer waiting.
        """
        text = "stopped"
        if run.mode == "job" and run.job and "/" in run.job:
            jobname, jobcount = run.job.split("/", 1)
            try:
                await self.sap.run(self._abort_job, jobname, jobcount)
                text = f"job {run.job} aborted"
            except ABAPException as exc:
                text = f"job {run.job} could not be aborted ({exc.key}); it will finish on its own"
            except (RFCError, ValueError) as exc:
                log.warning("abort %s: %s", run.job, exc)
                text = f"job {run.job} could not be aborted; it will finish on its own"
        fut = run.delivery
        if fut is not None and not fut.done():
            fut.cancel()
        run.delivery = None
        await self.delete(run.progname)
        return text

    def _abort_job(self, jobname: str, jobcount: str) -> None:
        """BP_JOB_ABORT where it is remote-enabled (ECC); on 4.5B it is not,
        so fall back to the XBP interface: BAPI_XMI_LOGON, BAPI_XBP_JOB_ABORT,
        BAPI_XMI_LOGOFF. Raises ABAPException/RFCError when neither works.
        Blocking; runs in the client pool's executor."""
        try:
            self.sap.call("BP_JOB_ABORT", JOBNAME=jobname, JOBCOUNT=jobcount)
            return
        except RFCError as exc:
            if exc.key != "CALL_FUNCTION_NOT_REMOTE":
                raise
        with self.sap.acquire() as client:
            # XBP 1.0 is what 4.5B knows; newer releases accept 2.0/3.0 too.
            ret = {}
            for version in ("1.0", "2.0", "3.0"):
                ret = client.call("BAPI_XMI_LOGON", EXTCOMPANY="CURIE", EXTPRODUCT="CURIE",
                                  INTERFACE="XBP", VERSION=version).get("RETURN", {})
                if str(ret.get("TYPE", "")).strip() not in ("E", "A"):
                    break
            _bapi_ok(ret, "BAPI_XMI_LOGON")
            try:
                ret = client.call("BAPI_XBP_JOB_ABORT", JOBNAME=jobname, JOBCOUNT=jobcount,
                                  EXTERNAL_USER_NAME="CURIE").get("RETURN", {})
                _bapi_ok(ret, "BAPI_XBP_JOB_ABORT")
            finally:
                try:
                    client.call("BAPI_XMI_LOGOFF", INTERFACE="XBP")
                except (ABAPException, RFCError):
                    pass

    async def delete(self, progname: str) -> None:
        if not progname:
            return
        run = AbapRun(run_id="", progname=progname, source="")
        try:
            await self.sap.run(self._call, "D", run, [], "", "")
        except (ABAPException, RFCError) as exc:
            log.warning("delete %s: %s", progname, exc)

    async def job_status(self, jobname: str, jobcount: str) -> str | None:
        if not jobname:
            return None
        try:
            cols, rows = await self.sap.run(
                read_rows, self.sap, "TBTCO", ["JOBNAME", "JOBCOUNT", "STATUS"],
                f"JOBNAME = '{jobname}' AND JOBCOUNT = '{jobcount}'", 1)
        except Exception as exc:  # noqa: BLE001
            log.warning("TBTCO for %s/%s: %s", jobname, jobcount, exc)
            return None
        for r in rows_as_dicts(cols, rows):
            return str(r.get("STATUS", "")).strip() or None
        return None

    async def sweep_stale(self) -> int:
        """Delete ZAI_* programs created before today (leftovers of crashes)."""
        prefix = config.ABAP_PROG_PREFIX
        today = date.today().strftime("%Y%m%d")
        try:
            cols, rows = await self.sap.run(
                read_rows, self.sap, "TRDIR", ["NAME", "CDAT"],
                f"NAME LIKE '{prefix}%' AND CDAT < '{today}'", 200)
        except Exception as exc:  # noqa: BLE001
            log.warning("sweep: cannot read TRDIR: %s", exc)
            return 0
        count = 0
        generated = re.compile(re.escape(prefix) + r"[0-9A-F]{8}$")
        for r in rows_as_dicts(cols, rows):
            name = str(r.get("NAME", "")).strip()
            # Only names Curie generates (prefix + 8 hex): ZAI_RUNNER and
            # ZAI_START share the prefix and must stay.
            if generated.match(name):
                await self.delete(name)
                count += 1
        if count:
            log.info("sweep: deleted %d stale %s* program(s)", count, prefix)
        return count


def _bapi_ok(ret: dict, where: str) -> None:
    """Raise ABAPException for a BAPIRET2/BAPIRETURN of type E or A."""
    if str(ret.get("TYPE", "")).strip() in ("E", "A"):
        raise ABAPException(f"{where}", str(ret.get("MESSAGE", "")).strip())


def _line(row: dict) -> str:
    """The text of a one-field line row, whatever the field is called."""
    value = row.get("LINE") if "LINE" in row else next(iter(row.values()), "")
    return str(value or "").rstrip()


def _syntax(out: dict) -> dict:
    ok = str(out.get("E_SYNTAX_OK", "")).strip() == "X"
    return {
        "ok": ok,
        "message": str(out.get("E_MESSAGE", "")).strip(),
        "line": int(out.get("E_LINE", 0) or 0),
        "word": str(out.get("E_WORD", "")).strip(),
    }


def _failed(message: str) -> dict:
    return {"ok": False, "rc": -1, "message": message, "lines": []}
