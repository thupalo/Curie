# 11 Operations

## Running

| Command | Purpose |
|---|---|
| `curie` | Serve until Ctrl+C |
| `curie --check` | Configuration, SAP logon, every FM interface, `T_SOURCE` width, LLM round trip |
| `curie --selftest [--rounds n] [--dest SAP_AI]` | With Curie running: round trips from inside SAP through the destination |
| `python -m curie.tools.rfc_dev` | Serve, restarted when a source file or `.env` changes (development) |
| `curie -v` | Debug logging |

One Program ID, one process. A second server registered as `SAP_AI` makes
the gateway alternate between them; the symptom is "function not found" on
some calls while the SM59 test passes. `--selftest` names this case. The
plain "program SAP_AI not registered" means no server at all: Curie is not
running.

## Windows service

```powershell
# elevated PowerShell, nssm.exe on PATH (https://nssm.cc)
cd sap_ai_chat
.\service\install-service.ps1            # install and start
.\service\install-service.ps1 -Uninstall
```

The maintenance jobs (chapter 14) can run as Windows scheduled tasks:

```powershell
.\service\install-tasks.ps1                 # register "Curie review|consolidate|eval|housekeeping", triggers disabled
.\service\install-tasks.ps1 -Start review   # run one now; output in service\logs\task-review.log
.\service\install-tasks.ps1 -Enable         # daily review 02:00, weekly consolidate (Sun 03:00) and housekeeping (Sat 04:00)
.\service\install-tasks.ps1 -Remove
```

The eval task has no trigger and is always started by hand. On a
development host register the tasks without `-Enable` and start them
when the model is free.

The service runs `venv\Scripts\python.exe -m curie` in the project
directory (so `.env` is found), restarts after a failure with a 5-second
delay, and stops by sending Ctrl+C, which Curie treats as a normal
shutdown. Console output goes to `service\logs\curie-stdout.log` (rotated
by NSSM). Set `CURIE_LOG_FILE=curie.log` for the application's own rotating
log.

## Logs

| Where | What |
|---|---|
| `curie --cache stats` / `warm [--tables A,B] [--from-knowledge]` / `purge [names]` | The persisted dictionary caches: what is on disk, prefetch structures and interfaces so the first dialog after a restart is warm, drop the files (chapter 07) |
| `curie --review-knowledge [--apply] [--all] [--no-sap]` | Review the knowledge notes with the model: facts against the dictionary, follow-up questions, duplicates; report in `knowledge/REVIEW.md` (chapter 14) |
| `curie --purge-knowledge --last 15m` / `--all` [`--types t,t`] [`--no-archive`] [`--no-reports`] [`--yes`] | Move the notes of every type, the `EVAL*`/`REVIEW` reports and the archived episodes (chat history) of a window to `knowledge/.trash/`; `--all` also moves the archive file and the bundle and asks twice (chapter 14) |
| `curie --eval [--limit N] [--cases id,id] [--timeout s] [--include-general]` | Replay the archive's confirmed questions through the real turn without running anything; `knowledge/EVAL.md` with the previous run beside (chapter 14); needs SAP and the model. Threads whose opening question was answered without asking SAP are left out unless `--include-general` |
| `curie --consolidate [--no-llm] [--no-sap] [--apply]` | The consolidation worker: report families, the glossary, the dictionary index, the system card proposal, the bundle (chapter 14); needs the model unless `--no-llm` and SAP unless `--no-sap` |
| `curie --trace <file \| session id prefix>` / `--trace --newest` / `--full` | The timeline of one session log, one line per event: the model calls with budget, tokens and duration, tool calls, repairs, syntax errors, the audit verdict and its memo flag, notices, the close reason. A session id prefix is searched under the log folder, eval included; `--newest` takes the latest session of any kind by its own open time, not by file time, `--newest eval` or `--newest user` the latest of that kind; `--full` prints answers and tool results whole. `EVAL.md` names each case's session in its Log column |
| `curie --housekeeping [--dry-run]` | Retention: session log folders older than `LOG_RETENTION_DAYS` are imported into the archive and removed, archive episodes older than `ARCHIVE_RETENTION_DAYS` removed and the file compacted |
| `curie --archive import [--force]` / `stats` / `purge --last 15m [--yes]` | The archive of episodes built from the session logs: back-fill or refresh it, show counts and outcomes, remove the episodes of a test window (chapter 14) |
| Startup LLM check | Curie pings the model once at start, before registering at the gateway. A failure stops the start with exit code 2; when the model name is unknown to the endpoint the served names are listed (`set LLM_MODEL to one of them`). The same hint appears in the browser notice of a failed turn and under `llm` on `/healthz` |
| Console / `CURIE_LOG_FILE` | Startup, registrations, sessions opened and closed, handler calls, warnings, tool timings with `-v` |
| `logs/sessions/<date>/<id>.jsonl` | The audit trail per session (see 06) |
| `rfc*.trc` | SDK traces when `RFC_SERVER_TRACE` is set |

## Health

`GET /healthz` returns `{ok, sessions, caches}`; the `caches` object lists
entries, hits, misses and evictions per cache. After a transport that
changed a table or a function module interface, run from the Curie host:

```powershell
Invoke-RestMethod -Method Post http://localhost:8765/admin/cache/flush
```

Otherwise changes are seen after `DDIC_CACHE_TTL` (one hour). The SAP-side view: SM59 connection
test (`RFC_PING`), SMGW logged-on clients (the registrations), SM37 for
`ZAI_*` jobs.

## Restart and upgrade

Stop Curie (Ctrl+C or service stop): the sessions are closed first, so
the browsers get `session_closed` and show the overlay, their event
streams end, and uvicorn's wait for open connections is short; then the
close callbacks are waited for (the archive import of each session's
log, up to 15 seconds), the caches are saved, the RFC server is
deregistered and the archive closed, ending with `stopped` in the log.
The SAP loops end on their next heartbeat within `SESSION_HEARTBEAT_SEC`.
Start the new version. Users start `ZAI` again. There is no persistent
state to migrate; the session logs are append-only files.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `--check`: `RFC_ASHOST is not set` and friends | `.env` not found or values empty; it is looked for in `sap_ai_chat/` and one level up |
| `--check`: `SAP_RELEASE must be one of` | An inline comment after an empty value in an old `config.py`; fixed, but keep `SAP_RELEASE=` clean |
| `FU_NOT_FOUND` at startup | A served FM does not exist in the backend; install the `ZAIC` group first |
| `CALL_FUNCTION_FORMAT_NOT_SUP` | A 7.50-based server answered a non-Unicode backend (pre-4.6C, or any non-Unicode kernel); against such a system Curie must run on SDK 7.20. Unicode backends work with 7.50 |
| `Could not open the ICU common library` | `SAPNWRFC720_HOME` wrong, or a 7.50 lib directory on PATH |
| Registration refused | Gateway ACL (`reg_info`, `gw/reg_no_conn_info`) |
| `Z_AI_SESSION_OPEN not found` from SAP | Another server holds the Program ID; stop it |
| `program SAP_AI not registered` | Curie not running or registered at another gateway |
| `ZAI_START`: heartbeat ends with `SYSTEM_FAILURE` at once | `E_STATE`/`E_STATUS_TEXT` declared as IMPORT; make them EXPORT |
| `SYNTAX_ERROR` in `SAPLZAIC` on every check | The function group does not compile (missing FORM `list_to_table`); activate it in SE80 |
| Chrome does not open | `WS_EXECUTE` truncates at 128 characters; `ZAI_START` must use `GUI_RUN`; check `p_chrome` path; the SAP GUI security module may need a one-time allow |
| Page says "Session ended: SAP session stopped sending heartbeats" | The GUI session was ended or the network dropped; start `ZAI` again |
| `UNKNOWN_RUN` in the log after a job | The delivery named a run Curie does not know: an old `ZAI_RUNNER` with an upper-cased session id, or Curie restarted meanwhile |
| Job finishes but no list | The job could not reach `SAP_AI` (job log in SM37); check `S_RFC` of the end user for `ZAIC`, and the destination |
| Job finishes within a second, no list, on a system with several application servers | The `SAP_AI` destination has no gateway of its own (`RFCOPTIONS` shows `H=%%RFCSERVER%%`): the job looks for the registered program at the gateway of the server it happens to run on, where Curie is not registered. In SM59 give the destination the gateway Curie registers at (`RFC_GWHOST` / `RFC_GWSERV`) under "Gateway options" |
| `DATA_BUFFER_EXCEEDED` notes in tool results | Normal: Curie retried with fewer fields; the model can ask for specific ones |
| Stop reports "could not be aborted" | `S_XMI_PROD` missing for the RFC user, or no XBP on the backend; the job finishes on its own |
| `Characters outside the SAP codepage were replaced` | Informational: dashes or quotes from the model were transliterated |
| Sessions from a second PC get 403 | `SESSION_BIND_IP`; expected. Set to 0 behind a proxy |
