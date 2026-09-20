# Curie – function group ZAIC

Create the function group first (4.5B: SE37 → Goto → Function groups →
Create group, or SE80; ECC: SE80 → Function group). Name `ZAIC` (a
function group id needs four characters; `ZAI` is refused), short text
"Curie – LLM chat interface". Package `ZAIC` (development class on 4.5B),
the one that holds every Curie object; `$TMP` only for a throw-away test.
The main program is `SAPLZAIC`, the includes `LZAICTOP`, `LZAICUXX` and
the per-module includes `LZAICU01` … `LZAICU05`.

Five function modules live in the group:

| FM | Implemented by | Direction |
|---|---|---|
| `Z_AI_SESSION_OPEN` | Curie (Python handler) | SAP → Curie |
| `Z_AI_HEARTBEAT` | Curie | SAP → Curie |
| `Z_AI_SESSION_CLOSE` | Curie | SAP → Curie |
| `Z_AI_LIST_DELIVER` | Curie | SAP → Curie |
| `Z_AI_PROG_CHECK_AND_EXECUTE` | ABAP (`Z_AI_PROG_CHECK_AND_EXECUTE.abap`) | Curie → SAP |

The four modules served by Curie must exist in the SAP system because the
caller (`CALL FUNCTION ... DESTINATION 'SAP_AI'`) needs the interface
definition from the local repository to marshal the parameters. Their
ABAP source body stays **empty** (only `FUNCTION`/`ENDFUNCTION`); the
registered server program `SAP_AI` receives the call.

## Attributes common to all five modules

- Processing type: **Remote-enabled module** (RFC).
- Every parameter with **Pass value** (`VALUE(...)`) — mandatory for
  RFC-enabled modules; SE37 enforces it.
- On 4.5B, scalar parameters are typed with a **reference field**
  (`LIKE`), which is why `ZAI_PARAMS` exists (`ZAI_DDIC.md`). Enter the
  reference as `ZAI_PARAMS-SESSION_ID` in the "Reference field" column
  with type spec `LIKE`. Integer parameters: `TYPE I` (fallback
  `LIKE ZAI_PARAMS-HEARTBEAT_SEC` etc.).
- TABLES parameters are typed with the **reference structure**
  (`LIKE ZAI_LINE`, shown as `STRUCTURE ZAI_LINE` in the source header).

## Z_AI_SESSION_OPEN

Short text: "Curie: open chat session, returns URL".

| Parameter | Class | Type | Optional | Default |
|---|---|---|---|---|
| I_PROMPT | IMPORT | LIKE ZAI_PARAMS-PROMPT (CHAR255) | X | |
| T_PROMPT | TABLES | LIKE ZAI_LINE | X | |
| E_SESSION_ID | EXPORT | LIKE ZAI_PARAMS-SESSION_ID (CHAR32) | | |
| E_URL | EXPORT | LIKE ZAI_PARAMS-URL (CHAR255) | | |
| E_HEARTBEAT_SEC | EXPORT | TYPE I | | |

Exceptions: `SERVER_BUSY`, `NOT_ALLOWED`.

## Z_AI_HEARTBEAT

Short text: "Curie: session heartbeat / status".

| Parameter | Class | Type | Optional | Default |
|---|---|---|---|---|
| I_SESSION_ID | IMPORT | LIKE ZAI_PARAMS-SESSION_ID (CHAR32) | | |
| E_STATE | EXPORT | LIKE ZAI_PARAMS-STATE (CHAR16) | | |
| E_STATUS_TEXT | EXPORT | LIKE ZAI_PARAMS-STATUS_TEXT (CHAR80) | | |

Exceptions: `UNKNOWN_SESSION`.

`E_STATE` values: `WAITING`, `THINKING`, `TOOL`, `CONFIRM`, `RUNNING`,
`CLOSED`. `ZAI_START` leaves its loop on `CLOSED`.

## Z_AI_SESSION_CLOSE

Short text: "Curie: close chat session".

| Parameter | Class | Type | Optional | Default |
|---|---|---|---|---|
| I_SESSION_ID | IMPORT | LIKE ZAI_PARAMS-SESSION_ID (CHAR32) | | |
| I_REASON | IMPORT | LIKE ZAI_PARAMS-REASON (CHAR32) | X | |

Exceptions: none.

## Z_AI_LIST_DELIVER

Short text: "Curie: deliver ABAP list of a run".

| Parameter | Class | Type | Optional | Default |
|---|---|---|---|---|
| I_SESSION_ID | IMPORT | LIKE ZAI_PARAMS-SESSION_ID (CHAR32) | | |
| I_RUN_ID | IMPORT | LIKE ZAI_PARAMS-RUN_ID (CHAR8) | | |
| I_RC | IMPORT | TYPE I | | |
| I_MESSAGE | IMPORT | LIKE ZAI_PARAMS-MESSAGE (CHAR255) | X | |
| T_LIST | TABLES | LIKE ZAI_LISTLINE | | |

Exceptions: `UNKNOWN_RUN`.

## Z_AI_PROG_CHECK_AND_EXECUTE

Short text: "Curie: syntax check / run / delete ZAI_* program".
Full source: `Z_AI_PROG_CHECK_AND_EXECUTE.abap`. It is the complete
source of the module, FORM `LIST_TO_TABLE` after `ENDFUNCTION` included:
paste or upload it as the module's source code, save, activate.

| Parameter | Class | Type | Optional | Default |
|---|---|---|---|---|
| I_SESSION_ID | IMPORT | LIKE ZAI_PARAMS-SESSION_ID (CHAR32) | | |
| I_RUN_ID | IMPORT | LIKE ZAI_PARAMS-RUN_ID (CHAR8) | | |
| I_PROGNAME | IMPORT | LIKE SY-REPID | | |
| I_USER | IMPORT | LIKE SY-UNAME | X | |
| I_MODE | IMPORT | LIKE ZAI_PARAMS-MODE (CHAR1) | X | 'C' |
| E_SYNTAX_OK | EXPORT | LIKE ZAI_PARAMS-SYNTAX_OK (CHAR1) | | |
| E_MESSAGE | EXPORT | LIKE ZAI_PARAMS-MESSAGE (CHAR255) | | |
| E_LINE | EXPORT | TYPE I | | |
| E_WORD | EXPORT | LIKE ZAI_PARAMS-WORD (CHAR72) | | |
| E_JOBNAME | EXPORT | LIKE TBTCO-JOBNAME | | |
| E_JOBCOUNT | EXPORT | LIKE TBTCO-JOBCOUNT | | |
| T_SOURCE | TABLES | LIKE ZAI_LINE | | |
| T_LIST | TABLES | LIKE ZAI_LISTLINE | X | |

Exceptions: `INSERT_FAILED`, `JOB_FAILED`, `NOT_AUTHORIZED`,
`PROGNAME_INVALID`.

Modes: `C` check only, `X` check + INSERT REPORT + job `ZAI_RUNNER`
under `I_USER` (SY-UNAME when initial), `S` check + INSERT REPORT +
synchronous run with the list in `T_LIST` + DELETE REPORT, `D` delete.

### Installing the source

Open the module in SE37, replace the generated source with the whole
content of `Z_AI_PROG_CHECK_AND_EXECUTE.abap` (Utilities → Upload, or
paste), save and activate. The file carries the `FUNCTION` statement,
the body, `ENDFUNCTION` and the FORM `LIST_TO_TABLE` after it; the FORM
belongs there and is found by `PERFORM`. Do not create a separate
include and do not edit the main program `SAPLZAIC` for it: that route
does not work.

## Order of creation in SE37

1. Function group `ZAIC` in package `ZAIC`.
2. `Z_AI_SESSION_OPEN`, `Z_AI_HEARTBEAT`, `Z_AI_SESSION_CLOSE`,
   `Z_AI_LIST_DELIVER`: attributes, interface, empty body, save.
3. `Z_AI_PROG_CHECK_AND_EXECUTE`: attributes, interface, then replace the
   source with the whole `.abap` file (FORM after `ENDFUNCTION`
   included), save, activate.
4. Generate the group. Check with SE37 → Test (F8) →
   `Z_AI_PROG_CHECK_AND_EXECUTE`, `I_MODE = 'C'`, `T_SOURCE` = the lines of
   `ZAI_TEST_HELLO.abap` with the first line changed to
   `REPORT zai_test_hello.` and `I_PROGNAME = ZAI_TEST_HELLO`:
   `E_SYNTAX_OK = 'X'` expected. Then `I_MODE = 'S'`: `T_LIST` must
   contain "Hello from Curie" and up to five T000 lines.

Curie's `--check` mode reads all five interfaces with `RfcGetFunctionDesc`
and reports parameter names and lengths; compare them with the tables
above after installation.
