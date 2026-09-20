# 04 Configuration

All settings come from `.env`, read by [`config.py`](../src/curie/config.py). Values
may carry an inline `# comment`; an empty value followed by a comment counts
as empty. Lists are comma separated, case-insensitive, with `*` and `?`
wildcards where patterns are mentioned. A change needs a restart (under
`curie.tools.rfc_dev` the restart is automatic; the parent `.env` is watched too).

## SAP logon and gateway

| Variable | Default | Meaning |
|---|---|---|
| `RFC_USER`, `RFC_PASS` | | Technical RFC user for the client direction |
| `RFC_ASHOST`, `RFC_SYSNR`, `RFC_CLIENT`, `RFC_LANG` | `00`, `100`, `PL` | Application server, system number, client, logon language (ISO) |
| `RFC_GWHOST`, `RFC_GWSERV` | `RFC_ASHOST`, `sapgw00` | Gateway to register at |
| `RFC_PROGID` | | Program ID of the SM59 T destination (`SAP_AI`) |
| `RFC_SAPROUTER` | | SAProuter string, applied to both directions |
| `RFC_REG_COUNT` | `3` | Gateway registrations = parallel SAP→Curie calls |
| `RFC_DISPATCH_TIMEOUT` | `5` | Idle wait per registration; bounds shutdown |
| `RFC_CLIENT_POOL` | `2` | Client connections for tool calls and execution |
| `RFC_CALL_TIMEOUT` | `120` | Reserved for per-call timeouts |
| `RFC_SERVER_TRACE` | | SDK trace level 0-3 (`rfc*.trc`) |
| `SAPNWRFC720_HOME` | `C:\nwrfcsdk` | SDK location |

## Target system profile

| Variable | Default | Meaning |
|---|---|---|
| `SAP_RELEASE` | autodetect | `45B` or `ECC6`. From `RFC_SYSTEM_INFO` when empty (`4xx` → 45B, else ECC6). Selects dialect rules in the prompt and the source width (72 / 255) |
| `SAP_LANGU` | derived from `RFC_LANG` | One-character `SY-LANGU` for `RFC_GET_FUNCTION_INTERFACE_P` and dictionary texts (`PL` → `L`, `EN` → `E`, `DE` → `D`, ...). 4.5B accepts nothing else |

## LLM

| Variable | Default | Meaning |
|---|---|---|
| `LLM_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible endpoint (OpenRouter, Azure, vLLM, Ollama, ...) |
| `LLM_API_KEY`, `LLM_MODEL` | | Credentials and model |
| `LLM_TOOL_CHOICE` | `auto` | How tools are offered: `auto` (tools with `tool_choice: auto`), `omit` (tools without the hint, for vLLM started without `--enable-auto-tool-choice`), `off` (no tools). Curie steps down by itself on the server's 400s; this is the starting point |
| `LLM_AUDIT_MODEL` | `LLM_MODEL` | Model for the security review |
| `LLM_TIMEOUT` | `120` | HTTP timeout in seconds |
| `LLM_MAX_TOOL_ROUNDS` | `8` | Tool rounds per turn |
| `LLM_MAX_OUTPUT_TOKENS` | `4096` | |
| `LLM_HISTORY_MAX_TOKENS` | `24000` | History budget (chars/4); tool groups are never split |
| `LLM_STREAM` | `1` | `0` sends non-streamed requests for servers with poor tool-call streaming |
| `LLM_TEMPERATURE` | `0.2` | Chat temperature; the audit always uses 0 |
| `LLM_ANSWER_LANGUAGE` | `RFC_LANG` | Language the assistant answers in |
| `TOOL_RESULT_MAX_CHARS` | `12000` | Tool results are cut to this before they reach the model |

## Web

| Variable | Default | Meaning |
|---|---|---|
| `WEB_HOST`, `WEB_PORT` | `0.0.0.0`, `8765` | Bind address |
| `WEB_PUBLIC_URL` | `http://localhost:8765` | The URL SAP hands to Chrome; must resolve from the end-user PCs |
| `CURIE_DEV` | `0` | `1` enables `POST /dev/open` (sessions without SAP GUI). Development only |

## Sessions

| Variable | Default | Meaning |
|---|---|---|
| `SESSION_HEARTBEAT_SEC` | `3` | Returned to SAP as `E_HEARTBEAT_SEC`; the loop interval |
| `SESSION_HEARTBEAT_TIMEOUT` | `15` | Seconds without a heartbeat before the session closes (`sap_gone`) |
| `BROWSER_TIMEOUT` | `10` | Seconds without a ping and without an event stream before `browser_gone` |
| `BROWSER_ATTACH_TIMEOUT` | `60` | Seconds a fresh session waits for its browser |
| `SESSION_IDLE_TIMEOUT` | `1800` | Seconds without activity in IDLE or CONFIRM |
| `SESSION_MAX` | `20` | Open sessions in total |
| `SESSION_MAX_PER_USER` | `3` | Open sessions per SAP user (system, client, user) |
| `SESSION_BIND_IP` | `1` | Bind a session to the IP of its first browser request; `0` behind a reverse proxy |
| `SESSION_LOG_DIR` | `logs/sessions` | JSONL audit trail, relative to the project directory |

## Tools

| Variable | Default | Meaning |
|---|---|---|
| `READ_TABLE_DEFAULT_ROWS` | `50` | Rows when the model gives no `max_rows` |
| `READ_TABLE_MAX_ROWS` | `500` | Hard cap per call |
| `READ_TABLE_DENY` | `USR02,USH02,USR40,USRBF*,PA0*,PCL*` | Tables the tool refuses and generated code may not read |
| `READ_TABLE_SPLIT` | `0` | Reserved (multi-call field stitching) |
| `READ_TABLE_CACHE_TTL` | `60` | Result cache seconds |
| `DDIC_CACHE_TTL` | `3600` | Structure and interface cache seconds (also the interfaces Curie itself calls) |
| `CACHE_MAX_ENTRIES` | `500` | Entries per cache; least recently used entries are evicted first |
| `CACHE_PERSIST` | `1` | Keep the dictionary caches (structures, function interfaces, domain values) on disk between restarts |
| `CACHE_DIR` | `cache` | Where, relative to the project directory |
| `CACHE_PERSIST_TTL` | `604800` | Age limit of persisted entries (seven days); persistent caches use it instead of `DDIC_CACHE_TTL` |
| `CACHE_SAVE_INTERVAL` | `60` | Seconds between saves of changed caches (also at shutdown) |

## Languages and customer conventions

Curie has no language or customer name in its code. The word lists that
read questions (follow-ups, general questions, program requests, month
names, stop words) live per language in [`lexicon.py`](../src/curie/lexicon.py);
the conventions of one installation live here.

| Variable | Default | Meaning |
|---|---|---|
| `CURIE_LANGUAGES` | `RFC_LANG` + `en` | ISO 639-1 codes of the users' languages; the word lists of these languages are active, English always (12) |
| `KEY_PATTERN` | | A composite document key the users write, as a regex with one group per part: an agreement written `100/2005` is `\b(\d{1,7})/(\d{4})\b`. Empty: no such key (14) |
| `KEY_ROLES` | | The parts of the key as stored in the tables, with zero padding: `nrum:7,rokum:0`. The names become the parameter names of report families |
| `VALUE_STOP_CODES` | | Upper-case codes that appear in questions but are never data values (column prefixes, registration prefixes): `PZST,TOW,DX` |

## Knowledge

| Variable | Default | Meaning |
|---|---|---|
| `KNOWLEDGE_ENABLED` | `1` | The notes layer (see 14) |
| `KNOWLEDGE_DIR` | `knowledge` | Folder of Markdown notes, relative to the project directory |
| `KNOWLEDGE_BUILD_DIR` | `.build` | The compiled bundle, relative to `KNOWLEDGE_DIR` (14) |
| `KNOWLEDGE_MAX_NOTES`, `KNOWLEDGE_MAX_CHARS` | `8`, `6000` | Notes per prompt and their total size |
| `KNOWLEDGE_EXEMPLARS` | `2` | Confirmed programs per prompt |
| `KNOWLEDGE_CAPTURE_EXEMPLARS` | `1` | Keep confirmed programs that ran |
| `KNOWLEDGE_CAPTURE_INSIGHTS` | `1` | Distil fixed syntax/rule failures into one-line pitfalls (one model call per accepted program that needed a fix) |
| `KNOWLEDGE_MAX_INSIGHTS` | `4` | Pitfalls per prompt |
| `KNOWLEDGE_CATALOGUE` | `1` | Replay programs marked correct for the same question shape without the model |
| `KNOWLEDGE_ROUTER`, `KNOWLEDGE_ROUTER_MODEL` | `1`, audit model | When no family matches a question lexically, the model picks one among the families that share a table or words with it (14); used only when the values it names are in the question and fill the template |
| `KNOWLEDGE_FEEDBACK`, `KNOWLEDGE_OWNERS` | `all`, | Who may teach with Correct / Wrong (14): `all` (every vote counts), `owners` (buttons for everyone, only the SAP users matching `KNOWLEDGE_OWNERS` patterns change notes or admit programs to the catalogue; other votes are logged only), `off` (no buttons) |
| `KNOWLEDGE_REVIEW_INTERVAL`, `KNOWLEDGE_REVIEW_APPLY`, `KNOWLEDGE_REVIEW_MAX`, `KNOWLEDGE_REVIEW_MODEL` | `0`, `0`, `50`, audit model | The knowledge review job (14): interval in seconds (0 = on demand), apply safe fixes, notes per run, model |
| `KNOWLEDGE_CONSOLIDATE_MODEL`, `KNOWLEDGE_CARD_AUTO` | review model, `0` | The consolidation worker's model; whether a generated system card goes straight into the bundle (14) |
| `ARCHIVE_ENABLED`, `ARCHIVE_DB` | `1`, `archive/curie.db` | The archive of episodes (14): built from the session logs at session close and by `curie --archive import` |
| `LOG_RETENTION_DAYS`, `ARCHIVE_RETENTION_DAYS` | `180`, `180` | `curie --housekeeping`: session log folders older than this are imported into the archive and removed; archive episodes older than this are removed (0 keeps everything) |
| `EVAL_TIMEOUT` | `600` | Seconds one case may take in `curie --eval`, all its turns together |
| `SESSION_LOG_RESULT_CHARS` | `20000` | Tool results are logged in full up to this many characters of JSON (0: summary only) |
| `LLM_STREAM_USAGE` | `1` | Ask for token usage on streamed answers (`stream_options`); switched off for the process when the server rejects it |
| `LLM_GROW_ON_TRUNCATION` | `1` | An answer cut at `LLM_MAX_OUTPUT_TOKENS` is asked for again once with twice the budget |
| `LLM_LIGHT_PATH`, `LLM_LIGHT_MAX_TOKENS` | `1`, `1500` | General questions about SAP or about Curie (no data named) are answered without tools, with the system card only and this output budget (07) |

## Chat plugin

Off by default; see 15 for the meaning of each setting.

| Variable | Default | Meaning |
|---|---|---|
| `CHAT_PLUGIN` | `0` | `1` serves `Z_BLA_RFC_CHAT_*` and `Z_AI_CHAT_COMPLETION` on the same Program ID |
| `CHAT_MODELS` | `ZGPT-45B=<LLM_MODEL>` | Logical model names, `LOGICAL=model,...`; the first is the default |
| `CHAT_MAX_TOKENS` | `LLM_MAX_OUTPUT_TOKENS` | Answer length cap |
| `CHAT_SYSTEM_PROMPT` | short built-in text | Prepended when the history has no system message; empty = none; `@file` reads a file |
| `CHAT_READ_WAIT` | `10` | Seconds a READ waits for the first chunk |
| `CHAT_HANDLE_TTL` | `300` | Seconds without READ before an answer is dropped |
| `CHAT_MAX_HANDLES` | `4` | Concurrent answers; keep below `RFC_REG_COUNT` |
| `CHAT_MAX_INPUT_CHARS` | `60000` | History size sent to the model |
| `CHAT_SYNC_TIMEOUT` | `60` | `Z_AI_CHAT_COMPLETION` deadline in seconds |
| `CHAT_STREAM_THREADS` | `0` | `1`: one thread and HTTP client per answer |

## ABAP execution

| Variable | Default | Meaning |
|---|---|---|
| `ABAP_EXEC_MODE` | `job` | `job`: background job as the end user; `sync`: in the RFC call as the RFC user |
| `ABAP_PROG_PREFIX` | `ZAI_` | Generated names are prefix + 8 hex digits |
| `ABAP_RUN_TIMEOUT` | `300` | Seconds to wait for the list; then `TBTCO` is consulted |
| `ABAP_FIX_RETRIES` | `3` | Automatic fix rounds after rule or syntax failures |
| `ABAP_REQUIRE_CONFIRM` | `1` | `0` runs allowed code without the button (not recommended) |
| `ABAP_AUDIT` | `1` | Model review on or off |
| `ABAP_AUDIT_BLOCKS` | `1` | A negative review blocks the Run button |
| `ABAP_AUDIT_MEMO` | `1` | A program whose shape (literals blanked, names kept) equals one the reviewer allowed and the user confirmed keeps that verdict without a second model call; the card says so (08) |
| `ABAP_REPAIR` | `1` | Deterministic repairs before the syntax check: a period missing before ELSE/ENDIF, an indented `*` comment, `UP TO n ROWS` after WHERE, `DESC`/`ASC` for `DESCENDING`/`ASCENDING`; each is shown to the user (08) |
| `ABAP_MAX_SELECT_ROWS` | `1000` | Largest `UP TO n ROWS` accepted; the prompt tells the model |
| `ABAP_INTERPRET_RESULT` | `1` | Hand the list back to the model for a summary |
| `ABAP_CHECK_FM` | `Z_AI_PROG_CHECK_AND_EXECUTE` | The check/execute function module |
| `ABAP_FUNCTION_POLICY` | `deny` | `deny`: all but the deny list; `allow`: only the allow list |
| `ABAP_FUNCTION_DENY` | see `.env.example` | Patterns for forbidden `CALL FUNCTION` names |
| `ABAP_FUNCTION_ALLOW` | | Patterns that are always allowed; wins over the deny list |

## Logging

| Variable | Default | Meaning |
|---|---|---|
| `CURIE_LOG_FILE` (or `RFC_SERVER_LOG`) | | Rotating application log (5 × 5 MB) relative to the project directory |
