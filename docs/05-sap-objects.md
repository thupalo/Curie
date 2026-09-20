# 05 SAP objects

Sources and step-by-step notes are in [`../abap/`](../abap/README.md). This
document describes the objects as they exist on the verified system (TC0,
R/3 4.5B) and what each one is for.

## Basis

- **SM59 destination `SAP_AI`**: connection type T, "Registered Server
  Program", Program ID `SAP_AI`, gateway of the application server. The
  connection test calls `RFC_PING`, which Curie serves.
- **Gateway**: `reg_info` must allow the Curie host to register.
- **RFC user** (communication type) for Curie's client direction:
  `S_RFC` for the function groups used (`SDTX` for `RFC_READ_TABLE`, the
  groups of the `RFC_GET_*_P` reads, `ZAIC`, and `SUTL`/`SDTX` for
  `RFC_ABAP_INSTALL_AND_RUN` if `--selftest` is used), `S_TABU_DIS` for the
  table groups Curie may read, `S_DEVELOP` (PROG, 01/02/06) for
  `INSERT REPORT`/`DELETE REPORT`, `S_BTCH_JOB` and `S_BTCH_NAM` to schedule
  jobs under the end user, and `S_XMI_PROD` (company and product `CURIE`,
  interface `XBP`) for aborting jobs. User defaults: decimal notation `.`
  and date format `YYYYMMDD`, so `RFC_READ_TABLE` output parses.
- **End users**: transaction `ZAI` guarded by `S_TCODE`. They need no
  `S_RFC`; the outbound call goes through the T destination.

## DDIC

| Object | Fields | Purpose |
|---|---|---|
| `ZAI_LINE` | `LINE CHAR 72` (CHAR 255 is fine on ECC) | Source line for `T_SOURCE`, long prompts in `T_PROMPT` |
| `ZAI_LISTLINE` | `LINE CHAR 255` | ABAP list line for `T_LIST` |
| `ZAI_PARAMS` | `SESSION_ID CHAR32`, `RUN_ID CHAR8`, `STATE CHAR16`, `STATUS_TEXT CHAR80`, `URL CHAR255`, `PROMPT CHAR255`, `REASON CHAR32`, `MESSAGE CHAR255`, `PROGNAME CHAR40`, `MODE CHAR1`, `SYNTAX_OK CHAR1`, `WORD CHAR72` | Reference fields for the function module interfaces |

On the verified system the table lines appear to the RFC layer as a single
flat field named after the type (`ZAI_LINE`, `ZAI_LISTLINE`), not as a
structure with a `LINE` component. Curie maps single-field lines by
position, so either shape works.

## Package and function group `ZAIC`

A function group id needs four characters, so the group is `ZAIC`, not
`ZAI` as the plan first wrote. Every Curie object, the DDIC structures,
the group, the two reports and the transaction, belongs to one package
(development class on 4.5B) named `ZAIC` as well, so one transport
request carries the installation to the next system and nothing is left
in `$TMP`.

### Served by Curie (remote-enabled, empty body)

| FM | IMPORT | EXPORT / TABLES | Exceptions |
|---|---|---|---|
| `Z_AI_SESSION_OPEN` | `I_PROMPT` CHAR255; TABLES `T_PROMPT` (ZAI_LINE) for long prompts | `E_SESSION_ID` CHAR32, `E_URL` CHAR255, `E_HEARTBEAT_SEC` INT | `SERVER_BUSY`, `NOT_ALLOWED` |
| `Z_AI_HEARTBEAT` | `I_SESSION_ID` CHAR32 | `E_STATE` CHAR16, `E_STATUS_TEXT` CHAR80 (both **EXPORT**) | `UNKNOWN_SESSION` |
| `Z_AI_SESSION_CLOSE` | `I_SESSION_ID`, `I_REASON` CHAR32 | | |
| `Z_AI_LIST_DELIVER` | `I_SESSION_ID`, `I_RUN_ID` CHAR8, `I_RC` INT, `I_MESSAGE` CHAR255 | TABLES `T_LIST` (ZAI_LISTLINE) | `UNKNOWN_RUN` |

Curie reads the caller's system, client, user and release from the RFC
connection attributes, so `Z_AI_SESSION_OPEN` needs no identity parameters.

### Implemented in ABAP, called by Curie

`Z_AI_PROG_CHECK_AND_EXECUTE`

```
IMPORTING  I_SESSION_ID CHAR32  I_RUN_ID CHAR8  I_PROGNAME LIKE SY-REPID
           I_USER LIKE SY-UNAME  I_MODE CHAR1
EXPORTING  E_SYNTAX_OK CHAR1  E_MESSAGE CHAR255  E_LINE INT  E_WORD CHAR72
           E_JOBNAME LIKE TBTCO-JOBNAME  E_JOBCOUNT LIKE TBTCO-JOBCOUNT
TABLES     T_SOURCE (ZAI_LINE)   T_LIST (ZAI_LISTLINE, mode S only)
EXCEPTIONS INSERT_FAILED  JOB_FAILED  NOT_AUTHORIZED  PROGNAME_INVALID
```

| Mode | Does |
|---|---|
| `C` | `SYNTAX-CHECK FOR t_source ... PROGRAM i_progname`; returns message, line and word on error |
| `X` | Mode C, then `INSERT REPORT`, `JOB_OPEN`, `SUBMIT zai_runner WITH p_prog p_sess p_run USER i_user VIA JOB ... NUMBER ... AND RETURN`, `JOB_CLOSE strtimmed = 'X'`; returns job name and count |
| `S` | Mode C, `INSERT REPORT`, `SUBMIT (i_progname) EXPORTING LIST TO MEMORY AND RETURN`, list into `T_LIST` via the FORM `list_to_table`, `DELETE REPORT` |
| `D` | `DELETE REPORT` |

The program name must start with `ZAI_`. The FORM `list_to_table`
(`LIST_FROM_MEMORY`, `LIST_TO_ASCI`, `FREE MEMORY`) is part of the
module's source, after `ENDFUNCTION`: the whole `.abap` file is pasted as
the source of the module.

## Reports

**`ZAI_RUNNER`** runs inside the job as the end user: `SUBMIT (p_prog)
EXPORTING LIST TO MEMORY AND RETURN`, `LIST_FROM_MEMORY`, `LIST_TO_ASCI`,
then `CALL FUNCTION 'Z_AI_LIST_DELIVER' DESTINATION 'SAP_AI'` with the lines,
the return code and a message. A short dump in the submitted program cancels
the job before delivery; Curie then reads `TBTCO`.

**`ZAI_START`** (transaction `ZAI`): `RFC_PING` through the destination,
`Z_AI_SESSION_OPEN` with the prompt, then `GUI_RUN` with the quoted Chrome
path as `COMMAND` and the flags as `PARAMETER` (`--app="<url>"
--window-size=1024,768 --user-data-dir="%TEMP%\chrome-kiosk-profile"
--no-first-run --no-default-browser-check --disable-features=Translate`,
about 205 characters), then the heartbeat loop: `Z_AI_HEARTBEAT`,
`SAPGUI_PROGRESS_INDICATOR` with the status text, `WAIT UP TO n SECONDS`,
until the state is `CLOSED` or the call fails. `WAIT UP TO` rolls the work
process out, so `rdisp/max_wproc_runtime` is not an issue.

`WS_EXECUTE` is not used: it truncates the command line at 128 characters.

**`ZAI_TEST_HELLO`** is a sample program for manual tests of the execution
path.

## Chat plugin objects (optional)

With `CHAT_PLUGIN=1` Curie also serves `Z_BLA_RFC_CHAT_OPEN / READ / CLOSE`
(group `ZBLA`, structures `ZBLA_RFC_MSG`, `ZBLA_RFC_CHUNK`, `ZBLA_RFC_CTL`)
and `Z_AI_CHAT_COMPLETION` (group `ZAICHAT`, table `ZCHATMSG`, `TLINE`).
Definitions, the facade change in group `ZBLA` and the wrapper with
fallback are in [`abap/ZBLA_CHAT.md`](../abap/ZBLA_CHAT.md); behaviour in 15.

## Standard function modules Curie calls

| FM | Used for | 4.5B interface as found |
|---|---|---|
| `RFC_SYSTEM_INFO` | Release detection | |
| `RFC_READ_TABLE` | Tools, `DD03L`, `DD04T`, `TBTCO`, `TRDIR` | `QUERY_TABLE`, `DELIMITER`, `NO_DATA`, `ROWSKIPS`, `ROWCOUNT`, `OPTIONS`(72), `FIELDS`, `DATA`(512) |
| `RFC_GET_STRUCTURE_DEFINITION_P` | Structure fields | `TABNAME`, `TABLENGTH`, `FIELDS`; no language parameter |
| `RFC_GET_FUNCTION_INTERFACE_P` | Function interfaces | `FUNCNAME`, `LANGUAGE` (1 char), table `PARAMS_P` |
| `BP_JOB_ABORT` | Stop a job (ECC) | Not remote-enabled on 4.5B |
| `BAPI_XMI_LOGON` / `BAPI_XBP_JOB_ABORT` / `BAPI_XMI_LOGOFF` | Stop a job (4.5B, XBP 1.0) | Remote-enabled |
| `RFC_ABAP_INSTALL_AND_RUN` | `--selftest` only | `MODE`, `PROGRAMNAME`, `PROGRAM`, `WRITES`, `ERRORMESSAGE` |
| `RPY_PROGRAM_READ`, `RPY_FUNCTIONMODULE_READ` | Diagnostics (reading sources) | Remote-enabled |

Curie reads every interface at startup and passes only parameters the
backend has (`SapClient.call_known`), which is how the same code runs on
4.5B and ECC.
