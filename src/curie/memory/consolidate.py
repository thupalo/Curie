"""The consolidation worker (knowledge plan, sections 7 and 9): from the
notes and the archive to report families, the glossary and the system
card, then the compiled bundle. Runs as its own process (`curie
--consolidate`), never on a user's turn.

Steps, each fail-closed and skippable:

1. Families (P4, deterministic): confirmed exemplars clustered by program
   shape; family notes written to knowledge/families/ (proposed unless a
   member is in the report catalogue). With a model, one call per family
   writes its Polish and English intent and a description; the
   deterministic intents stay when the call fails.
2. Glossary (P5, deterministic): question words aligned with the tables
   of confirmed programs, from the exemplars and the archive's episodes
   that ran successfully; written to the bundle's glossary.json (entries a
   person marked `confirmed` are kept as they are).
3. System card (P8): with a model, one call writes the card from the
   table and concept notes, the glossary and the families. It is a
   proposal (.build/card.proposed.md) until applied (`--apply`, or
   KNOWLEDGE_CARD_AUTO=1); a person's knowledge/CARD.md always wins.
4. The bundle's lexical parts are rebuilt and the derived parts saved.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from curie import config
from curie.llm.client import LLMError, parse_json_object
from curie.llm.prompts import CARD_PROMPT, FAMILY_PROMPT, card_prompt, family_prompt
from curie.memory import bundle as bundle_mod
from curie.memory import families as families_mod
from curie.memory import glossary as glossary_mod
from curie.memory.store import KnowledgeStore, write_note

log = logging.getLogger(__name__)


class Consolidator:
    def __init__(self, store: KnowledgeStore, build_dir: str | Path, llm=None, archive=None,
                 model: str | None = None, sap=None):
        self.store = store
        self.build_dir = Path(build_dir)
        self.llm = llm
        self.archive = archive
        self.sap = sap
        self.model = model or config.KNOWLEDGE_CONSOLIDATE_MODEL
        self.report: dict = {}

    async def run(self, apply_card: bool = False) -> dict:
        started = time.time()
        report = {"started": started, "families": {}, "glossary": {}, "card": {}, "bundle": {},
                  "dictionary": {}}
        report["families"] = await self.families()
        report["glossary"] = self.glossary()
        report["dictionary"] = await self.dictionary()
        report["facts"] = self.facts()
        report["pitfalls"] = self.pitfalls()
        report["card"] = await self.card(apply=apply_card)
        bundle = bundle_mod.ensure(self.store, self.build_dir)
        bundle_mod.save(bundle, self.build_dir)
        report["bundle"] = bundle.summary()
        report["seconds"] = round(time.time() - started, 1)
        self.report = report
        try:
            (self.build_dir / "consolidate.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        except OSError as exc:
            log.warning("consolidate report not written: %s", exc)
        return report

    # -- P4 families ---------------------------------------------------------------- #

    async def families(self) -> dict:
        drafts = families_mod.build(self.store)
        notes = families_mod.write(self.store, drafts)
        named = 0
        for note, draft in zip(notes, drafts):
            if self.llm is None or note.source != "curie:consolidate":
                continue
            if note.status == "confirmed" and note.body and not note.body.startswith("Report family"):
                continue                  # a person wrote the description: keep it
            try:
                text = await self.llm.complete(
                    [{"role": "system", "content": FAMILY_PROMPT},
                     {"role": "user", "content": family_prompt(draft.intents, note.params, note.program)}],
                    model=self.model, temperature=0.0, json_mode=True,
                    max_tokens=max(int(config.LLM_MAX_OUTPUT_TOKENS), 1024))
            except LLMError as exc:
                log.warning("family naming failed for %s: %s", note.id, exc)
                continue
            obj = parse_json_object(text) or {}
            pl = " ".join(str(obj.get("intent_pl") or "").split())
            en = " ".join(str(obj.get("intent_en") or "").split())
            desc = " ".join(str(obj.get("description") or "").split())
            if not _intents_ok(pl, en, note.params):
                log.info("family %s: model intents rejected (placeholders missing)", note.id)
                continue
            for intent in (pl, en):
                if intent and intent not in note.aliases:
                    note.aliases.append(intent)
            if 12 <= len(desc) <= 300:
                rest = note.body.split("\n\nMembers:", 1)
                tail = ("\n\nMembers:" + rest[1]) if len(rest) == 2 else ""
                note.body = desc + tail
            write_note(note)
            named += 1
        self.store.refresh(force=True)
        out = {"drafts": len(drafts), "written": [n.id for n in notes], "named": named,
               "confirmed": [n.id for n in notes if n.status == "confirmed"]}
        log.info("families: %d draft(s), %d note(s) written, %d named by the model, %d confirmed",
                 len(drafts), len(notes), named, len(out["confirmed"]))
        return out

    # -- P5 glossary ---------------------------------------------------------------- #

    def glossary(self) -> dict:
        pairs: list[tuple[str, list[str]]] = []
        for n in self.store.notes():
            if n.type == "exemplar" and n.status == "confirmed" and n.question and n.tables:
                pairs.append((n.question, list(n.tables)))
        archived = 0
        if self.archive is not None:
            try:
                rows = self.archive.query(
                    "SELECT e.question, p.tables FROM episodes e JOIN programs p ON p.episode_id = e.id "
                    "WHERE e.outcome = 'confirmed_ran_ok' AND p.run_ok = 1 AND e.exemplar_id = ''")
                for r in rows:
                    tables = json.loads(r["tables"] or "[]")
                    if r["question"] and tables:
                        pairs.append((r["question"], tables))
                        archived += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("glossary: archive not read: %s", exc)
        mined = glossary_mod.mine(pairs)
        existing = bundle_mod.load(self.build_dir)
        merged = glossary_mod.merge(existing.glossary if existing else {}, mined)
        bundle = existing or bundle_mod.Bundle()
        bundle.glossary = merged
        bundle_mod.save(bundle, self.build_dir, parts=("glossary.json",))
        log.info("glossary: %d pair(s) (%d from the archive) -> %d term(s)", len(pairs), archived, len(merged))
        return {"pairs": len(pairs), "archived": archived, "terms": len(merged),
                "sample": dict(list(merged.items())[:8])}

    # -- P10 the dictionary index ------------------------------------------------------ #

    async def dictionary(self) -> dict:
        """Read the customer namespace and every table the notes or the
        archive mention into the bundle's dictionary.json (needs SAP)."""
        if self.sap is None:
            return {"skipped": "no SAP connection"}
        from curie.memory import dictionary as dict_mod
        from curie.sap_tools.ddic import DdicCache
        from curie.sap_tools.read_table import read_rows, rows_as_dicts

        def texts_and_names() -> tuple[dict[str, dict], set[str]]:
            texts: dict[str, dict] = {}
            names: set[str] = set()
            for prefix in ("Z", "Y"):
                skip = 0
                while True:
                    cols, rows = read_rows(self.sap, "DD02L", ["TABNAME", "TABCLASS"],
                                           f"TABNAME LIKE '{prefix}%' AND AS4LOCAL = 'A'",
                                           rowcount=2000, rowskips=skip)
                    for r in rows_as_dicts(cols, rows):
                        name = str(r.get("TABNAME", "")).strip().upper()
                        klass = str(r.get("TABCLASS", "")).strip()
                        if dict_mod.is_customer_table(name) and klass in ("TRANSP", "VIEW", "POOL", "CLUSTER"):
                            names.add(name)
                            texts.setdefault(name, {})["class"] = klass
                    if len(rows) < 2000:
                        break
                    skip += 2000
            for n in self.store.notes():
                names.update(t for t in n.tables if t and t.isidentifier())
            if self.archive is not None:
                try:
                    for r in self.archive.query("SELECT DISTINCT tables FROM tool_calls WHERE tables <> '[]'"):
                        names.update(t.upper() for t in json.loads(r["tables"] or "[]"))
                except Exception as exc:  # noqa: BLE001
                    log.debug("dictionary: archive tables not read: %s", exc)
            for langu, key in ((config.SAP_LANGU[:1] or "L", "text"), ("E", "text_en")):
                for prefix in ("Z", "Y"):
                    skip = 0
                    while True:
                        cols, rows = read_rows(self.sap, "DD02T", ["TABNAME", "DDTEXT"],
                                               f"DDLANGUAGE = '{langu}' AND TABNAME LIKE '{prefix}%'",
                                               rowcount=2000, rowskips=skip)
                        for r in rows_as_dicts(cols, rows):
                            name = str(r.get("TABNAME", "")).strip().upper()
                            if name in names:
                                texts.setdefault(name, {})[key] = str(r.get("DDTEXT", "")).strip()
                        if len(rows) < 2000:
                            break
                        skip += 2000
                standard = sorted(n for n in names if not dict_mod.is_customer_table(n))
                for i in range(0, len(standard), 30):
                    chunk = standard[i:i + 30]
                    where = f"DDLANGUAGE = '{langu}' AND ( " + " OR ".join(
                        f"TABNAME = '{n}'" for n in chunk) + " )"
                    try:
                        cols, rows = read_rows(self.sap, "DD02T", ["TABNAME", "DDTEXT"], where,
                                               rowcount=len(chunk))
                    except Exception as exc:  # noqa: BLE001
                        log.debug("dictionary: DD02T texts: %s", exc)
                        continue
                    for r in rows_as_dicts(cols, rows):
                        name = str(r.get("TABNAME", "")).strip().upper()
                        texts.setdefault(name, {})[key] = str(r.get("DDTEXT", "")).strip()
            return texts, names

        loop = __import__("asyncio").get_running_loop()
        try:
            texts, names = await loop.run_in_executor(None, texts_and_names)
            ddic = DdicCache(self.sap)
            data = await loop.run_in_executor(None, lambda: dict_mod.build(ddic, sorted(names), texts))
        except Exception as exc:  # noqa: BLE001
            log.warning("dictionary index failed: %s", exc)
            return {"error": str(exc)}
        usage, field_usage = dict_mod.usage_from(self.archive, self.store)
        data["usage"] = usage
        data["field_usage"] = field_usage
        existing = bundle_mod.load(self.build_dir) or bundle_mod.Bundle()
        existing.dictionary = data
        bundle_mod.save(existing, self.build_dir, parts=("dictionary.json",))
        try:
            from curie import caches
            caches.save_all()             # the structures just read persist for the runtime too
        except Exception:  # noqa: BLE001
            pass
        log.info("dictionary index: %d table(s), %d with texts", len(data["tables"]),
                 sum(1 for t in data["tables"].values() if t.get("text")))
        return {"tables": len(data["tables"]), "customer": sum(1 for n in data["tables"]
                                                                 if dict_mod.is_customer_table(n))}

    # -- P7 pitfalls ------------------------------------------------------------------- #

    def pitfalls(self) -> dict:
        """Triggers, merges and counter-evidence for the insights, the syntax
        clusters of the archive, and the bundle's pitfalls.json."""
        from curie.memory import pitfalls as pitfalls_mod

        report = pitfalls_mod.consolidate(self.store, self.archive)
        part = report.pop("bundle", None)
        if part:
            pitfalls_mod.write_part(part, self.build_dir)
        return report

    # -- P2/P3/P6 facts and drafted table notes ----------------------------------------- #

    def facts(self) -> dict:
        """Mine facts from the confirmed programs, verify them against the
        dictionary index, keep them in the archive, render them into the
        table notes, and draft notes for used tables that have none."""
        from curie.memory import dictionary as dict_mod
        from curie.memory import facts as facts_mod

        facts = facts_mod.mine(self.store, self.archive)
        bundle = bundle_mod.load(self.build_dir) or bundle_mod.Bundle()
        index = dict_mod.Index(bundle.dictionary)
        counts = facts_mod.verify(facts, index) if len(index) else {"unknown": len(facts)}
        promoted = facts_mod.promote(facts)
        saved = facts_mod.save(self.archive, facts) if self.archive is not None else 0
        touched = facts_mod.render_into_notes(self.store, facts)
        usage = facts_mod.program_usage(self.store, self.archive)
        drafted = facts_mod.synthesise_notes(self.store, index, usage, facts, bundle.glossary, bundle.co_read) \
            if len(index) else []
        log.info("facts: %d mined, %s, %d confirmed, %d saved; notes updated %d, drafted %d",
                 len(facts), ", ".join(f"{k} {v}" for k, v in counts.items()), promoted, saved,
                 len(touched), len(drafted))
        return {"mined": len(facts), **counts, "confirmed": promoted, "saved": saved,
                "notes_updated": touched, "drafted": [n.id for n in drafted]}

    # -- P8 the system card ----------------------------------------------------------- #

    async def card(self, apply: bool = False) -> dict:
        if self.llm is None:
            return {"skipped": "no model"}
        bundle = bundle_mod.load(self.build_dir) or bundle_mod.Bundle()
        reads = (bundle.co_read or {}).get("counts", {})
        tables = [{"name": n.name, "text": n.body, "reads": reads.get(n.name.upper(), 0)}
                  for n in self.store.notes() if n.type == "table"]
        tables.sort(key=lambda t: -t["reads"])
        concepts = [{"name": n.name, "text": n.body} for n in self.store.notes() if n.type == "concept"]
        fams = [{"id": n.id, "intent": n.question} for n in self.store.notes() if n.type == "family"]
        conventions = _conventions(self.store)
        sysid = _system_name()
        material = {"system": sysid, "tables": tables[:15], "concepts": concepts[:10],
                    "glossary": dict(list(bundle.glossary.items())[:25]), "families": fams[:15],
                    "conventions": conventions}
        try:
            text = await self.llm.complete(
                [{"role": "system", "content": CARD_PROMPT},
                 {"role": "user", "content": card_prompt(material)}],
                model=self.model, temperature=0.0,
                max_tokens=max(int(config.LLM_MAX_OUTPUT_TOKENS), 2048))
        except LLMError as exc:
            log.warning("card generation failed: %s", exc)
            return {"error": str(exc)}
        text = text.strip()
        if len(text) < 80:
            return {"rejected": "too short"}
        text = text[:2500]
        self.build_dir.mkdir(parents=True, exist_ok=True)
        (self.build_dir / "card.proposed.md").write_text(text + "\n", encoding="utf-8")
        applied = apply or config.KNOWLEDGE_CARD_AUTO
        if applied:
            (self.build_dir / "card.md").write_text(text + "\n", encoding="utf-8")
        log.info("system card: %d characters written to %s%s", len(text),
                 self.build_dir / "card.proposed.md", " and applied as card.md" if applied else "")
        return {"chars": len(text), "applied": bool(applied), "head": text[:160]}


def _system_name(archive=None) -> str:
    """'TC0 client 005, release 45B': the system id comes from the archive's
    sessions (SAP reports it at session open), the rest from the config."""
    sysid = ""
    try:
        from curie.archive import Archive
        path = Path(config.ARCHIVE_DB)
        if path.exists():
            a = Archive(path)
            try:
                rows = a.query("SELECT sysid FROM sessions WHERE sysid <> '' ORDER BY opened DESC LIMIT 1")
                sysid = rows[0]["sysid"] if rows else ""
            finally:
                a.close()
    except Exception:  # noqa: BLE001
        sysid = ""
    return (f"SAP {sysid or 'system'} client {config.SAP_RFC.get('client', '?')}, "
            f"release {config.SAP_RELEASE or '45B'}")


def _intents_ok(pl: str, en: str, params: dict) -> bool:
    if not pl and not en:
        return False
    holes = {"{" + spec.get("type", "") + "}" for spec in params.values()}
    holes = {h.replace("{range}", "{year}") for h in holes}
    for intent in (pl, en):
        if not intent:
            continue
        if not 12 <= len(intent) <= 300:
            return False
        for hole in holes:
            if hole == "{year}" and any(h in intent for h in ("{year}", "{month}", "{period}")):
                continue
            if hole not in intent:
                return False
    return True


def _conventions(store: KnowledgeStore) -> list[str]:
    """Conventions the confirmed programs show, stated deterministically."""
    from curie.memory import shape as sh

    out: list[str] = []
    seen_key = seen_padded = False
    key_fields: dict[str, str] = {}          # role -> table.field the key part fills
    year_fields: list[str] = []
    for n in store.notes():
        if n.type != "exemplar" or not n.program or not n.question:
            continue
        shape = sh.analyse(n.program)
        for p in sh.bind(n.question, shape):
            if sh.value_type(p.type) == "key":
                key_fields.setdefault(p.role, p.key)
                if not seen_key:
                    example = p.example
                    seen_key = True
            if p.type in ("year", "range") and p.key and p.key not in year_fields:
                year_fields.append(p.key)
            if p.type == "number" and p.literal.strip("'").startswith("0") and not seen_padded:
                out.append(f"{p.key} is zero padded to {len(p.literal.strip(chr(39)))} characters "
                           f"(the user wrote {p.example}).")
                seen_padded = True
    if seen_key and key_fields:
        parts = []
        for role, width in sh.KEY_ROLES:
            if role in key_fields:
                parts.append(f"{key_fields[role]} takes the part '{role}'"
                             + (f" zero padded to {width} characters" if width else ""))
        out.insert(0, f"Users write a document key as {example}; in the tables " + ", ".join(parts) + ".")
    if year_fields:
        out.append("A year in a question fills " + ", ".join(year_fields[:3]) + " (4 characters).")
    return out
