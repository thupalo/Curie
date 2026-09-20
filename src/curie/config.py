"""Central configuration for Curie, read from .env in CURIE_HOME.

One SDK generation per process: everything -- the registered server and the
client calls -- runs on NW RFC SDK 7.20 through sapnwrfc720.py. pyrfc is not
used any more (see CURIE_PLAN.md, decision D1).

Settings are module-level names so that the rest of the code reads
`config.NAME` at call time; tests override them with monkeypatch.
"""
from __future__ import annotations

import os
from pathlib import Path

import dotenv

# The project directory: where .env, knowledge/, logs/ and the SAP sources
# live. CURIE_HOME when set, otherwise the current working directory (the
# service installer and rfc_dev set it). The code itself is installed as a
# package and does not care where it sits.
HOME = Path(os.environ.get("CURIE_HOME") or Path.cwd()).resolve()
BASE_DIR = HOME
PACKAGE_DIR = Path(__file__).resolve().parent

# SAP credentials and app settings live in .env (never committed).
# Looked up in the project directory first, then one level above it (so
# several checkouts can share one .env kept outside the repositories).
if not dotenv.load_dotenv(HOME / ".env"):
    dotenv.load_dotenv(HOME.parent / ".env")

os.environ.setdefault("SAPNWRFC720_HOME", r"C:\nwrfcsdk")
SAPNWRFC720_HOME = os.environ["SAPNWRFC720_HOME"]


def _env(name: str, default: str = "") -> str:
    """os.environ with .env-style inline comments removed.

    python-dotenv strips "value  # comment" but keeps "# comment" when the
    value before it is empty; treat both the same.
    """
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    if value.startswith("#"):
        return default
    if " #" in value and not (value.startswith('"') or value.startswith("'")):
        value = value.split(" #", 1)[0].rstrip()
    return value or default


def _int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = _env(name)
    return float(value) if value else default


def _bool(name: str, default: bool) -> bool:
    value = _env(name).lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on", "x")


def _list(name: str, default: str = "") -> list[str]:
    raw = _env(name, default)
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


# --------------------------------------------------------------------------- #
# SAP logon (client direction) and gateway registration (server direction)
# --------------------------------------------------------------------------- #

SAP_RFC = {
    "user": _env("RFC_USER"),
    "passwd": _env("RFC_PASS"),
    "ashost": _env("RFC_ASHOST"),
    "sysnr": _env("RFC_SYSNR", "00"),
    "client": _env("RFC_CLIENT", "100"),
    "lang": _env("RFC_LANG", "PL"),
}

SAP_RFC_SERVER = {
    "GWHOST": _env("RFC_GWHOST", _env("RFC_ASHOST")),
    "GWSERV": _env("RFC_GWSERV", "sapgw00"),
    "PROGRAM_ID": _env("RFC_PROGID"),
}

# Registrations at the gateway = parallel ABAP -> Curie calls. Heartbeats are
# short, list deliveries longer; 3 keeps them from queueing behind each other.
RFC_REG_COUNT = _int("RFC_REG_COUNT", 3)
RFC_DISPATCH_TIMEOUT = _int("RFC_DISPATCH_TIMEOUT", 5)

# Client connections for tool calls and code execution (one per thread at a
# time; see rfc/client.py).
RFC_CLIENT_POOL = _int("RFC_CLIENT_POOL", 2)
RFC_CALL_TIMEOUT = _float("RFC_CALL_TIMEOUT", 120.0)

if _env("RFC_SERVER_TRACE"):
    SAP_RFC_SERVER["TRACE"] = _env("RFC_SERVER_TRACE")
if _env("RFC_SAPROUTER"):
    SAP_RFC_SERVER["SAPROUTER"] = _env("RFC_SAPROUTER")
    SAP_RFC["saprouter"] = _env("RFC_SAPROUTER")

# --------------------------------------------------------------------------- #
# Target system profile
# --------------------------------------------------------------------------- #

# "45B" or "ECC6". Empty = autodetect from RFC_SYSTEM_INFO at startup
# (cli.py sets SAP_RELEASE then). The profile decides the ABAP dialect in
# the prompts and the source line width.
SAP_RELEASE = _env("SAP_RELEASE").upper()

# ISO 639 (what RFC_LANG holds) -> the one-character SAP language key that
# SY-LANGU carries. RFC_GET_STRUCTURE_DEFINITION_P and
# RFC_GET_FUNCTION_INTERFACE_P take that one character, and a 4.5B backend
# accepts nothing else. SAP_LANGU in .env overrides the derived value.
ISO_TO_SAP_LANGU = {
    "EN": "E", "DE": "D", "FR": "F", "IT": "I", "ES": "S", "PT": "P",
    "NL": "N", "PL": "L", "CS": "C", "HU": "H", "RU": "R", "JA": "J",
    "TR": "T", "FI": "U", "SV": "V", "DA": "K", "NO": "O", "EL": "G",
    "HE": "B", "SK": "Q", "SL": "5", "HR": "6", "RO": "4", "UK": "8",
    "BG": "W", "ZH": "1", "ZF": "M", "KO": "3",
}


def sap_langu(iso_or_key: str) -> str:
    """One-character SY-LANGU for an ISO code; a single character passes through."""
    value = (iso_or_key or "").strip().upper()
    if len(value) == 1:
        return value
    return ISO_TO_SAP_LANGU.get(value, "E")


SAP_LANGU = (_env("SAP_LANGU").strip().upper() or sap_langu(SAP_RFC["lang"]))[:1]

# --------------------------------------------------------------------------- #
# LLM (OpenAI-compatible)
# --------------------------------------------------------------------------- #

LLM_BASE_URL = _env("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY = _env("LLM_API_KEY", "not-set")
LLM_MODEL = _env("LLM_MODEL")
LLM_AUDIT_MODEL = _env("LLM_AUDIT_MODEL") or LLM_MODEL
LLM_TIMEOUT = _float("LLM_TIMEOUT", 120.0)
LLM_MAX_TOOL_ROUNDS = _int("LLM_MAX_TOOL_ROUNDS", 8)
LLM_MAX_OUTPUT_TOKENS = _int("LLM_MAX_OUTPUT_TOKENS", 4096)
LLM_HISTORY_MAX_TOKENS = _int("LLM_HISTORY_MAX_TOKENS", 24000)
LLM_STREAM = _bool("LLM_STREAM", True)
# Ask the server for token usage on streamed answers (stream_options); a
# server that rejects the option is asked without it from then on.
LLM_STREAM_USAGE = _bool("LLM_STREAM_USAGE", True)
# How tools are offered: auto (tools + tool_choice=auto, the OpenAI default),
# omit (tools without the hint: vLLM without --enable-auto-tool-choice), off
# (no tools). Curie steps down on the server's 400s by itself; this only
# sets the starting point.
LLM_TOOL_CHOICE = _env("LLM_TOOL_CHOICE", "auto").lower()
LLM_TEMPERATURE = _float("LLM_TEMPERATURE", 0.2)
TOOL_RESULT_MAX_CHARS = _int("TOOL_RESULT_MAX_CHARS", 12000)
# Language the assistant answers in; defaults to the logon language.
LLM_ANSWER_LANGUAGE = _env("LLM_ANSWER_LANGUAGE") or SAP_RFC["lang"]

# --------------------------------------------------------------------------- #
# Web
# --------------------------------------------------------------------------- #

WEB_HOST = _env("WEB_HOST", "0.0.0.0")
WEB_PORT = _int("WEB_PORT", 8765)
# The URL handed to SAP -> Chrome; must resolve from the end-user PCs.
WEB_PUBLIC_URL = _env("WEB_PUBLIC_URL", f"http://localhost:{WEB_PORT}").rstrip("/")

# Development: POST /dev/open creates a session without the SAP GUI.
# Anyone who can reach the port can then chat as the RFC user -- never in
# production.
CURIE_DEV = _bool("CURIE_DEV", False)

# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #

SESSION_HEARTBEAT_SEC = _int("SESSION_HEARTBEAT_SEC", 3)
SESSION_HEARTBEAT_TIMEOUT = _float("SESSION_HEARTBEAT_TIMEOUT", 15.0)
BROWSER_TIMEOUT = _float("BROWSER_TIMEOUT", 10.0)
BROWSER_ATTACH_TIMEOUT = _float("BROWSER_ATTACH_TIMEOUT", 60.0)
SESSION_IDLE_TIMEOUT = _float("SESSION_IDLE_TIMEOUT", 1800.0)
SESSION_MAX = _int("SESSION_MAX", 20)
# Open sessions one SAP user may hold at a time (SERVER_BUSY beyond that).
SESSION_MAX_PER_USER = _int("SESSION_MAX_PER_USER", 3)
# Bind a session to the IP address of the first browser request. The SAP GUI
# and the Chrome window run on the same PC, so this is cheap protection
# for the URL token. Switch off behind a reverse proxy that hides the
# client address.
SESSION_BIND_IP = _bool("SESSION_BIND_IP", True)
# Log file (rotating, 5 x 5 MB); empty = console only.
LOG_FILE = _env("CURIE_LOG_FILE") or _env("RFC_SERVER_LOG")
# A relative path is taken from the project directory, not the working one.
SESSION_LOG_DIR = str(Path(BASE_DIR, _env("SESSION_LOG_DIR", "logs/sessions")))
# Tool results are logged in full up to this many characters of JSON (the
# archive reads structures, rows and domain values from the log); 0 logs
# only the summary.
SESSION_LOG_RESULT_CHARS = _int("SESSION_LOG_RESULT_CHARS", 20000)
# The archive (knowledge plan, phase K1): episodes built from the session
# logs, in one SQLite file. ARCHIVE_ENABLED=0 leaves the logs as they are.
ARCHIVE_ENABLED = _bool("ARCHIVE_ENABLED", True)
ARCHIVE_DB = str(Path(BASE_DIR, _env("ARCHIVE_DB", "archive/curie.db")))
# Housekeeping (curie --housekeeping): session log folders older than
# LOG_RETENTION_DAYS are imported into the archive and removed; archive
# episodes older than ARCHIVE_RETENTION_DAYS are removed. 0 keeps everything.
LOG_RETENTION_DAYS = _int("LOG_RETENTION_DAYS", 180)
ARCHIVE_RETENTION_DAYS = _int("ARCHIVE_RETENTION_DAYS", 180)
# Seconds one case may take in curie --eval (a stuck case costs this much).
EVAL_TIMEOUT = _float("EVAL_TIMEOUT", 600.0)

# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

READ_TABLE_DEFAULT_ROWS = _int("READ_TABLE_DEFAULT_ROWS", 50)
READ_TABLE_MAX_ROWS = _int("READ_TABLE_MAX_ROWS", 500)
READ_TABLE_DENY = _list("READ_TABLE_DENY", "USR02,USH02,USR40,USRBF*,PA0*,PCL*")
READ_TABLE_SPLIT = _bool("READ_TABLE_SPLIT", False)
READ_TABLE_CACHE_TTL = _float("READ_TABLE_CACHE_TTL", 60.0)
DDIC_CACHE_TTL = _float("DDIC_CACHE_TTL", 3600.0)
# Entries per cache (structures, interfaces, read results); least recently
# used entries go first. POST /admin/cache/flush from the Curie host itself
# empties them without a restart; /healthz shows hit and miss counters.
CACHE_MAX_ENTRIES = _int("CACHE_MAX_ENTRIES", 500)
# Keep the dictionary caches (structures, interfaces, domain values) on disk
# between restarts, under CACHE_DIR, for CACHE_PERSIST_TTL seconds (7 days).
# Table reads never persist. `curie --cache stats|warm|purge` manages it.
CACHE_PERSIST = _bool("CACHE_PERSIST", True)
CACHE_DIR = str(Path(BASE_DIR, _env("CACHE_DIR", "cache")))
CACHE_PERSIST_TTL = _float("CACHE_PERSIST_TTL", 7 * 86400.0)
CACHE_SAVE_INTERVAL = _float("CACHE_SAVE_INTERVAL", 60.0)

# --------------------------------------------------------------------------- #
# Knowledge (memory/store.py): Markdown notes about the system
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Languages and customer conventions (curie/lexicon.py, memory/shape.py)
# --------------------------------------------------------------------------- #
# The users' languages, ISO 639-1, for the word lists in curie/lexicon.py
# (follow-ups, general questions, months, stop words). English is always
# included. Default: the logon language and English.
CURIE_LANGUAGES = _list("CURIE_LANGUAGES") or [_env("RFC_LANG", "PL").lower(), "en"]
# A composite document key the users write in questions, e.g. an
# agreement as number/year ("100/2005"): KEY_PATTERN is a regex with one
# group per part, KEY_ROLES names the parts as they are stored in the
# tables with their zero padding ("nrum:7,rokum:0"). The names become the
# parameter names of report families. Empty: no composite key.
KEY_PATTERN = _env("KEY_PATTERN")
KEY_ROLES = _env("KEY_ROLES")
# Upper-case codes in questions that are never values of this system
# (column prefixes, registration prefixes), comma separated.
VALUE_STOP_CODES = [c.upper() for c in _list("VALUE_STOP_CODES")]


def key_roles() -> list[tuple[str, int]]:
    """[(role, zero padding)] from KEY_ROLES; roles without a width get 0."""
    out: list[tuple[str, int]] = []
    for item in (KEY_ROLES or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, _, width = item.partition(":")
        try:
            out.append((name.strip().lower(), int(width or 0)))
        except ValueError:
            out.append((name.strip().lower(), 0))
    return out


KNOWLEDGE_ENABLED = _bool("KNOWLEDGE_ENABLED", True)
KNOWLEDGE_DIR = str(Path(BASE_DIR, _env("KNOWLEDGE_DIR", "knowledge")))
KNOWLEDGE_BUILD_DIR = str(Path(KNOWLEDGE_DIR, _env("KNOWLEDGE_BUILD_DIR", ".build")))  # the compiled bundle
KNOWLEDGE_MAX_NOTES = _int("KNOWLEDGE_MAX_NOTES", 8)         # notes per prompt
KNOWLEDGE_MAX_CHARS = _int("KNOWLEDGE_MAX_CHARS", 6000)      # their total size
KNOWLEDGE_EXEMPLARS = _int("KNOWLEDGE_EXEMPLARS", 2)         # confirmed programs per prompt
# Keep a confirmed, successfully run program as an exemplar.
KNOWLEDGE_CAPTURE_EXEMPLARS = _bool("KNOWLEDGE_CAPTURE_EXEMPLARS", True)
# Stage two: pitfalls learned from fixed syntax/rule failures (one model
# call after a successful run that needed a fix round), and the report
# catalogue (programs the user marked correct, replayed for the same
# question shape without the model).
KNOWLEDGE_CAPTURE_INSIGHTS = _bool("KNOWLEDGE_CAPTURE_INSIGHTS", True)
KNOWLEDGE_MAX_INSIGHTS = _int("KNOWLEDGE_MAX_INSIGHTS", 4)     # pitfalls per prompt
KNOWLEDGE_CATALOGUE = _bool("KNOWLEDGE_CATALOGUE", True)
# The model as a router (memory/families.py route): when no family matches
# a question lexically but some share a table or words with it, the model
# picks one (or none) and names the parameter values; the pick is used only
# when the values are in the question and fill the template.
KNOWLEDGE_ROUTER = _bool("KNOWLEDGE_ROUTER", True)
KNOWLEDGE_ROUTER_MODEL = _env("KNOWLEDGE_ROUTER_MODEL") or LLM_AUDIT_MODEL
# The knowledge review job (memory/review.py): one model call per note not
# reviewed yet; report in knowledge/REVIEW.md. Interval in seconds for the
# in-process job, 0 = only on demand (curie --review-knowledge, or
# POST /admin/knowledge/review from the Curie host).
# Who may teach Curie with the Correct / Wrong buttons:
#   all     every user's vote credits or debits the notes and admits programs
#           to the report catalogue (the pilot phase);
#   owners  the buttons stay, but only the SAP users in KNOWLEDGE_OWNERS
#           (names or patterns, e.g. "THUPALO,KNOW*") change anything; other
#           votes are logged and archived, nothing else;
#   off     the buttons are not shown and the endpoint declines.
KNOWLEDGE_FEEDBACK = (_env("KNOWLEDGE_FEEDBACK", "all").strip().lower() or "all")
if KNOWLEDGE_FEEDBACK not in ("all", "owners", "off"):
    KNOWLEDGE_FEEDBACK = "all"
KNOWLEDGE_OWNERS = _list("KNOWLEDGE_OWNERS")


def is_owner(user: str) -> bool:
    """Is this SAP user a knowledge owner (KNOWLEDGE_OWNERS, patterns)?"""
    import fnmatch

    name = (user or "").upper()
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in KNOWLEDGE_OWNERS)


def feedback_counts(user: str) -> bool:
    """Does a vote by this user change the notes and the catalogue?"""
    if KNOWLEDGE_FEEDBACK == "all":
        return True
    if KNOWLEDGE_FEEDBACK == "owners":
        return is_owner(user)
    return False


KNOWLEDGE_REVIEW_INTERVAL = _float("KNOWLEDGE_REVIEW_INTERVAL", 0.0)
KNOWLEDGE_REVIEW_APPLY = _bool("KNOWLEDGE_REVIEW_APPLY", False)   # the job also applies safe fixes
KNOWLEDGE_REVIEW_MAX = _int("KNOWLEDGE_REVIEW_MAX", 50)           # notes per run
KNOWLEDGE_REVIEW_MODEL = _env("KNOWLEDGE_REVIEW_MODEL") or LLM_AUDIT_MODEL
# The consolidation worker (curie --consolidate): its model, and whether a
# generated system card goes straight into the bundle (else it stays a
# proposal in .build/card.proposed.md until --apply).
KNOWLEDGE_CONSOLIDATE_MODEL = _env("KNOWLEDGE_CONSOLIDATE_MODEL") or KNOWLEDGE_REVIEW_MODEL
KNOWLEDGE_CARD_AUTO = _bool("KNOWLEDGE_CARD_AUTO", False)
# An answer cut at LLM_MAX_OUTPUT_TOKENS is asked for again once with the
# budget doubled (reasoning models spend it on hidden thinking).
LLM_GROW_ON_TRUNCATION = _bool("LLM_GROW_ON_TRUNCATION", True)
# The light path (memory/threads.py is_general): a general question about
# SAP or about Curie ("jak wyświetlić saldo w SAP GUI", "who are you") is
# answered without tools, with the system card only and a small output
# budget. A follow-up that names data leaves the light path.
LLM_LIGHT_PATH = _bool("LLM_LIGHT_PATH", True)
LLM_LIGHT_MAX_TOKENS = _int("LLM_LIGHT_MAX_TOKENS", 1500)

# --------------------------------------------------------------------------- #
# Chat plugin: direct SAP -> model calls (Z_BLA_RFC_CHAT_*, Z_AI_CHAT_COMPLETION)
# --------------------------------------------------------------------------- #

# Off by default: nothing is registered, no service exists.
CHAT_PLUGIN = _bool("CHAT_PLUGIN", False)
# Logical model names the ABAP side may ask for (Z_AI_CHAT_COMPLETION-I_MODEL),
# "LOGICAL=model,LOGICAL=model"; the first is the default and serves the
# streamed calls. Empty: ZGPT-45B mapped to LLM_MODEL.
CHAT_MODELS = _env("CHAT_MODELS")
CHAT_MAX_TOKENS = _int("CHAT_MAX_TOKENS", LLM_MAX_OUTPUT_TOKENS)
# Prepended when the history has no system message. "@path" reads a file;
# {sysid}, {client} and {language} are replaced. Empty value = none.
CHAT_SYSTEM_PROMPT_DEFAULT = (
    "You are an assistant answering from inside SAP system {sysid} client {client}. "
    "Answer in {language} unless asked otherwise. Keep lines under 200 characters "
    "and use plain text: no Markdown tables, no code fences."
)
CHAT_SYSTEM_PROMPT = _env("CHAT_SYSTEM_PROMPT", CHAT_SYSTEM_PROMPT_DEFAULT) \
    if "CHAT_SYSTEM_PROMPT" not in os.environ else _env("CHAT_SYSTEM_PROMPT")
CHAT_READ_WAIT = _float("CHAT_READ_WAIT", 10.0)          # seconds a READ waits for the first chunk
CHAT_HANDLE_TTL = _float("CHAT_HANDLE_TTL", 300.0)       # seconds without READ before a handle is dropped
CHAT_MAX_HANDLES = _int("CHAT_MAX_HANDLES", 4)           # concurrent answers; keep below RFC_REG_COUNT
CHAT_MAX_INPUT_CHARS = _int("CHAT_MAX_INPUT_CHARS", 60000)
CHAT_SYNC_TIMEOUT = _float("CHAT_SYNC_TIMEOUT", 60.0)    # Z_AI_CHAT_COMPLETION deadline
# 1: one thread and one HTTP client per answer instead of tasks on Curie's loop.
CHAT_STREAM_THREADS = _bool("CHAT_STREAM_THREADS", False)


def chat_models() -> dict[str, str]:
    """CHAT_MODELS parsed; logical names upper case, insertion order kept."""
    out: dict[str, str] = {}
    for item in CHAT_MODELS.split(","):
        item = item.strip()
        if not item:
            continue
        logical, _, real = item.partition("=")
        logical = logical.strip().upper()
        if logical:
            out[logical] = real.strip() or LLM_MODEL
    if not out and LLM_MODEL:
        out["ZGPT-45B"] = LLM_MODEL
    return out


def chat_system_prompt() -> str:
    text = CHAT_SYSTEM_PROMPT
    if text.startswith("@"):
        try:
            return Path(BASE_DIR, text[1:]).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return text

# --------------------------------------------------------------------------- #
# ABAP execution
# --------------------------------------------------------------------------- #

ABAP_EXEC_MODE = _env("ABAP_EXEC_MODE", "job").lower()      # job | sync
ABAP_PROG_PREFIX = _env("ABAP_PROG_PREFIX", "ZAI_").upper()
ABAP_RUN_TIMEOUT = _float("ABAP_RUN_TIMEOUT", 300.0)
ABAP_FIX_RETRIES = _int("ABAP_FIX_RETRIES", 3)
ABAP_REQUIRE_CONFIRM = _bool("ABAP_REQUIRE_CONFIRM", True)
ABAP_AUDIT = _bool("ABAP_AUDIT", True)
ABAP_AUDIT_BLOCKS = _bool("ABAP_AUDIT_BLOCKS", True)
# A program whose shape equals one the reviewer already allowed (and the
# user confirmed) keeps that verdict; the card says so (memory/audit_memo.py).
ABAP_AUDIT_MEMO = _bool("ABAP_AUDIT_MEMO", True)
# Deterministic repairs before the syntax check (sap_tools.abap_source
# .repair_source): a period before ELSE/ENDIF, an indented `*` comment,
# UP TO n ROWS after WHERE. Each repair is shown to the user.
ABAP_REPAIR = _bool("ABAP_REPAIR", True)
ABAP_MAX_SELECT_ROWS = _int("ABAP_MAX_SELECT_ROWS", 1000)
ABAP_INTERPRET_RESULT = _bool("ABAP_INTERPRET_RESULT", True)
ABAP_CHECK_FM = _env("ABAP_CHECK_FM", "Z_AI_PROG_CHECK_AND_EXECUTE")

# Which function modules generated programs may CALL FUNCTION.
#   ABAP_FUNCTION_POLICY=deny   everything is allowed except ABAP_FUNCTION_DENY
#   ABAP_FUNCTION_POLICY=allow  nothing is allowed except ABAP_FUNCTION_ALLOW
# ABAP_FUNCTION_ALLOW always wins over the deny list, so one entry can
# re-enable a single module that a deny pattern covers. Patterns are
# fnmatch-style (* and ?), case-insensitive, comma separated.
ABAP_FUNCTION_POLICY = _env("ABAP_FUNCTION_POLICY", "deny").lower()
ABAP_FUNCTION_DENY = _list("ABAP_FUNCTION_DENY", ",".join([
    "BAPI_TRANSACTION_COMMIT", "DB_COMMIT", "SXPG_*", "RFC_REMOTE_*", "TH_*",
    "BAPI_*_CREATE*", "BAPI_*_CHANGE*", "BAPI_*_DELETE*", "BAPI_*_SAVE*",
    "BAPI_*_POST*", "BAPI_*_CANCEL*", "BAPI_*_RELEASE*", "BAPI_*_CONFIRM*",
    "ENQUEUE_*", "DEQUEUE_*", "JOB_OPEN", "JOB_CLOSE", "JOB_SUBMIT", "BP_*",
    "RSPO_*", "SUSR_*", "BAPI_USER_*", "PRGN_*", "ARCHIVE_*", "SO_*_SEND*",
    "SCMS_*", "GUI_*", "WS_*", "*_UPLOAD", "*_DOWNLOAD", "SAPGUI_*",
    "RFC_ABAP_INSTALL_AND_RUN", "Z_AI_*",
]))
ABAP_FUNCTION_ALLOW = _list("ABAP_FUNCTION_ALLOW")


def function_allowed(name: str) -> bool:
    """May a generated program call this function module?"""
    import fnmatch

    name = name.upper()
    if any(fnmatch.fnmatchcase(name, p) for p in ABAP_FUNCTION_ALLOW):
        return True
    if ABAP_FUNCTION_POLICY == "allow":
        return False
    return not any(fnmatch.fnmatchcase(name, p) for p in ABAP_FUNCTION_DENY)

# --------------------------------------------------------------------------- #
# Release profiles
# --------------------------------------------------------------------------- #

PROFILES = {
    "45B": {"line_width": 72, "codepage": "iso-8859-2", "dialect": "classic"},
    "ECC6": {"line_width": 255, "codepage": "utf-8", "dialect": "7.0"},
}


def profile() -> dict:
    """The active release profile (falls back to 45B, the stricter one)."""
    return PROFILES.get(SAP_RELEASE or "45B", PROFILES["45B"])


def release_from_info(rfcsi: dict) -> str:
    """Map RFC_SYSTEM_INFO.RFCSAPRL ('45B', '46C', '700', '731', ...) to a profile."""
    rel = str(rfcsi.get("RFCSAPRL", "")).strip().upper()
    if not rel:
        return "45B"
    if rel[:1].isdigit() and rel[0] == "4":
        return "45B"
    return "ECC6"


# --------------------------------------------------------------------------- #
# Client helpers
# --------------------------------------------------------------------------- #


def open_connection():
    """A single client connection (sapnwrfc720.Client) with the .env logon."""
    from curie import sapnwrfc720 as sap

    client = sap.Client(SAP_RFC)
    client.open()
    return client


def check_connection() -> dict:
    """Smoke-test the client direction; returns RFC_SYSTEM_INFO of the backend."""
    client = open_connection()
    try:
        return client.call("RFC_SYSTEM_INFO")["RFCSI_EXPORT"]
    finally:
        client.close()


def problems() -> list[str]:
    """Configuration errors that make a start pointless."""
    out = []
    if not SAP_RFC["ashost"]:
        out.append("RFC_ASHOST is not set")
    if not SAP_RFC["user"]:
        out.append("RFC_USER is not set")
    if not SAP_RFC_SERVER["PROGRAM_ID"]:
        out.append("RFC_PROGID is not set")
    if not LLM_MODEL:
        out.append("LLM_MODEL is not set")
    if ABAP_EXEC_MODE not in ("job", "sync"):
        out.append(f"ABAP_EXEC_MODE must be job or sync, not {ABAP_EXEC_MODE!r}")
    if ABAP_FUNCTION_POLICY not in ("deny", "allow"):
        out.append(f"ABAP_FUNCTION_POLICY must be deny or allow, not {ABAP_FUNCTION_POLICY!r}")
    if SAP_RELEASE and SAP_RELEASE not in PROFILES:
        out.append(f"SAP_RELEASE must be one of {', '.join(PROFILES)}")
    if len(SAP_LANGU) != 1:
        out.append("SAP_LANGU must be a single character")
    return out


if __name__ == "__main__":
    info = check_connection()
    print(
        f"connected to {info['RFCHOST']} "
        f"{info['RFCSYSID']}/{SAP_RFC['client']} release {info['RFCSAPRL']} "
        f"({info['RFCPROTO']}, {info['RFCCHARTYP']}) -> profile "
        f"{release_from_info(info)}"
    )
