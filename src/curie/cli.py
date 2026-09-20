"""Curie: LLM chat for SAP over RFC -- the process entry point.

One process holds everything:

* RFC server threads (NW RFC SDK 7.20) registered as PROGRAM_ID at the SAP
  gateway, serving Z_AI_SESSION_OPEN / Z_AI_HEARTBEAT / Z_AI_SESSION_CLOSE /
  Z_AI_LIST_DELIVER (handlers/session.py) plus the connectivity checks;
* a pool of client connections on the same SDK for RFC_READ_TABLE, the
  RFC_GET_*_P reads and Z_AI_PROG_CHECK_AND_EXECUTE;
* uvicorn/FastAPI on the main thread's asyncio loop for the Chrome window.

Usage:
    curie             # serve until Ctrl+C
    curie --check     # verify SAP logon, every FM interface and the LLM
    curie --selftest  # with Curie running: round trips from inside SAP
    curie --trace <session id prefix> | --trace --newest [eval|user]   # a session log as a timeline
    python -m curie.tools.rfc_dev           # the same, restarted when a source file changes

The maintenance commands (--review-knowledge, --consolidate, --eval,
--archive, --housekeeping, --cache, --purge-knowledge) are described in
docs/11-operations.md.
"""
from __future__ import annotations

import argparse
import asyncio
import time
import logging
import os
import sys
from pathlib import Path

from curie import config

log = logging.getLogger("curie")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    if config.LOG_FILE:
        from logging.handlers import RotatingFileHandler

        path = os.path.join(config.BASE_DIR, config.LOG_FILE)
        handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5,
                                      encoding="utf-8")
        handler.setFormatter(logging.root.handlers[0].formatter)
        logging.root.addHandler(handler)


def report_llm_check(result: dict) -> bool:
    """Log an LLMClient.check() result; True when the model answered."""
    if result["ok"]:
        log.info("LLM ok: %s at %s replied %r", result["model"], result["base_url"], result["reply"])
        return True
    log.error("LLM FAILED: %s", result["error"])
    if result.get("served"):
        log.error("models served at %s: %s -- set LLM_MODEL to one of them and restart",
                  result["base_url"], ", ".join(result["served"]))
    elif "does not exist" in result["error"] or "NotFound" in result["error"]:
        log.error("the endpoint does not list its models; check the server's served model name")
    return False


def detect_release(sap) -> dict:
    info = sap.system_info()
    detected = config.release_from_info(info)
    if not config.SAP_RELEASE:
        config.SAP_RELEASE = detected
    log.info("backend %s/%s release %s (%s, %s) -> profile %s%s",
             info.get("RFCSYSID"), config.SAP_RFC["client"], info.get("RFCSAPRL"),
             info.get("RFCPROTO"), info.get("RFCCHARTYP"), config.SAP_RELEASE,
             "" if config.SAP_RELEASE == detected else f" (detected {detected}, .env wins)")
    return info


def build(sap):
    """Wire the objects together; returns (manager, orchestrator, flow, executor, app)."""
    from curie.llm.abap_flow import AbapFlow
    from curie.llm.client import LLMClient
    from curie.llm.orchestrator import Orchestrator
    from curie.llm.tools import ToolBox
    from curie.sap_tools.abap_exec import AbapExecutor
    from curie.sap_tools.ddic import DdicCache
    from curie.sap_tools.function_interface import FunctionInterfaceCache
    from curie.sap_tools.read_table import ReadTableTool
    from curie.sessions.manager import SessionManager
    from curie.web.app import create_app

    from curie.memory import Knowledge, KnowledgeStore
    from curie.sap_tools.domain_values import DomainValues

    manager = SessionManager()
    ddic = DdicCache(sap)
    fi = FunctionInterfaceCache(sap)
    knowledge = None
    if config.KNOWLEDGE_ENABLED:
        store = KnowledgeStore(config.KNOWLEDGE_DIR, config.KNOWLEDGE_MAX_NOTES,
                               config.KNOWLEDGE_MAX_CHARS, config.KNOWLEDGE_EXEMPLARS,
                               max_insights=config.KNOWLEDGE_MAX_INSIGHTS)
        store.table_exists = ddic.exists     # /remember tags only real tables
        knowledge = Knowledge(store, config.KNOWLEDGE_BUILD_DIR)
        log.info("knowledge: %d note(s) in %s; bundle %s", len(store), config.KNOWLEDGE_DIR,
                 ", ".join(f"{k}={v}" for k, v in knowledge.bundle.summary().items() if v))
    toolbox = ToolBox(ReadTableTool(sap, ddic), ddic, fi, sap, DomainValues(sap), knowledge)
    llm = LLMClient()
    executor = AbapExecutor(sap, manager)
    orchestrator = Orchestrator(manager, llm, toolbox, knowledge)
    from curie.memory.audit_memo import AuditMemo
    flow = AbapFlow(manager, executor, llm, orchestrator, knowledge,
                    audit_memo=AuditMemo() if config.ABAP_AUDIT_MEMO else None)
    orchestrator.flow = flow
    app = create_app(manager, orchestrator, flow, executor)
    return manager, orchestrator, flow, executor, app, llm


def check() -> int:
    """Verify logon, every function module interface Curie relies on, and the LLM."""
    from curie import handlers
    from curie import sapnwrfc720 as sap_sdk
    from curie.rfc.client import SapClient

    failed = 0
    log.info("NW RFC SDK %s from %s", sap_sdk.sdk_version(), sap_sdk.SDK_HOME)
    for problem in config.problems():
        log.error("config: %s", problem)
        failed += 1

    sap = SapClient(config.SAP_RFC, size=1)
    try:
        info = detect_release(sap)
        log.info("SAP_LANGU=%s (logon language %s)", config.SAP_LANGU, config.SAP_RFC["lang"])
        del info
        for fm in ("RFC_READ_TABLE", "RFC_GET_STRUCTURE_DEFINITION_P",
                   "RFC_GET_FUNCTION_INTERFACE_P", config.ABAP_CHECK_FM):
            try:
                iface = sap.interface(fm)
                names = ", ".join(f"{p['name']}:{p['direction'][0]}" for p in iface)
                log.info("interface ok: %-30s %s", fm, names)
            except sap_sdk.RFCError as exc:
                failed += 1
                log.error("interface FAILED: %s -- %s", fm, exc)
        for name in handlers.HANDLERS:
            optional = " (plugin)" if name in handlers.OPTIONAL else ""
            try:
                sap.interface(name)
                log.info("served FM ok:  %-30s <- %s%s", name,
                         handlers.HANDLERS[name].__module__, optional)
            except sap_sdk.RFCError as exc:
                if optional:
                    log.warning("served FM missing: %s%s -- %s (the plugin would start "
                                "disabled)", name, optional, exc)
                    continue
                failed += 1
                log.error("served FM FAILED: %s -- %s", name, exc)
        if config.CHAT_PLUGIN:
            log.info("chat plugin: models %s, max %d handle(s), READ wait %ss",
                     ", ".join(f"{k}={v}" for k, v in config.chat_models().items()),
                     config.CHAT_MAX_HANDLES, config.CHAT_READ_WAIT)
            if config.CHAT_MAX_HANDLES >= config.RFC_REG_COUNT:
                log.warning("CHAT_MAX_HANDLES (%d) >= RFC_REG_COUNT (%d): chats could "
                            "occupy every registration and starve Curie's own sessions",
                            config.CHAT_MAX_HANDLES, config.RFC_REG_COUNT)
        if config.ABAP_CHECK_FM in sap.param_names(config.ABAP_CHECK_FM) or True:
            width = sap.table_line_length(config.ABAP_CHECK_FM, "T_SOURCE")
            log.info("T_SOURCE line length %s, profile width %s", width,
                     config.profile()["line_width"])
    except sap_sdk.RFCError as exc:
        failed += 1
        log.error("SAP: %s", exc)
    finally:
        sap.close()

    async def llm_check():
        from curie.llm.client import LLMClient
        return 0 if report_llm_check(await LLMClient().check()) else 1

    failed += asyncio.run(llm_check())
    log.info("would register PROGRAM_ID=%s (%sx) at %s/%s and serve %s",
             config.SAP_RFC_SERVER["PROGRAM_ID"], config.RFC_REG_COUNT,
             config.SAP_RFC_SERVER["GWHOST"], config.SAP_RFC_SERVER["GWSERV"],
             config.WEB_PUBLIC_URL)
    return 1 if failed else 0


SELFTEST_ABAP = """\
REPORT zai_rfc_selftest.
DATA: lv_sess(32), lv_url(255), lv_state(16), lv_text(80),
      lv_msg(80), lv_hb TYPE i, lv_i TYPE i.
DO {rounds} TIMES.
  lv_i = sy-index.
  CLEAR: lv_sess, lv_url, lv_msg, lv_state.
  CALL FUNCTION 'Z_AI_SESSION_OPEN' DESTINATION '{dest}'
    EXPORTING i_prompt = 'selftest'
    IMPORTING e_session_id = lv_sess e_url = lv_url
              e_heartbeat_sec = lv_hb
    EXCEPTIONS communication_failure = 1 MESSAGE lv_msg
               system_failure = 2 MESSAGE lv_msg
               OTHERS = 3.
  IF sy-subrc <> 0.
    WRITE: / 'FAIL', lv_i, 'OPEN rc', sy-subrc, lv_msg.
  ELSE.
    CALL FUNCTION 'Z_AI_HEARTBEAT' DESTINATION '{dest}'
      EXPORTING i_session_id = lv_sess
      IMPORTING e_state = lv_state e_status_text = lv_text
      EXCEPTIONS OTHERS = 1.
    IF sy-subrc <> 0.
      WRITE: / 'FAIL', lv_i, 'HEARTBEAT rc', sy-subrc.
    ELSE.
      WRITE: / 'OK', lv_i, lv_state, lv_hb, lv_url(80).
    ENDIF.
    CALL FUNCTION 'Z_AI_SESSION_CLOSE' DESTINATION '{dest}'
      EXPORTING i_session_id = lv_sess i_reason = 'SELFTEST'
      EXCEPTIONS OTHERS = 1.
  ENDIF.
ENDDO.
"""


def selftest(dest: str, rounds: int) -> int:
    """Call the served function modules from inside SAP, through the SM59
    destination, while Curie runs. Every round must succeed; a failure with
    "not found" means another server is registered under the Program ID.
    Uses RFC_ABAP_INSTALL_AND_RUN, which needs S_DEVELOP for the RFC user.
    """
    from curie import sapnwrfc720 as sap_sdk
    from curie.rfc.client import SapClient

    source = SELFTEST_ABAP.replace("{rounds}", str(rounds)).replace("{dest}", dest)
    sap = SapClient(config.SAP_RFC, size=1)
    chat_rc = 0
    try:
        out = sap.call_known("RFC_ABAP_INSTALL_AND_RUN", MODE="F",
                             PROGRAMNAME="ZAI_RFC_SELFTEST",
                             PROGRAM=[{"LINE": line} for line in source.splitlines()],
                             WRITES=[])
        if config.CHAT_PLUGIN:
            from curie.chat import selftest as chat_selftest
            chat_rc = chat_selftest.run(sap, dest)
    except (sap_sdk.RFCError, sap_sdk.ABAPException) as exc:
        log.error("selftest could not run: %s", exc)
        return 2
    finally:
        sap.close()
    if str(out.get("ERRORMESSAGE", "")).strip():
        log.error("selftest: %s", out["ERRORMESSAGE"])
        return 2
    failed = 0
    messages: list[str] = []
    for row in out.get("WRITES", []):
        text = str(next(iter(row.values()))).strip()
        if text.startswith("FAIL"):
            failed += 1
            log.error("%s", text)
        elif text:
            messages.append(text)
            log.info("%s", text)
    if failed:
        joined = " ".join(messages).lower()
        progid = config.SAP_RFC_SERVER["PROGRAM_ID"]
        if "not registered" in joined:
            log.error("%d of %d rounds failed: nothing is registered as %s -- Curie is "
                      "not running (or registered at another gateway)", failed, rounds, progid)
        elif "not found" in joined or "fu_not_found" in joined:
            log.error("%d of %d rounds failed: a server without the Z_AI_* handlers answered "
                      "-- another RFC server is registered as %s", failed, rounds, progid)
        else:
            log.error("%d of %d rounds failed", failed, rounds)
        return 1
    log.info("selftest ok: %d rounds through destination %s", rounds, dest)
    return chat_rc


CORE_FUNCTIONS = ("RFC_READ_TABLE", "RFC_GET_STRUCTURE_DEFINITION_P",
                  "RFC_GET_FUNCTION_INTERFACE_P", "RFC_SYSTEM_INFO", "RFC_ABAP_INSTALL_AND_RUN")


def warm_targets(store) -> list[str]:
    """Every table and structure the knowledge notes name, exemplar programs included."""
    names: set[str] = set()
    for note in store.notes():
        names.update(t.upper() for t in note.tables)
    return sorted(n for n in names if n and "/" not in n)


def cache_admin(action: str, tables: str = "", from_knowledge: bool = False, names: str = "") -> int:
    """curie --cache stats | warm | purge."""
    from curie import caches

    directory = Path(config.CACHE_DIR)
    if action == "stats":
        disk = caches.disk_stats(directory)
        if not disk:
            log.info("no cache files in %s (CACHE_PERSIST=%d)", directory, config.CACHE_PERSIST)
            return 0
        for name, s in disk.items():
            if "error" in s:
                log.warning("%-22s %s", name, s["error"])
                continue
            log.info("%-22s %4d entries, %6d bytes, saved %s ago, oldest %s, newest %s%s",
                     name, s["entries"], s["bytes"], _age(s["saved_age"]), _age(s["oldest_age"]),
                     _age(s["newest_age"]), "" if s["same_system"] else f"  (other system: {s['system']})")
        log.info("persist TTL %s, directory %s", _age(int(config.CACHE_PERSIST_TTL)), directory)
        return 0

    if action == "purge":
        wanted = [n.strip() for n in names.split(",") if n.strip()]
        removed = caches.purge_disk(directory, wanted or None)
        log.info("removed %s from %s", ", ".join(removed) if removed else "nothing", directory)
        log.info("a running Curie keeps its memory copy until POST /admin/cache/flush or a restart")
        return 0

    # warm: fetch structures and interfaces through the normal caches, then save.
    from curie.memory import KnowledgeStore
    from curie.rfc.client import SapClient
    from curie.sap_tools.ddic import DdicCache
    from curie.sap_tools.function_interface import FunctionInterfaceCache
    from curie import handlers

    if not config.CACHE_PERSIST:
        log.error("CACHE_PERSIST=0: warming would be lost at exit")
        return 2
    sap = SapClient(config.SAP_RFC, size=1)
    try:
        detect_release(sap)
        ddic = DdicCache(sap)
        fi = FunctionInterfaceCache(sap)
        loaded = caches.load_all(directory)
        log.info("loaded from disk: %s", ", ".join(f"{k}={v}" for k, v in loaded.items()))
        wanted = [t.strip().upper() for t in tables.split(",") if t.strip()]
        if not wanted or from_knowledge:
            store = KnowledgeStore(config.KNOWLEDGE_DIR, refresh_every=0)
            wanted += [t for t in warm_targets(store) if t not in wanted]
        ok = failed = 0
        for name in wanted:
            try:
                info = ddic.structure(name)
                ok += 1
                log.info("structure ok:  %-30s %d field(s)", name, len(info.get("fields", [])))
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.warning("structure FAILED: %s -- %s", name, str(exc)[:120])
        functions = list(CORE_FUNCTIONS) + [config.ABAP_CHECK_FM] + list(handlers.HANDLERS)
        for fm in dict.fromkeys(functions):
            try:
                sap.interface(fm)
                fi.interface(fm)
                ok += 1
                log.info("interface ok:  %s", fm)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.warning("interface FAILED: %s -- %s", fm, str(exc)[:120])
        saved = caches.save_all(directory, force=True)
        log.info("warmed %d, failed %d; saved %s to %s", ok, failed,
                 ", ".join(k for k, v in saved.items() if v) or "nothing", directory)
        log.info("a running Curie picks the files up at its next start or POST /admin/cache/reload")
    finally:
        sap.close()
    return 1 if failed and not ok else 0


def _age(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def archive_admin(action: str, force: bool = False, last: str | None = None, yes: bool = False) -> int:
    """curie --archive import | stats | purge [--last 15m] [--force] [--yes]."""
    from curie.archive import Archive

    archive = Archive(config.ARCHIVE_DB)
    try:
        if action == "import":
            stats = archive.import_dir(config.SESSION_LOG_DIR, force=force)
            log.info("archive %s: %d log(s), %d imported, %d unchanged, %d episode(s)",
                     config.ARCHIVE_DB, stats["logs"], stats["imported"], stats["unchanged"],
                     stats["episodes"])
            return 0
        if action == "stats":
            st = archive.stats()
            log.info("archive %s (%s)", st["path"], _size(st["size_bytes"]))
            log.info("  sessions %d, episodes %d, model calls %d, tool calls %d, programs %d, "
                     "events %d, facts %d", st["sessions"], st["episodes"], st["calls"],
                     st["tool_calls"], st["programs"], st["events"], st["facts"])
            log.info("  outcomes: %s", ", ".join(f"{k} {v}" for k, v in st["outcomes"].items()) or "-")
            log.info("  tokens in %d, out %d; last import %s", st["tokens_in"], st["tokens_out"],
                     (_age(time.time() - st["last_import"]) + " ago") if st["last_import"] else "never")
            return 0
        if action == "purge":
            from curie.memory.purge import parse_interval
            if not last:
                log.error("--archive purge needs --last <interval> (e.g. 15m); the logs stay")
                return 2
            since = time.time() - parse_interval(last)
            count = len(archive.episodes(limit=100000, since=since))
            if count == 0:
                log.info("nothing to purge in the last %s", last)
                return 0
            if not yes:
                answer = input(f"Remove {count} episode(s) of the last {last} from the archive "
                               f"(the logs stay)? [y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    log.info("purge cancelled")
                    return 1
            removed = archive.purge(since=since)
            log.info("archive: %d episode(s) removed", removed)
            return 0
        log.error("unknown archive action %s", action)
        return 2
    finally:
        archive.close()


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def consolidate(use_llm: bool = True, apply: bool = False, use_sap: bool = True) -> int:
    """curie --consolidate [--no-llm] [--no-sap] [--apply]: families, glossary,
    dictionary index, system card, bundle."""
    from curie.archive import Archive
    from curie.memory import KnowledgeStore
    from curie.memory.consolidate import Consolidator

    store = KnowledgeStore(config.KNOWLEDGE_DIR, refresh_every=0)
    archive = None
    if config.ARCHIVE_ENABLED and Path(config.ARCHIVE_DB).exists():
        archive = Archive(config.ARCHIVE_DB)
    llm = None
    if use_llm:
        from curie.llm.client import LLMClient
        llm = LLMClient()
    sap = None
    if use_sap:
        from curie.rfc.client import SapClient
        try:
            sap = SapClient(config.SAP_RFC, size=1)
            detect_release(sap)
            from curie import caches
            if config.CACHE_PERSIST:
                caches.load_all()
        except Exception as exc:  # noqa: BLE001
            log.warning("SAP not reachable (%s): the dictionary index is skipped", exc)
            sap = None

    async def go():
        if llm is not None and not report_llm_check(await llm.check()):
            log.error("consolidation without the model (deterministic steps only)")
            worker = Consolidator(store, config.KNOWLEDGE_BUILD_DIR, None, archive, sap=sap)
        else:
            worker = Consolidator(store, config.KNOWLEDGE_BUILD_DIR, llm, archive, sap=sap)
        return await worker.run(apply_card=apply)

    try:
        report = asyncio.run(go())
    finally:
        if archive is not None:
            archive.close()
        if sap is not None:
            sap.close()
    dic = report.get("dictionary", {})
    log.info("dictionary index: %s", dic.get("skipped") or dic.get("error") or
             f"{dic.get('tables', 0)} table(s), {dic.get('customer', 0)} in the customer namespace")
    fam = report["families"]
    log.info("consolidated in %ss: families %s (confirmed %s), glossary %d term(s), card %s, bundle %s",
             report.get("seconds"), ", ".join(fam.get("written", [])) or "-",
             ", ".join(fam.get("confirmed", [])) or "-", report["glossary"].get("terms", 0),
             report["card"].get("skipped") or report["card"].get("error") or
             report["card"].get("rejected") or f"{report['card'].get('chars', 0)} chars",
             ", ".join(f"{k}={v}" for k, v in report["bundle"].items() if v))
    return 0


def trace(spec: str = "", newest: str | None = None, full: bool = False) -> int:
    """curie --trace <file | session id prefix> | --trace --newest [any|eval|user]
    [--full]: the timeline of one session log."""
    from curie.tools import trace as tr

    path = tr.find_log(spec, config.SESSION_LOG_DIR, last=newest is not None, kind=newest or "any")
    if path is None:
        log.error("no session log for %s under %s", spec or f"--newest {newest}", config.SESSION_LOG_DIR)
        return 1
    for line in tr.timeline(path, full=full):
        print(line)
    return 0


def housekeeping(dry_run: bool = False) -> int:
    """curie --housekeeping [--dry-run]: retention for session logs and the archive."""
    from curie.memory import housekeeping as hk

    archive = None
    if config.ARCHIVE_ENABLED:
        from curie.archive import Archive
        archive = Archive(config.ARCHIVE_DB)
    try:
        report = hk.run(config.SESSION_LOG_DIR, archive, config.LOG_RETENTION_DAYS,
                        config.ARCHIVE_RETENTION_DAYS, dry_run=dry_run)
    finally:
        if archive is not None:
            archive.close()
    for folder in report["log_folders_removed"]:
        log.info("  %s %s", "would remove" if dry_run else "removed", folder)
    return 0


def evaluate(limit: int = 0, cases: str = "", timeout: float | None = None,
             include_general: bool = False) -> int:
    """curie --eval [--limit N] [--cases id,id] [--include-general]: replay
    archived questions through the real turn (model, tools, checks), run
    nothing, report."""
    from curie.archive import Archive
    from curie.memory import evaluate as ev
    from curie.rfc.client import SapClient

    timeout = timeout or config.EVAL_TIMEOUT
    if not Path(config.ARCHIVE_DB).exists():
        log.error("no archive at %s: run curie --archive import first", config.ARCHIVE_DB)
        return 2
    archive = Archive(config.ARCHIVE_DB)
    wanted = [c.strip() for c in cases.split(",") if c.strip()] if cases else None
    excluded: list[str] = []
    bank = ev.question_bank(archive, limit=limit, ids=wanted, include_general=include_general,
                            excluded=excluded)
    for e in excluded:
        log.info("eval: %s excluded: %s (--include-general replays it)", e["id"], e["reason"])
    if not bank:
        log.error("no confirmed episodes to replay")
        archive.close()
        return 1
    config.ABAP_REQUIRE_CONFIRM = True       # never execute during an evaluation
    sap = SapClient(config.SAP_RFC, size=config.RFC_CLIENT_POOL)
    detect_release(sap)
    manager, orchestrator, flow, executor, app, llm = build(sap)
    manager.log_dir = Path(config.SESSION_LOG_DIR) / "eval"      # not episodes
    if flow.audit_memo is not None:
        log.info("audit memo: %d allowed program shape(s) from the archive", flow.audit_memo.load(archive))

    async def go():
        manager.bind_loop(asyncio.get_running_loop())
        if not report_llm_check(await llm.check()):
            return None
        from curie import caches
        if config.CACHE_PERSIST:
            caches.load_all()
        evaluator = ev.Evaluator(manager, orchestrator, orchestrator.knowledge, llm)
        log.info("evaluating %d case(s), model %s, timeout %.0fs per case", len(bank), llm.model, timeout)
        result = await evaluator.run(bank, timeout=timeout)
        await manager.stop("shutdown")
        return result

    try:
        result = asyncio.run(go())
        if result is None:
            return 2
        result["excluded"] = excluded
        previous = archive.last_eval()
        archive.save_eval(result)
        path = ev.write_report(ev.report_markdown(result, previous), config.KNOWLEDGE_DIR)
        s = result["summary"]
        log.info("eval: %d case(s): runnable %d, syntax ok first %d/%d, shape equal %d, replayed %d, "
                 "denied %d, errors %d; report %s", s["cases"], s["runnable"], s["first_syntax_ok"],
                 s["syntax_checked"], s["shape_equal"], s["replayed"], s["denied"], s["errors"], path)
        return 0
    finally:
        archive.close()
        sap.close()


def review_knowledge(apply: bool = False, everything: bool = False, use_sap: bool = True) -> int:
    """Run the knowledge review once, standalone (no registration, no web)."""
    from curie.llm.client import LLMClient
    from curie.memory import KnowledgeStore
    from curie.memory.review import KnowledgeReviewer
    from curie.rfc.client import SapClient

    store = KnowledgeStore(config.KNOWLEDGE_DIR, config.KNOWLEDGE_MAX_NOTES,
                           config.KNOWLEDGE_MAX_CHARS, config.KNOWLEDGE_EXEMPLARS,
                           max_insights=config.KNOWLEDGE_MAX_INSIGHTS)
    sap = None
    if use_sap:
        try:
            sap = SapClient(config.SAP_RFC, size=1)
            sap.ping()
        except Exception as exc:  # noqa: BLE001
            log.warning("no SAP connection for the dictionary check: %s", exc)
            sap = None
    reviewer = KnowledgeReviewer(store, LLMClient(), sap)
    log.info("reviewing %d pending note(s) of %d with %s%s", len(reviewer.pending(everything)),
             len(store), config.KNOWLEDGE_REVIEW_MODEL, " (apply)" if apply else "")
    try:
        report = asyncio.run(reviewer.run(apply=apply, everything=everything))
    finally:
        if sap is not None:
            sap.close()
    for flag in report.get("flags", []):
        log.warning("%s (%s): %s -- %s%s", flag["id"], flag["type"], flag["verdict"],
                    "; ".join(flag.get("problems") or []) or "-",
                    f" -> {flag['question']}" if flag.get("question") else "")
    log.info("report: %s", store.root / "REVIEW.md")
    return 0


def build_web_server(app, manager, loop):
    """The uvicorn server for the web app. On the shutdown signal it closes
    the sessions first, so the browsers' event streams end and uvicorn's
    wait for open connections is short and quiet; the cleanup after
    serve() then finds the sessions closed and their callbacks waited for."""
    import uvicorn

    class CurieServer(uvicorn.Server):
        def handle_exit(self, sig, frame):
            super().handle_exit(sig, frame)
            try:
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(manager.stop("shutdown")))
            except RuntimeError:           # loop already closed
                pass

    return CurieServer(uvicorn.Config(
        app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="info",
        access_log=False, loop="asyncio", timeout_graceful_shutdown=5,
    ))


def quiet_signals() -> None:
    """Handlers for SIGINT, SIGTERM and SIGBREAK that only log: installed
    before uvicorn's serve(), which saves them, installs its own, restores
    ours on exit and re-raises the signal it caught."""
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        return

    def noted(sig, frame):
        log.info("shutdown requested (signal %s)", sig)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, noted)
            except (ValueError, OSError):
                pass


async def serve_async() -> int:
    import uvicorn

    from curie.tools import rfc_server
    from curie import sapnwrfc720 as sap_sdk
    from curie.rfc.client import SapClient

    problems = config.problems()
    if problems:
        for p in problems:
            log.error("config: %s", p)
        return 2

    log.info("NW RFC SDK %s from %s", sap_sdk.sdk_version(), sap_sdk.SDK_HOME)
    sap = SapClient(config.SAP_RFC, size=config.RFC_CLIENT_POOL)
    detect_release(sap)
    manager, orchestrator, flow, executor, app, llm = build(sap)
    manager.bind_loop(asyncio.get_running_loop())
    manager.start_monitor()

    # The model is checked once at start, before anything is registered at
    # the gateway. A failure is fatal: a Curie without a model would only
    # collect sessions that cannot be served. The log names the served
    # models when LLM_MODEL does not match the endpoint.
    if not report_llm_check(await llm.check()):
        log.error("startup stopped: fix LLM_BASE_URL / LLM_API_KEY / LLM_MODEL in .env and start again")
        await manager.stop("shutdown")
        sap.close()
        return 2

    from curie import caches
    save_task = None
    if config.CACHE_PERSIST:
        loaded = caches.load_all()
        log.info("caches from %s: %s", config.CACHE_DIR,
                 ", ".join(f"{k}={v}" for k, v in loaded.items()) or "nothing")

        async def save_loop():
            while True:
                try:
                    await asyncio.sleep(config.CACHE_SAVE_INTERVAL)
                    await asyncio.get_running_loop().run_in_executor(None, caches.save_all)
                except asyncio.CancelledError:
                    return
                except Exception:  # noqa: BLE001
                    log.exception("cache save")
        save_task = asyncio.ensure_future(save_loop())

    review_task = None
    if orchestrator.knowledge is not None:
        from curie.memory.review import KnowledgeReviewer
        reviewer = KnowledgeReviewer(orchestrator.knowledge.store, llm, sap)
        app.state.reviewer = reviewer
        if config.KNOWLEDGE_REVIEW_INTERVAL > 0:
            async def review_loop():
                while True:
                    try:
                        await asyncio.sleep(config.KNOWLEDGE_REVIEW_INTERVAL)
                        await reviewer.run(apply=config.KNOWLEDGE_REVIEW_APPLY)
                    except asyncio.CancelledError:
                        return
                    except Exception:  # noqa: BLE001
                        log.exception("knowledge review")
            review_task = asyncio.ensure_future(review_loop())
            log.info("knowledge review every %ds%s", int(config.KNOWLEDGE_REVIEW_INTERVAL),
                     " with apply" if config.KNOWLEDGE_REVIEW_APPLY else "")

    archive = None
    if config.ARCHIVE_ENABLED:
        from curie.archive import Archive
        try:
            archive = Archive(config.ARCHIVE_DB)
            log.info("archive: %s", config.ARCHIVE_DB)
        except Exception as exc:  # noqa: BLE001 -- the archive never blocks serving
            log.warning("archive unavailable (%s); sessions are logged only", exc)
            archive = None
        if archive is not None and flow.audit_memo is not None:
            log.info("audit memo: %d allowed program shape(s) from the archive",
                     flow.audit_memo.load(archive))

        async def archive_session(session, reason):
            """Session close: the log becomes episodes in the archive."""
            if archive is None:
                return
            path = Path(config.SESSION_LOG_DIR) / time.strftime("%Y-%m-%d") / f"{session.id}.jsonl"
            if not path.exists():
                candidates = sorted(Path(config.SESSION_LOG_DIR).glob(f"*/{session.id}.jsonl"))
                if not candidates:
                    return
                path = candidates[-1]
            try:
                n = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: archive.import_log(path, force=True))
                log.debug("archive: session %s -> %d episode(s)", session.id[:8], n)
            except Exception as exc:  # noqa: BLE001
                log.warning("archive import of %s failed: %s", session.id[:8], exc)

        manager.on_close = archive_session

    chat = None
    if config.CHAT_PLUGIN:
        from curie.chat import ChatService
        chat = ChatService(llm, asyncio.get_running_loop())
        chat.start_monitor()
        log.info("chat plugin on: models %s, max %d handle(s)%s",
                 ", ".join(config.chat_models()), config.CHAT_MAX_HANDLES,
                 " (thread per answer)" if config.CHAT_STREAM_THREADS else "")
        if config.CHAT_MAX_HANDLES >= config.RFC_REG_COUNT:
            log.warning("CHAT_MAX_HANDLES (%d) >= RFC_REG_COUNT (%d): raise RFC_REG_COUNT",
                        config.CHAT_MAX_HANDLES, config.RFC_REG_COUNT)

    server = rfc_server.build_server()
    server.start()
    log.info("registered PROGRAM_ID=%s (%sx) at gateway %s/%s",
             config.SAP_RFC_SERVER["PROGRAM_ID"], config.RFC_REG_COUNT,
             config.SAP_RFC_SERVER["GWHOST"], config.SAP_RFC_SERVER["GWSERV"])
    try:
        await executor.sweep_stale()
    except Exception as exc:  # noqa: BLE001
        log.warning("sweep: %s", exc)

    web = build_web_server(app, manager, asyncio.get_running_loop())
    log.info("web on http://%s:%s, public URL %s", config.WEB_HOST, config.WEB_PORT,
             config.WEB_PUBLIC_URL)
    # uvicorn re-raises the signal it caught once serve() returns, which
    # would turn into a KeyboardInterrupt inside the cleanup below and cut
    # it short (the archive import, the RFC server stop). Our own handlers
    # take that re-raised signal and only log it.
    quiet_signals()
    try:
        await web.serve()  # returns on SIGINT / SIGTERM / SIGBREAK
    finally:
        log.info("shutting down")
        if review_task is not None:
            review_task.cancel()
        if save_task is not None:
            save_task.cancel()
            saved = caches.save_all()
            log.info("caches saved: %s", ", ".join(k for k, v in saved.items() if v) or "nothing changed")
        await manager.stop("shutdown")
        if chat is not None:
            chat.shutdown()   # wakes READs in flight so server.stop() can finish
        server.stop()
        server.close()
        sap.close()
        if archive is not None:
            archive.close()
        log.info("stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="verify SAP logon, FM interfaces and the LLM, then exit")
    parser.add_argument("--selftest", action="store_true",
                        help="with Curie running: call the served FMs from inside SAP "
                             "through the SM59 destination (SAP_AI) and report")
    parser.add_argument("--dest", default=None,
                        help="SM59 destination for --selftest (default: RFC_PROGID)")
    parser.add_argument("--rounds", type=int, default=6, help="rounds for --selftest")
    parser.add_argument("--review-knowledge", action="store_true",
                        help="review the knowledge notes with the model (facts, follow-up "
                             "questions, duplicates); report in knowledge/REVIEW.md")
    parser.add_argument("--apply", action="store_true",
                        help="with --review-knowledge: apply question rewrites and retirements")
    parser.add_argument("--all", action="store_true",
                        help="with --review-knowledge: review every note, not only unreviewed ones")
    parser.add_argument("--no-sap", action="store_true",
                        help="with --review-knowledge: skip the dictionary check")
    parser.add_argument("--cache", choices=("stats", "warm", "purge"), default=None,
                        help="the persisted dictionary caches: what is on disk, prefetch "
                             "structures and interfaces (--tables, --from-knowledge), or drop the files")
    parser.add_argument("--tables", default="",
                        help="with --cache warm: table or structure names, comma separated")
    parser.add_argument("--from-knowledge", action="store_true",
                        help="with --cache warm: every table the knowledge notes mention (default "
                             "when --tables is not given)")
    parser.add_argument("--names", default="",
                        help="with --cache purge: cache names, comma separated (default all)")
    parser.add_argument("--eval", action="store_true",
                        help="replay archived questions through the real turn without running "
                             "anything and write knowledge/EVAL.md (needs SAP and the model)")
    parser.add_argument("--limit", type=int, default=0, help="with --eval: at most N cases (newest first)")
    parser.add_argument("--cases", default="", help="with --eval: episode ids, comma separated")
    parser.add_argument("--timeout", type=float, default=None,
                        help="with --eval: seconds per case (default EVAL_TIMEOUT, 600)")
    parser.add_argument("--include-general", action="store_true",
                        help="with --eval: also replay cases whose opening question was answered "
                             "without asking SAP (excluded by default)")
    parser.add_argument("--housekeeping", action="store_true",
                        help="retention: import and remove session log folders older than "
                             "LOG_RETENTION_DAYS, remove archive episodes older than "
                             "ARCHIVE_RETENTION_DAYS (--dry-run reports only)")
    parser.add_argument("--dry-run", action="store_true", help="with --housekeeping: report only")
    parser.add_argument("--trace", nargs="?", const="", default=None, metavar="LOG",
                        help="print the timeline of one session log: a file, a session id prefix "
                             "(searched under the log folder, eval included), or --newest [any|eval|user]")
    parser.add_argument("--newest", nargs="?", const="any", default=None, choices=["any", "eval", "user"],
                        help="with --trace: the newest session (any), the newest evaluation session "
                             "(eval) or the newest real session (user)")
    parser.add_argument("--full", action="store_true",
                        help="with --trace: answers, prompts and tool results in full")
    parser.add_argument("--consolidate", action="store_true",
                        help="build report families, the glossary and the system card from the "
                             "notes and the archive, then the bundle (--no-llm: deterministic "
                             "parts only; --apply: the generated card goes into the bundle)")
    parser.add_argument("--no-llm", action="store_true", help="with --consolidate: no model calls")
    parser.add_argument("--archive", choices=("import", "stats", "purge"), default=None,
                        help="the archive: import the session logs into episodes, show counts, "
                             "or purge the episodes of the last --last interval")
    parser.add_argument("--force", action="store_true",
                        help="with --archive import: re-import logs already imported")
    parser.add_argument("--purge-knowledge", action="store_true",
                        help="move the notes of every type, the EVAL*/REVIEW reports and the archived "
                             "episodes (chat history) of a window to knowledge/.trash: --last 15m for a "
                             "window, --all for everything including the archive file and the bundle "
                             "(asks twice)")
    parser.add_argument("--last", default=None, metavar="INTERVAL",
                        help="with --purge-knowledge: window such as 15m, 2h, 1d")
    parser.add_argument("--types", default="",
                        help="with --purge-knowledge: note types to purge, e.g. exemplar,insight "
                             "(default: all of table,concept,family,exemplar,hint,insight)")
    parser.add_argument("--yes", action="store_true",
                        help="with --purge-knowledge --last: do not ask (never with --all)")
    parser.add_argument("--no-archive", action="store_true",
                        help="with --purge-knowledge: leave the archive (chat history) alone")
    parser.add_argument("--no-reports", action="store_true",
                        help="with --purge-knowledge: leave EVAL*.md and REVIEW.md alone")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    if args.check:
        return check()
    if args.selftest:
        return selftest(args.dest or config.SAP_RFC_SERVER["PROGRAM_ID"], args.rounds)
    if args.review_knowledge:
        return review_knowledge(apply=args.apply, everything=args.all, use_sap=not args.no_sap)
    if args.cache:
        return cache_admin(args.cache, tables=args.tables, from_knowledge=args.from_knowledge,
                           names=args.names)
    if args.trace is not None or args.newest is not None:
        return trace(spec=args.trace or "", newest=args.newest, full=args.full)
    if args.housekeeping:
        return housekeeping(dry_run=args.dry_run)
    if args.eval:
        return evaluate(limit=args.limit, cases=args.cases, timeout=args.timeout,
                        include_general=args.include_general)
    if args.consolidate:
        return consolidate(use_llm=not args.no_llm, apply=args.apply, use_sap=not args.no_sap)
    if args.archive:
        return archive_admin(args.archive, force=args.force, last=args.last, yes=args.yes)
    if args.purge_knowledge:
        from curie.memory import KnowledgeStore
        from curie.memory import purge as purge_mod
        store = KnowledgeStore(config.KNOWLEDGE_DIR, refresh_every=0)
        try:
            purged = purge_mod.run(store, last=args.last, everything=args.all, types=args.types,
                                   yes=args.yes,
                                   archive_path=None if args.no_archive or not config.ARCHIVE_ENABLED
                                   else config.ARCHIVE_DB,
                                   with_reports=not args.no_reports, build_dir=config.KNOWLEDGE_BUILD_DIR)
        except ValueError as exc:
            log.error("%s", exc)
            return 2
        return 0 if purged >= 0 else 1
    try:
        return asyncio.run(serve_async())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
