# 01 Overview

## What Curie does

1. An SAP user runs transaction `ZAI` (report `ZAI_START`), optionally with
   a first question.
2. SAP calls Curie over RFC; Curie creates a session and answers with a URL.
3. The SAP GUI starts Chrome in app mode on the user's PC with that URL.
4. The user chats. The model can read SAP tables, look up the dictionary
   and function interfaces, and it can write an ABAP report.
5. Every report goes through static rules, a syntax check in SAP and an
   independent model review. The user presses **Run in SAP**.
6. The report runs as a background job under the user's own SAP user; its
   list comes back to Curie and to the browser, and the model explains it.
7. Meanwhile the SAP GUI session sits in a heartbeat loop; closing the
   Chrome window ends it.

## Components

```
 SAP GUI (end-user PC)                         Curie host (Windows, Python 3.12)
 ┌──────────────────────┐                      ┌──────────────────────────────────────────┐
 │ ZAI_START            │  RFC, registered     │ RFC server threads (NW RFC SDK 7.20)      │
 │  RFC_PING            │  server SAP_AI       │   Z_AI_SESSION_OPEN  Z_AI_HEARTBEAT       │
 │  Z_AI_SESSION_OPEN   ├─────────────────────►│   Z_AI_SESSION_CLOSE Z_AI_LIST_DELIVER    │
 │  GUI_RUN chrome.exe  │                      │              │ thread-safe hand-over      │
 │  heartbeat loop      │                      │              ▼                            │
 └──────────┬───────────┘                      │ asyncio loop: FastAPI + SSE, sessions,    │
            ▼                                  │ orchestrator, code path                   │
 ┌──────────────────────┐  HTTP + SSE          │              │ executor threads           │
 │ Chrome --app window  │◄────────────────────►│              ▼                            │
 └──────────────────────┘                      │ RFC client pool (same SDK) ───────────────┼──► OpenAI-compatible LLM
                                               └──────────────────┬───────────────────────┘
                                                                  ▼ RFC client calls
                                     SAP: RFC_READ_TABLE, RFC_GET_STRUCTURE_DEFINITION_P,
                                     RFC_GET_FUNCTION_INTERFACE_P, Z_AI_PROG_CHECK_AND_EXECUTE,
                                     background job ZAI_RUNNER ──RFC──► Z_AI_LIST_DELIVER
```

| Component | Where | Role |
|---|---|---|
| SAP system | R/3 4.5B or ECC 6 | Data, dictionary, program execution; package and function group `ZAIC`, reports `ZAI_START`, `ZAI_RUNNER`, transaction `ZAI`, SM59 destination `SAP_AI` |
| Curie | one Python process | Registered RFC server, RFC client, web server, session state, model orchestration, code path |
| Browser | Chrome `--app` on the user's PC | The chat page, served by Curie |
| LLM | any OpenAI-compatible endpoint | Answers, tool calls, ABAP, and the security review |

## Key numbers

| | |
|---|---|
| Python | 3.12 on Windows |
| SAP SDK | NW RFC SDK 7.20 (7.21 patch 39 in use), the only SDK; no pyrfc |
| Backends verified | R/3 4.5B (system TC0, codepage 1401, Polish texts) |
| Source width | 72 columns on 4.5B, 255 on ECC |
| Heartbeat | every 3 s from SAP; session closed after 15 s of silence |
| Tests | 42, all with fakes, no SAP needed |

## Reading the rest

Architecture and internals: 02, 06, 07, 08, 09. Setting up and running:
03, 04, 05, 11. Assessing: 10. Extending: 12. Where things stand: 13.
