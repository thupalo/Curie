# Curie documentation

Curie puts an LLM chat inside SAP. A user starts transaction `ZAI` in the
SAP GUI, a Chrome window opens, and the model can read tables and the data
dictionary over RFC, write ABAP reports, and run them in SAP after a syntax
check, a security review and the user's confirmation. It works with
SAP R/3 4.5B (non-Unicode) and ECC 6.

State of this documentation: **9 September 2026**, version 1 feature-complete
and verified on a live R/3 4.5B system (TC0). See
[13 Status and roadmap](13-status-and-roadmap.md) for what is verified and
what is open.

| # | Document | Read it when you |
|---|---|---|
| 01 | [Overview](01-overview.md) | want the picture in five minutes |
| 02 | [Architecture](02-architecture.md) | change code, or need to know what runs where |
| 03 | [Installation](03-installation.md) | set up a Curie host and the SAP side |
| 04 | [Configuration](04-configuration.md) | look up a `.env` setting |
| 05 | [SAP objects](05-sap-objects.md) | install or review the ABAP side |
| 06 | [Session lifecycle](06-session-lifecycle.md) | need to know how a session starts, lives and ends |
| 07 | [LLM and tools](07-llm-and-tools.md) | tune prompts, models or the tools |
| 08 | [ABAP execution](08-abap-execution.md) | need the code path from model answer to SAP list |
| 09 | [Web UI and API](09-web-ui-and-api.md) | work on the page or script against Curie |
| 10 | [Security](10-security.md) | assess or harden the deployment |
| 11 | [Operations](11-operations.md) | run Curie as a service, read logs, troubleshoot |
| 12 | [Development](12-development.md) | extend Curie, run tests, use the dev tools |
| 13 | [Status and roadmap](13-status-and-roadmap.md) | want to know what is done and what is next |
| 14 | [Knowledge](14-knowledge.md) | teach Curie about your tables, terms and programs |
| 15 | [Chat plugin](15-chat-plugin.md) | let ABAP programs call the model directly (optional) |

Related files outside this folder:

- [`../README.md`](../README.md): the short project README.
- [`../abap/README.md`](../abap/README.md): installation notes and findings for
  the SAP objects; the ABAP sources are next to it.
- [`../../CURIE_PLAN.md`](../../CURIE_PLAN.md): the original implementation
  plan (5 September 2026). Where the plan and these documents differ, these
  documents describe what was built.
