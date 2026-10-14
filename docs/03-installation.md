# 03 Installation

## Curie host

Requirements: Windows, Python 3.12, one NW RFC SDK unpacked to
`C:\nwrfcsdk` (or set `SAPNWRFC720_HOME`): 7.50 for a Unicode backend
(ECC 6 and newer), 7.20 (7.21) for a non-Unicode backend such as R/3
4.5B, which rejects a 7.50 server with `CALL_FUNCTION_FORMAT_NOT_SUP`;
network access to the SAP gateway and to the LLM endpoint. The SDK comes
from the SAP Support Portal (SAP Note 1025361) and is not in the
repository.

```powershell
cd sap_ai_chat
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
copy .env.example ..\.env        # or .env in this directory
notepad ..\.env                  # SAP logon, RFC_PROGID, LLM_*, WEB_PUBLIC_URL
```

`config.py` looks for `.env` in `sap_ai_chat/` first and then one level up.
The parent location keeps credentials outside the git repository.

Minimum settings: `RFC_USER`, `RFC_PASS`, `RFC_ASHOST`, `RFC_SYSNR`,
`RFC_CLIENT`, `RFC_LANG`, `RFC_GWHOST`, `RFC_GWSERV`, `RFC_PROGID=SAP_AI`,
`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, and `WEB_PUBLIC_URL` with the
host name the end-user PCs can resolve (for example
`http://MIAPTHUPALO2:8765`). See [04 Configuration](04-configuration.md).

## SAP side

Install the objects in [`../abap/`](../abap/README.md) in this order:
a package `ZAIC` (development class on 4.5B) that holds every Curie
object, DDIC structures (`ZAI_LINE`, `ZAI_LISTLINE`, `ZAI_PARAMS`),
function group `ZAIC` with the five function modules (a function group id
needs four characters), reports `ZAI_RUNNER` and `ZAI_START`, transaction
`ZAI`, SM59 destination `SAP_AI` (type T, registered server program,
Program ID `SAP_AI`), and the RFC user with the authorizations listed in
[05 SAP objects](05-sap-objects.md). With everything in one package, one
transport request moves the installation to the next system.

Verified installation pitfalls on 4.5B, all documented in
[`../abap/README.md`](../abap/README.md) under "Findings": `E_STATE` and
`E_STATUS_TEXT` of `Z_AI_HEARTBEAT` must be EXPORT parameters; the source
of `Z_AI_PROG_CHECK_AND_EXECUTE` is the whole `.abap` file, the FORM
`list_to_table` after `ENDFUNCTION` included (pasting only the body
leaves the FORM out and the group does not compile); the table lines are
flat `CHAR` fields; `WS_EXECUTE` cuts
the command line at 128 characters, so `ZAI_START` uses `GUI_RUN`.

## First start

```powershell
.\venv\Scripts\curie --check
```

`--check` reports: the SDK version, configuration problems, the backend
system and release with the chosen profile, `SAP_LANGU`, the interface of
every function module Curie calls (`RFC_READ_TABLE`,
`RFC_GET_STRUCTURE_DEFINITION_P`, `RFC_GET_FUNCTION_INTERFACE_P`,
`Z_AI_PROG_CHECK_AND_EXECUTE`) and serves (`Z_AI_*`, `RFC_PING`, `STFC_*`)
exactly as the backend has them, the `T_SOURCE` line width, and a one-token
LLM round trip.

```powershell
.\venv\Scripts\curie            # serve
.\venv\Scripts\curie --selftest # in a second window, while serving
```

`--selftest` runs a small report inside SAP (through
`RFC_ABAP_INSTALL_AND_RUN`) that calls `Z_AI_SESSION_OPEN`,
`Z_AI_HEARTBEAT` and `Z_AI_SESSION_CLOSE` via the SM59 destination six
times. It distinguishes "nothing registered" (Curie not running) from
"function not found" (another server holds the Program ID).

Then run `ZAI` in the SAP GUI. Chrome must open on the session URL with the
prompt pre-filled and a green SAP dot in the header.

## Run as a service

`service\install-service.ps1` installs Curie under NSSM; see
[11 Operations](11-operations.md).

## Development mode

`CURIE_DEV=1` enables `POST /dev/open`, which creates a session without the
SAP GUI; `curie.tools.devchat` uses it. Never enable it on a production host.
