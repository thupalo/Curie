# Curie knowledge

What Curie knows about this SAP system beyond the data dictionary. Plain
Markdown with YAML frontmatter; open the folder as an Obsidian vault to edit
and to see the relationships (wikilinks). Curie reloads changed files within
a few seconds; no restart.

| Folder | Written by | What |
|---|---|---|
| `tables/` | people | one note per table: what it holds, what important fields mean |
| `concepts/` | people | a business term and how it maps to tables, fields and values |
| `hints/` | `/remember` in the chat | what a user told Curie, with the user's name and date |
| `exemplars/` | Curie | a program the user confirmed and that ran successfully, with its question |

Frontmatter:

```yaml
---
type: table            # table | concept | hint | exemplar
name: ZUMOWYL
aliases: [umowa leasingowa, umowy leasingowe, leasing agreement]
tables: [ZUMOWYL]      # SAP names this note is about (retrieval keys)
keywords: [leasing]    # business words (matched by word stem)
status: confirmed      # proposed | confirmed | retired (retired = never used)
source: user:THUPALO   # who or what it came from
created: 2026-09-06
uses: 0                # maintained by Curie
successes: 0
failures: 0
---
The body is what the model reads. Keep it short and factual. Link related
notes with [[wikilinks]]; a linked note is pulled in with a small weight.
```

Retrieval is by names: table and field names in the question or in tool
calls match `tables` and `name` exactly; `aliases` and `keywords` match by
word stems, so Polish inflection is fine. Curie puts at most
`KNOWLEDGE_MAX_NOTES` notes and `KNOWLEDGE_EXEMPLARS` programs into a
prompt, curated facts before user hints. `/knowledge` in the chat shows what
applies to the current conversation; `/forget <id>` retires a note.

Trust: notes here are read by the model as facts. Anyone who can write to
this folder can steer the assistant, so treat it like configuration. The
function-module policy and the table deny list are not affected by notes.
