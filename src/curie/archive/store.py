"""The archive database: SQLite, one file, WAL, append-mostly.

Tables (knowledge plan 5.1 and 5.2):

    sessions    one row per session log
    episodes    one thread of a session: question, outcome, counters
    calls       model calls of an episode with the prompt context
    tool_calls  tool calls with full arguments and results
    programs    every program version with verdicts, run and exemplar
    events      the raw records of the episode (recordings)
    facts       typed statements with evidence (filled by later pipelines)
    meta        key/value: schema version, last import

Import is idempotent: a session is (re)imported when its log is newer
than the stored import time; its rows are replaced. Writers are the
runtime (session close) and the CLI; readers are the consolidation
pipelines and `curie --archive stats`.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from curie.archive.episodes import Episode, SessionRecord, build_session

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, log_path TEXT, sysid TEXT, client TEXT, user TEXT, language TEXT,
    release TEXT, opened REAL, closed REAL, reason TEXT, initial_prompt TEXT,
    selftest INTEGER DEFAULT 0, episodes INTEGER DEFAULT 0, log_mtime REAL, imported REAL);
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER, thread TEXT, question TEXT,
    refinements TEXT, language TEXT, started REAL, ended REAL, outcome TEXT, feedback TEXT,
    next_action TEXT, next_gap REAL, exemplar_id TEXT, family_id TEXT, origin TEXT,
    calls INTEGER, tool_calls INTEGER, tools_failed INTEGER, programs INTEGER,
    syntax_rounds INTEGER, tokens_in INTEGER, tokens_out INTEGER, user TEXT, sysid TEXT);
CREATE INDEX IF NOT EXISTS episodes_session ON episodes(session_id);
CREATE INDEX IF NOT EXISTS episodes_outcome ON episodes(outcome);
CREATE TABLE IF NOT EXISTS calls (
    id TEXT, episode_id TEXT, ts REAL, model TEXT, temperature REAL, max_tokens INTEGER,
    tool_mode TEXT, tools INTEGER, notes TEXT, exemplars TEXT, knowledge_chars INTEGER,
    messages INTEGER, history_chars INTEGER, finish TEXT, tool_calls INTEGER, chars INTEGER,
    ms INTEGER, tokens_in INTEGER, tokens_out INTEGER, text TEXT);
CREATE INDEX IF NOT EXISTS calls_episode ON calls(episode_id);
CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT, episode_id TEXT, call_id TEXT, ts REAL, name TEXT, args TEXT, ok INTEGER,
    summary TEXT, ms INTEGER, result TEXT, tables TEXT);
CREATE INDEX IF NOT EXISTS tool_calls_episode ON tool_calls(episode_id);
CREATE INDEX IF NOT EXISTS tool_calls_name ON tool_calls(name);
CREATE TABLE IF NOT EXISTS programs (
    run_id TEXT PRIMARY KEY, episode_id TEXT, ts REAL, attempt INTEGER, progname TEXT,
    source TEXT, static TEXT, syntax TEXT, audit TEXT, can_run INTEGER, confirmed INTEGER,
    discarded INTEGER, run TEXT, run_ok INTEGER, lines INTEGER, exemplar_id TEXT, origin TEXT,
    tables TEXT);
CREATE INDEX IF NOT EXISTS programs_episode ON programs(episode_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, episode_id TEXT, ts REAL,
    kind TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS events_episode ON events(episode_id);
CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT, evidence TEXT,
    support INTEGER DEFAULT 0, users INTEGER DEFAULT 0, verify TEXT, status TEXT,
    confidence REAL, created REAL, updated REAL, note_id TEXT, source TEXT);
CREATE INDEX IF NOT EXISTS facts_subject ON facts(subject, predicate);
CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL, seconds REAL, model TEXT, cases INTEGER,
    summary TEXT, results TEXT);
"""


def _j(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class Archive:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
            self._conn.executescript(SCHEMA)
            self._conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema', ?)",
                               (str(SCHEMA_VERSION),))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- import ------------------------------------------------------------------- #

    def import_log(self, path: str | Path, force: bool = False) -> int:
        """Import one session log; returns the number of episodes, -1 when
        the log was already imported and unchanged."""
        path = Path(path)
        try:
            from curie.archive.episodes import session_files
            mtime = max(p.stat().st_mtime for p in session_files(path))
        except (OSError, ValueError):
            return -1
        with self._lock:
            row = self._conn.execute("SELECT log_mtime FROM sessions WHERE id = ?",
                                     (path.stem,)).fetchone()
        if row is not None and not force and row["log_mtime"] is not None \
                and float(row["log_mtime"]) >= mtime:
            return -1
        session, episodes = build_session(path)
        self.store(session, episodes, mtime)
        return len(episodes)

    def import_dir(self, root: str | Path, force: bool = False) -> dict:
        """Import every *.jsonl under root (day folders); returns counts."""
        root = Path(root)
        stats = {"logs": 0, "imported": 0, "unchanged": 0, "episodes": 0}
        done: set[str] = set()
        for path in sorted(root.rglob("*.jsonl")):
            if path.stem in done:          # a session that ran past midnight: one import
                continue
            if "eval" in path.relative_to(root).parts[:-1]:
                continue                   # evaluation sessions are not episodes
            done.add(path.stem)
            stats["logs"] += 1
            n = self.import_log(path, force=force)
            if n < 0:
                stats["unchanged"] += 1
            else:
                stats["imported"] += 1
                stats["episodes"] += n
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_import', ?)",
                               (str(time.time()),))
            self._conn.commit()
        return stats

    def store(self, session: SessionRecord, episodes: list[Episode], log_mtime: float) -> None:
        from curie.memory.store import program_tables

        with self._lock:
            c = self._conn
            c.execute("DELETE FROM events WHERE session_id = ?", (session.id,))
            for table in ("calls", "tool_calls", "programs"):
                c.execute(f"DELETE FROM {table} WHERE episode_id IN "
                          f"(SELECT id FROM episodes WHERE session_id = ?)", (session.id,))
            c.execute("DELETE FROM episodes WHERE session_id = ?", (session.id,))
            c.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                session.id, session.log_path, session.sysid, session.client, session.user,
                session.language, session.release, session.opened, session.closed, session.reason,
                session.initial_prompt, int(session.selftest), len(episodes), log_mtime, time.time()))
            for ep in episodes:
                c.execute("INSERT INTO episodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    ep.id, ep.session_id, ep.seq, ep.thread, ep.question, _j(ep.refinements),
                    ep.language, ep.started, ep.ended, ep.outcome, _j(ep.feedback), ep.next_action,
                    ep.next_gap, ep.exemplar_id, "", _j(ep.origin) if ep.origin else None,
                    len(ep.calls), len(ep.tool_calls),
                    sum(1 for t in ep.tool_calls if t.get("ok") is False), len(ep.programs),
                    ep.syntax_rounds, ep.tokens_in, ep.tokens_out, session.user, session.sysid))
                for call in ep.calls:
                    c.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        call.get("id"), ep.id, call.get("ts"), call.get("model"), call.get("temperature"),
                        call.get("max_tokens"), call.get("tool_mode"), call.get("tools"),
                        _j(call.get("notes") or []), _j(call.get("exemplars") or []),
                        call.get("knowledge_chars"), call.get("messages"), call.get("history_chars"),
                        call.get("finish"), call.get("tool_calls"), call.get("chars"), call.get("ms"),
                        call.get("tokens_in"), call.get("tokens_out"), call.get("text")))
                for t in ep.tool_calls:
                    c.execute("INSERT INTO tool_calls VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                        t["id"], ep.id, t.get("call_id"), t.get("ts"), t.get("name"), _j(t.get("args")),
                        None if t.get("ok") is None else int(bool(t.get("ok"))), t.get("summary"),
                        t.get("ms"), _j(t.get("result")) if t.get("result") is not None else None,
                        _j(_tables_of(t))))
                for p in ep.programs:
                    run = p.get("run") or {}
                    c.execute("INSERT OR REPLACE INTO programs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        p["run_id"], ep.id, p.get("ts"), p.get("attempt"), p.get("progname"),
                        p.get("source"), _j(p.get("static") or []),
                        _j(p.get("syntax")) if p.get("syntax") else None,
                        _j(p.get("audit")) if p.get("audit") else None,
                        None if p.get("can_run") is None else int(bool(p.get("can_run"))),
                        int(bool(p.get("confirmed"))), int(bool(p.get("discarded"))),
                        _j(run) if run else None, None if "ok" not in run else int(bool(run.get("ok"))),
                        len(run.get("lines") or []), p.get("exemplar_id"),
                        _j(p.get("origin")) if p.get("origin") else None,
                        _j(program_tables(p.get("source") or ""))))
                c.executemany("INSERT INTO events(session_id, episode_id, ts, kind, data) VALUES (?,?,?,?,?)",
                              [(session.id, ep.id, float(r.get("ts") or 0), r.get("kind"), _j(r))
                               for r in ep.events])
            c.commit()

    # -- reading ------------------------------------------------------------------- #

    def stats(self) -> dict:
        with self._lock:
            c = self._conn
            out = {"path": str(self.path), "size_bytes": self.path.stat().st_size if self.path.exists() else 0}
            for table in ("sessions", "episodes", "calls", "tool_calls", "programs", "events", "facts"):
                out[table] = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            out["outcomes"] = {r[0]: r[1] for r in c.execute(
                "SELECT outcome, COUNT(*) FROM episodes GROUP BY outcome ORDER BY 2 DESC")}
            out["tokens_in"] = c.execute("SELECT COALESCE(SUM(tokens_in), 0) FROM episodes").fetchone()[0]
            out["tokens_out"] = c.execute("SELECT COALESCE(SUM(tokens_out), 0) FROM episodes").fetchone()[0]
            row = c.execute("SELECT value FROM meta WHERE key = 'last_import'").fetchone()
            out["last_import"] = float(row[0]) if row else None
        return out

    def episodes(self, outcome: str | None = None, limit: int = 100, since: float = 0.0) -> list[dict]:
        sql = "SELECT * FROM episodes WHERE started >= ?"
        args: list = [since]
        if outcome:
            sql += " AND outcome = ?"
            args.append(outcome)
        sql += " ORDER BY started DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def query(self, sql: str, args: tuple = ()) -> list[dict]:
        """Read-only SQL for the pipelines and the CLI."""
        if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
            raise ValueError("read-only")
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # -- evaluation runs ------------------------------------------------------------- #

    def save_eval(self, result: dict) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO eval_runs(started, seconds, model, cases, summary, results) VALUES (?,?,?,?,?,?)",
                (result.get("started"), result.get("seconds"), result.get("model", ""),
                 int(result.get("summary", {}).get("cases", 0)), _j(result.get("summary", {})),
                 _j(result.get("cases", []))))
            self._conn.commit()
            return int(cur.lastrowid)

    def last_eval(self) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM eval_runs ORDER BY started DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return {"started": row["started"], "seconds": row["seconds"], "model": row["model"],
                "summary": json.loads(row["summary"] or "{}"), "cases": json.loads(row["results"] or "[]")}

    # -- purge ---------------------------------------------------------------------- #

    def purge(self, since: float | None = None, session_ids: list[str] | None = None,
              before: float | None = None) -> int:
        """Delete episodes (and their rows) started after `since`, before
        `before`, or of the given sessions; returns the number of episodes
        removed. Sessions stay (with episodes = 0) so a re-import is not
        triggered. Facts and evaluation runs are kept."""
        with self._lock:
            c = self._conn
            if session_ids:
                marks = ",".join("?" * len(session_ids))
                ids = [r[0] for r in c.execute(
                    f"SELECT id FROM episodes WHERE session_id IN ({marks})", session_ids)]
            elif since is not None:
                ids = [r[0] for r in c.execute("SELECT id FROM episodes WHERE started >= ?", (since,))]
            elif before is not None:
                ids = [r[0] for r in c.execute("SELECT id FROM episodes WHERE started < ?", (before,))]
            else:
                return 0
            if not ids:
                return 0
            marks = ",".join("?" * len(ids))
            for table in ("calls", "tool_calls", "programs", "events"):
                c.execute(f"DELETE FROM {table} WHERE episode_id IN ({marks})", ids)
            c.execute(f"DELETE FROM episodes WHERE id IN ({marks})", ids)
            c.execute("UPDATE sessions SET episodes = (SELECT COUNT(*) FROM episodes e "
                      "WHERE e.session_id = sessions.id)")
            c.commit()
        return len(ids)

    def vacuum(self) -> None:
        with self._lock:
            self._conn.execute("VACUUM")


def _tables_of(tool_call: dict) -> list[str]:
    args = tool_call.get("args") or {}
    name = tool_call.get("name", "")
    if name in ("sap_read_table", "sap_get_domain_values") and args.get("table"):
        return [str(args["table"]).upper()]
    if name == "sap_get_structure" and args.get("name"):
        return [str(args["name"]).upper()]
    return []
