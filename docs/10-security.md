# 10 Security

## What Curie can do in SAP

Curie acts with two identities:

- **The RFC user** (`RFC_USER`) for tools, the syntax check, `INSERT
  REPORT`, job scheduling, cleanup and job abort. Everything the model reads
  through the tools is read with this user's `S_TABU_DIS`.
- **The end user** for generated programs in job mode: the job runs under
  the SAP user who started `ZAI`. Note that a plain `SELECT` in ABAP performs
  no authorization check, so a generated report can read any table
  regardless of that user's table authorizations. This is the main reason
  the code path has four gates and why transaction `ZAI` should be limited
  to trusted users.

## Controls, in order of appearance

1. **Session identity** comes from the RFC connection attributes of a call
   SAP made; the browser never supplies it.
2. **URL token**: 128 random bits, delivered SAP → GUI → Chrome on the
   user's PC only; **IP binding** ties the session to the first browser
   address; limits per user and in total.
3. **Table deny list** for the tools and for `FROM`/`JOIN` in generated
   code.
4. **Static rules** on generated code: no database changes, no system or
   file access, no dialogs, no RFC or transactions, bounded reads, the
   function-module policy.
5. **Syntax check** in SAP before anything is created.
6. **Model audit** with an independent, strict prompt; an unavailable
   auditor counts as a negative verdict.
7. **User confirmation** in the browser.
8. **Background job** isolates Curie from dumps and long runs; programs are
   deleted afterwards and swept at startup.
9. **Audit trail**: every prompt, tool call, program, verdict, confirmation
   and result in a JSONL file per session.

## Known limits

- Static rules match literal function names; a name built at runtime slips
  past rule 4 and relies on the audit.
- The audit is a model; treat it as a strong heuristic, not a proof.
- The page and the API are plain HTTP on the LAN. Put a reverse proxy with
  TLS in front for anything beyond a trusted network, and then set
  `SESSION_BIND_IP=0` unless the proxy preserves client addresses.
- `RFC_ABAP_INSTALL_AND_RUN` is used only by `--selftest`; it needs
  `S_DEVELOP` for the RFC user and is worth restricting in production
  systems anyway.
- `CURIE_DEV=1` lets anyone who reaches the port open a session as the RFC
  user. Never on a production host.
- The `.env` holds the SAP password and the LLM key in clear text; protect
  the file with NTFS permissions and keep it out of the repository (it is
  ignored by `.gitignore`).
- Session logs may contain business data; `logs/` is ignored by git and
  should be retained and protected according to your data policy.

## Recommended settings for production

```
CURIE_DEV=0
SESSION_BIND_IP=1
ABAP_REQUIRE_CONFIRM=1
ABAP_AUDIT=1
ABAP_AUDIT_BLOCKS=1
ABAP_FUNCTION_POLICY=allow      # and a short ABAP_FUNCTION_ALLOW list
READ_TABLE_DENY=USR02,USH02,USR40,USRBF*,PA0*,PCL*,HRP*,REGUH,PAYR
ABAP_EXEC_MODE=job
```

Plus a dedicated RFC user with exactly the authorizations in
[05 SAP objects](05-sap-objects.md), and `ZAI` restricted through
`S_TCODE`.
