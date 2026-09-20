# 13 Status and roadmap

As of 6 September 2026.

## Verified on the live system (TC0, R/3 4.5B, client 005)

| Area | Evidence |
|---|---|
| RFC bridge both directions on SDK 7.20 | `--check` lists every interface; `--selftest` six rounds through `SAP_AI` |
| Tools | `RFC_READ_TABLE` on T000, DD03L, TBTCO, TRDIR; MARA structure with key flags and Polish texts; function interface of `RFC_READ_TABLE` |
| Chat | A data question answered in Polish with a corrected tool call after a field-name error |
| Code path | Static rules, syntax check (good and bad), model audit, confirmation |
| Execution | Synchronous mode and the job path: `INSERT REPORT`, job under the end user, `ZAI_RUNNER`, `Z_AI_LIST_DELIVER`, list in the browser, model interpretation, program deleted |
| SAP GUI entry | `ZAI_START` opens Chrome with the prompt pre-filled; SAP dot green |
| Job abort path | XBP 1.0 logon and `BAPI_XBP_JOB_ABORT` reachable (tested against a non-existent job) |
| Operations | `--check`, `--selftest`, rotating log; the service installer is written but was not executed here |

## Phases of the plan

| Phase | State |
|---|---|
| 0 Spikes on 4.5B | done 5 Sep |
| 1 Session plumbing, `ZAI_START` in the GUI | done 6 Sep |
| 2 LLM chat and tools | done 5 Sep |
| 3 ABAP code path | done 5 Sep |
| 5 Learning, stage one (notes, hints, exemplars, domain values) | done 6 Sep; stages two (insights, Z-table index) and three (template replay, embeddings) open |
| 4 Hardening and operations | done 6 Sep: IP binding, per-user limit, Stop aborts jobs, `--selftest`, rotating log, NSSM script, function policy, codepage transliteration, `/reset` |

## Findings log

| Date | Finding | Consequence |
|---|---|---|
| 5 Sep | Polish language key is `L` | `ISO_TO_SAP_LANGU` in `config.py` |
| 5 Sep | `RFC_GET_STRUCTURE_DEFINITION_P` has no language parameter; `RFC_GET_FUNCTION_INTERFACE_P` uses `PARAMS_P` | `call_known()` drops unknown parameters |
| 5 Sep | Table lines are flat fields named after the type | single-field lines mapped by position |
| 5 Sep | `Z_AI_HEARTBEAT` had `E_STATE`/`E_STATUS_TEXT` as IMPORT | fixed in SE37 |
| 5 Sep | `PERFORM list_to_table` without the FORM: group did not compile | FORM installed after `ENDFUNCTION` |
| 5 Sep | `ZAI_RUNNER` upper-cased the session id | ids are upper-case hex |
| 5 Sep | A handler's ABAP exception made the server re-register | dispatch treats it as handled |
| 5 Sep | A stale `curie.tools.rfc_server` from another project held `SAP_AI` | `--selftest` names the case; "one Program ID, one process" |
| 6 Sep | `WS_EXECUTE` cuts the command line at 128 characters | `ZAI_START` uses `GUI_RUN` |
| 6 Sep | An en dash in a comment blocked a program | transliteration instead of rejection |
| 6 Sep | The card said "Syntax error" when the check had not run | "Syntax: not checked" |
| 6 Sep | `BP_JOB_ABORT` not remote-enabled on 4.5B; XBP needs version 1.0 | fallback chain in `AbapExecutor._abort_job` |
| 6 Sep | The startup sweep would have deleted `ZAI_RUNNER`/`ZAI_START` | only prefix + 8 hex names are swept |
| 6 Sep | Caches had no size bound, no counters and no flush; the read cache had no lock | one `TTLCache` helper with LRU, counters on `/healthz`, `POST /admin/cache/flush` |
| 6 Sep | Learning stage one: knowledge notes, `/remember`, exemplar capture, domain values | chapter 14 |

## Open items

| Item | Notes |
|---|---|
| ECC 6 verification | The code carries the `ECC6` profile and the interface differences are handled generically, but no ECC system has been connected yet |
| HTTPS | Reverse proxy with TLS in front of port 8765; then `SESSION_BIND_IP=0` unless client addresses are preserved |
| Long initial prompts | Curie accepts `T_PROMPT`; `ZAI_START` still passes one 255-character line |
| Selection screens for generated programs | V1 forbids `PARAMETERS`; a `RSPARAMS` table with `WITH SELECTION-TABLE` would allow them |
| Service installation | `service\install-service.ps1` was written but not run; needs NSSM and an elevated shell |
| Windows session for Chrome profile | `%TEMP%\chrome-kiosk-profile` is shared by all sessions of one user; a second session reuses the window's profile, which is fine, but a lock could appear if Chrome was killed uncleanly |
| Model quality | Field-name guesses and modern syntax are caught by the check loop; a small corpus of 4.5B examples in the prompt would reduce retries |
| Dev tools placement | `curie.tools.phase0` and `curie.tools.devchat` could move under `tools/` |
| Learning stage two live test | Insights, feedback buttons and the report catalogue built 9 Sep (chapter 14); to verify on TC0: a fix round that yields a pitfall, Correct on a run result, then the same question with another year replayed from the catalogue |
| Chat plugin live test | Code, tests and docs done 9 Sep (chapter 15); waits for `ZBLA_RFC_*` structures and `Z_BLA_RFC_CHAT_*` in TC0, then `--selftest` and transaction ZBLA with `gc_backend = 'R'` |
