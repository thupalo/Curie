# Curie – SAP-side objects

Everything in this directory is written for **SAP R/3 4.5B** (no ABAP
Objects, no STRING, 72-column source, `OCCURS ... WITH HEADER LINE`) and is
also valid on **ECC 6.0**. Sources are meant to be pasted into SE38/SE37;
there is no transport file.

See `CURIE_PLAN.md` sections 4.1–4.4 for the design; this README is the
installation guide.

## Files

| File | Object | Kind |
|---|---|---|
| `ZAI_DDIC.md` | `ZAI_LINE`, `ZAI_LISTLINE`, `ZAI_PARAMS` | SE11 structures |
| `ZAI_FUNCTION_GROUP.md` | function group `ZAIC`, interfaces of `Z_AI_SESSION_OPEN`, `Z_AI_HEARTBEAT`, `Z_AI_SESSION_CLOSE`, `Z_AI_LIST_DELIVER` (served by Curie, empty body) and `Z_AI_PROG_CHECK_AND_EXECUTE` | SE37 |
| `Z_AI_PROG_CHECK_AND_EXECUTE.abap` | the complete source of the only module implemented in ABAP, FORM `LIST_TO_TABLE` included: paste it as the module's source | SE37 |
| `ZAI_RUNNER.abap` | wrapper report executed by the background job as the end user; runs the generated program and pushes its list to Curie | SE38 |
| `ZAI_START.abap` | end-user entry report (transaction `ZAI`): ping, open session, start Chrome, heartbeat loop | SE38 |
| `ZAI_TEST_HELLO.abap` | tiny sample program to test the execution path by hand | paste into Curie / SE37 test |

## What each object does

- **ZAI_LINE / ZAI_LISTLINE** – line structures for source code (72 chars on
  4.5B) and list output (255 chars). **ZAI_PARAMS** – only supplies typed
  reference fields for the RFC interfaces.
- **Z_AI_SESSION_OPEN / Z_AI_HEARTBEAT / Z_AI_SESSION_CLOSE /
  Z_AI_LIST_DELIVER** – called by SAP *into* Curie through destination
  `SAP_AI`. The SAP system only needs their interfaces; Curie's Python
  handlers implement them.
- **Z_AI_PROG_CHECK_AND_EXECUTE** – called by Curie with the RFC user.
  Mode `C` syntax-checks a `ZAI_*` program; `X` inserts it and schedules
  job `ZAI_RUNNER` under the end user; `S` inserts, runs synchronously,
  returns the list and deletes; `D` deletes. Refuses program names that
  do not start with `ZAI_`; checks `S_DEVELOP` before creating/deleting.
- **ZAI_RUNNER** – job step. `SUBMIT (p_prog) EXPORTING LIST TO MEMORY`,
  `LIST_FROM_MEMORY`, `LIST_TO_ASCI`, then `Z_AI_LIST_DELIVER` to Curie.
  A short dump in the generated program cancels the job (visible in SM37,
  detected by Curie via `TBTCO`).
- **ZAI_START** – `RFC_PING` → `Z_AI_SESSION_OPEN` (with the prompt from
  the selection screen) → `GUI_RUN` starts `chrome.exe` in
  `--app` mode with the session URL → heartbeat loop with
  `SAPGUI_PROGRESS_INDICATOR` and `WAIT UP TO n SECONDS` until Curie
  answers `CLOSED`. The Chrome path is a selection-screen parameter
  (`p_chrome`, default `C:\Program Files\Google\Chrome\Application\chrome.exe`).

## Installation order

0. **SE80 (SE21 on 4.5B)** – package `ZAIC` (development class on 4.5B),
   transportable, in your customer namespace's transport layer. Every
   Curie object below goes into it, so one transport request carries the
   whole installation to the next system and nothing lives in `$TMP`.
1. **SE11** – structures `ZAI_LINE`, `ZAI_LISTLINE`, `ZAI_PARAMS`
   (`ZAI_DDIC.md`). Activate.
2. **SE37** – function group `ZAIC` (a function group id needs four
   characters; `ZAI` is refused); the four Curie-served modules with
   empty bodies; `Z_AI_PROG_CHECK_AND_EXECUTE` with the whole content of
   the `.abap` file pasted (or uploaded) as its source code, the FORM
   `LIST_TO_TABLE` after `ENDFUNCTION` included, then save and activate
   (`ZAI_FUNCTION_GROUP.md`). No separate include is needed. All five
   **remote-enabled**, all parameters **pass by value**. Generate the
   group.
3. **SE38** – `ZAI_RUNNER` (type executable, no logical database) and
   `ZAI_START` (type executable). Both without authorization group unless
   you want to restrict them further. Activate / generate.
4. **SE93** – transaction `ZAI`: *Report transaction* (on 4.5B: transaction
   type "Report transaction"), program `ZAI_START`, selection screen 1000,
   do not skip the initial screen (the prompt is entered there).
5. **SM59** – destination `SAP_AI`:
   - Connection type **T** (TCP/IP).
   - Activation type **Registered server program** ("Registration" on
     4.5B).
   - Program ID **`SAP_AI`** (must match `RFC_PROGRAM_ID` in Curie's
     `.env`).
   - Gateway host / gateway service of the application server Curie
     registers at (`RFC_GWHOST` / `RFC_GWSERV` in `.env`).
   - No logon data (type T has none). Curie's server side uses the
     NW RFC SDK 7.20, which negotiates the codepage with a 4.5B backend;
     leave the ECC Unicode tab at its default unless Curie logs a
     codepage error.
   - Gateway ACL: the Curie host must be allowed to register `SAP_AI`
     (`reg_info` / `gw/reg_info`), as for the existing registered server.
6. **SU01** – RFC user for Curie's client direction (see below).
7. **Curie** – `.env` (`RFC_*`, `SAP_RELEASE`, `SAP_LANGU`, ...), start
   `src/curie/cli.py`, run `--check`.

## RFC user (Curie → SAP)

Technical user, type **CPIC** (4.5B) / **Communication** (ECC), logon
language as `RFC_LANG`, no dialog. User defaults (SU01 → Defaults):
**decimal notation `.`** (1,234,567.89) and **date format `YYYYMMDD`** so
that `RFC_READ_TABLE` output is parseable.

| Object | Fields | Purpose |
|---|---|---|
| `S_RFC` | `RFC_TYPE = FUGR`, `RFC_NAME = SRFC, SDTX, SUNI, ZAIC` (add the groups SU53 reports if `RFC_GET_STRUCTURE_DEFINITION_P` / `RFC_GET_FUNCTION_INTERFACE_P` live elsewhere on your release), `ACTVT = 16` | `RFC_PING`, `RFC_SYSTEM_INFO`, `RFC_READ_TABLE`, the `*_P` metadata reads, `Z_AI_*` |
| `S_TABU_DIS` | `DICBERCLS` = table authorization groups Curie may read, `ACTVT = 03` | `RFC_READ_TABLE` |
| `S_DEVELOP` | `DEVCLASS = $TMP` (or `*`), `OBJTYPE = PROG`, `OBJNAME = ZAI_*`, `P_GROUP = *`, `ACTVT = 01, 02, 06` (03 for display) | `INSERT REPORT` / `DELETE REPORT`; also checked explicitly by `Z_AI_PROG_CHECK_AND_EXECUTE` |
| `S_BTCH_JOB` | `JOBACTION = RELE`, `JOBGROUP = *` (or empty) | `JOB_CLOSE strtimmed = 'X'` |
| `S_BTCH_NAM` | `BTCUNAME` = the end users (or `*`) | `SUBMIT ... USER <end user> VIA JOB` |
| `S_PROGRAM` | only if `ZAI_RUNNER` gets an authorization group | `SUBMIT zai_runner` |

Possible extra on some systems: `S_TRANSPRT` if `INSERT REPORT` insists on
a TADIR/transport entry for a new program — check with SU53 after the
first mode `X` call (see `CURIE_PLAN.md` section 10.4).

## End users

- Transaction `ZAI` via `S_TCODE`. No `S_RFC` needed: the outbound call
  goes through the T destination without logon.
- The user must be a **dialog user that exists in the client** and may run
  background jobs — the job step of `ZAI_RUNNER` runs under this name.
- Chrome must be installed on the PC at the path shown in `p_chrome`
  (change the default in `ZAI_START` or in the SE93 transaction variant
  if your standard path differs). The SAP GUI security module (ECC) may
  ask once to allow `chrome.exe`.
- Plain `SELECT` in a generated program performs **no** authorization
  check; restrict `ZAI` to trusted users (plan section 10.3).

## Test sequence

1. Start Curie; check its log for "registered as SAP_AI".
2. **SM59 → SAP_AI → Connection test**. Must succeed (the registered
   program answers). If it fails: program ID, gateway host/service,
   gateway ACL, or Curie not running.
3. Optional: **SE37 → Test** `Z_AI_PROG_CHECK_AND_EXECUTE` (as the RFC
   user or any developer): `I_PROGNAME = ZAI_TEST_HELLO`, `I_MODE = 'C'`,
   `T_SOURCE` = `ZAI_TEST_HELLO.abap` lines. Expect `E_SYNTAX_OK = 'X'`.
   Repeat with `I_MODE = 'S'`; `T_LIST` shows the list. Repeat with
   `I_MODE = 'X'` and your own user in `I_USER`; SM37 shows job
   `ZAI_TEST_HELLO` and Curie logs a `Z_AI_LIST_DELIVER`
   (Curie answers `UNKNOWN_RUN` for a run it did not start — that is
   expected here and cancels the job with the message from `ZAI_RUNNER`).
   Finish with `I_MODE = 'D'`.
4. **SE38 → ZAI_START** (or transaction `ZAI`), enter a prompt, F8.
   Chrome opens the session page with the prompt pre-filled; the SAP GUI
   status bar shows Curie's status text. Close the Chrome window: the
   report ends within one heartbeat with "Curie session ended".
5. Stop Curie while a session is open: the report ends with
   "Curie connection lost".

## Known differences 4.5B ↔ ECC 6.0

| Topic | 4.5B | ECC 6.0 |
|---|---|---|
| Source line width | 72 (`ZAI_LINE-LINE` CHAR72) | 255 possible; Curie adapts to the interface |
| Starting Chrome | `GUI_RUN` | `GUI_RUN` still works; `cl_gui_frontend_services=>execute` is the modern way (comment block in `ZAI_START.abap`) |
| Development class / package | Development class `ZAIC` (SE21) | Package `ZAIC` (SE80, SE21) |
| Activation | "Generate" | "Activate" |
| FM parameter typing | reference fields (`LIKE`) via `ZAI_PARAMS` | same works; `TYPE` with DDIC types also fine |

## Findings on the first system (TC0, R/3 4.5B, 2026-09-05)

What `curie --check` and `python -m curie.tools.phase0` showed after the
objects were installed, and what still has to change in SE37:

1. **`Z_AI_HEARTBEAT`: `E_STATE` and `E_STATUS_TEXT` were created as IMPORT
   parameters.** They must be EXPORT, otherwise Curie cannot answer and the
   `ZAI_START` loop ends at the first heartbeat with `SYSTEM_FAILURE`.
2. **Function group `ZAIC` has a syntax error**, so every call of
   `Z_AI_PROG_CHECK_AND_EXECUTE` dumps with `SYNTAX_ERROR`. Cause: the
   function calls `PERFORM list_to_table`, but the FORM was not installed
   because only the part between `FUNCTION` and `ENDFUNCTION` had been
   pasted. Paste the whole `.abap` file as the module's source, the FORM
   after `ENDFUNCTION` included, save and activate. A separate include
   (`LZAICF01`) is not needed and the manual edit of the main program
   does not work.
3. **`ZAI_LINE` / `ZAI_LISTLINE` are flat lines** (the RFC layer reports a
   single field named after the type, not `LINE`). Curie handles that on its
   side. In the FORM, replace `pt_list-line = lt_asci.` by whatever the
   component is really called (`pt_list-zai_listline = lt_asci.`), or
   `pt_list = lt_asci.` if the type is elementary.
4. Interfaces as the 4.5B backend has them (Curie adapts automatically):
   - `RFC_GET_STRUCTURE_DEFINITION_P`: `TABNAME`, `TABLENGTH`, `FIELDS` — no
     language parameter; texts come from `DD04T` in `SAP_LANGU` instead.
   - `RFC_GET_FUNCTION_INTERFACE_P`: `FUNCNAME`, `LANGUAGE` (1 char), and the
     table is called `PARAMS_P`.
   - `RFC_READ_TABLE`: the usual `QUERY_TABLE`, `DELIMITER`, `NO_DATA`,
     `ROWSKIPS`, `ROWCOUNT`, `OPTIONS`(72), `FIELDS`, `DATA`(512).
   - `RPY_PROGRAM_READ` and `RPY_FUNCTIONMODULE_READ` are remote-enabled and
     let Curie read sources (used for diagnostics only).
5. Items 1 and 2 were fixed the same evening (the FORM lives in the
   module's own source after ENDFUNCTION, which is the way to install it).
   Verified working
   afterwards: `SYNTAX-CHECK FOR t_source ... PROGRAM` (errors come back
   with line and word), `INSERT REPORT`, the synchronous run, and the full
   job path: `JOB_OPEN` / `SUBMIT zai_runner ... USER ... VIA JOB` /
   `JOB_CLOSE`, `ZAI_RUNNER` with `LIST TO MEMORY` + `LIST_TO_ASCI`, the
   delivery through `Z_AI_LIST_DELIVER` about 25 s after confirmation, and
   the deletion of the generated program. Not yet verified: `ZAI_START` in
   the GUI (Chrome start via `GUI_RUN`, heartbeat loop).
6. `ZAI_RUNNER` was installed without `LOWER CASE` on `P_SESS`, so the
   session id came back upper-cased. Curie now issues upper-case hex ids,
   so this no longer matters; adding `LOWER CASE` is still tidy.
7. `BP_JOBLOG_READ` is not remote-enabled on 4.5B; job status comes from
   `TBTCO` (Curie reads it after `ABAP_RUN_TIMEOUT`).
8. **`WS_EXECUTE` cuts the command line at 128 characters** (2026-09-06),
   too short for the Chrome flags. `ZAI_START` now uses `GUI_RUN`
   (`COMMAND` = quoted chrome.exe path, `PARAMETER` = the flags, 255
   characters), which the frontend runs with `%TEMP%` expanded, so the
   `WS_QUERY` step was dropped.
9. **"Z_AI_SESSION_OPEN not found" although the SM59 test passes**
   (2026-09-06): a second RFC server was registered under the same Program
   ID `SAP_AI` (the older `Projects\SAP_AI
fc_server.py` under its
   `curie.tools.rfc_dev`). The gateway alternates between registrations, and the old
   server only serves `RFC_PING`/`STFC_*`. One Program ID, one process:
   before starting Curie, make sure nothing else is registered as `SAP_AI`
   (SMGW → Logged-on clients, or the process list on the Curie host). After
   stopping it, six `Z_AI_SESSION_OPEN`/`Z_AI_HEARTBEAT`/`Z_AI_SESSION_CLOSE`
   round trips from inside SAP (run through `RFC_ABAP_INSTALL_AND_RUN`) all
   succeeded.
