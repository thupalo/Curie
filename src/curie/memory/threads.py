"""Conversation threads: which prompts are follow-ups on an earlier question.

An exemplar captured under "Wygeneruj jeszcze raz poprawiając problem" is
useless: nobody will ask that again, and the business question the program
answers is two prompts back. A thread is the business question plus the
refinements that followed it; exemplar capture uses the thread, not the
last prompt. Detection is deterministic (word lists per language in
curie/lexicon.py); the model only rewrites the final question when there
were refinements.
"""
from __future__ import annotations

import re

from curie import lexicon

# Language-dependent: every word list below comes from curie/lexicon.py
# for the active languages (CURIE_LANGUAGES); nothing language-specific
# is written here.
#
# Words and phrases that refer to the previous answer, program or data, or
# ask to redo/adjust it.
FOLLOWUP = lexicon.regex("followup")
# A new business question: starts with an interrogative or names its own
# subject; these win over a follow-up marker inside the sentence.
QUESTION_START = lexicon.words_regex("question_start")
# Explicit references to the previous answer: a follow-up even when the
# prompt starts like a question ("Pokaż to samo dla umowy 200/2005").
REFERENCE = lexicon.regex("reference")
# A reference to an entity of the thread ("dla tej umowy", "tego klienta",
# "for this vehicle"): a follow-up only when the thread talked about that
# entity. "Czy możesz przedstawić amortyzację środka trwałego dla tej
# umowy" after a question about SAP GUI balances is a new topic; the same
# words after "umowa 100/2005" are a refinement.
ENTITY_REF = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in lexicon.merged_list("determiners")) + r")\s+([^\W\d_]{3,})",
    re.IGNORECASE)
WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
ENTITY_PREFIX = 4          # "umowy" / "umowa" / "umowie" share "umow"


def _stems(text: str) -> set[str]:
    return {w.lower()[:ENTITY_PREFIX] for w in WORD.findall(text or "")}


def entity_in_context(text: str, context: str | None) -> bool | None:
    """None when the text has no entity reference; otherwise whether the
    entity is part of the context (always True when no context is known,
    which keeps the older behaviour)."""
    m = ENTITY_REF.search(text or "")
    if m is None:
        return None
    if context is None:
        return True
    return m.group(1).lower()[:ENTITY_PREFIX] in _stems(context)


def is_followup(text: str, has_thread: bool = True, context: str | None = None) -> bool:
    """Does `text` refine the previous question rather than ask a new one?

    `context` is the thread so far (its question and the answers); with it,
    an entity reference counts only when the entity was in the thread."""
    if not has_thread:
        return False
    text = (text or "").strip()
    if not text:
        return False
    entity = entity_in_context(text, context)
    if QUESTION_START.match(text) and not REFERENCE.search(text) and not entity:
        return False
    return bool(FOLLOWUP.search(text)) or bool(entity)


# --------------------------------------------------------------------------- #
# General questions: SAP knowledge, not this system's data
# --------------------------------------------------------------------------- #

# How-to and explanation wording (language-dependent, curie/lexicon.py).
GENERAL = lexicon.regex("general")
# Identifiers that look like table or field names; SAP and ABAP words,
# currencies and module codes are not.
IDENT = re.compile(r"\b[A-Z][A-Z0-9_]{3,}\b")
IDENT_STOP = {"ABAP", "SAP", "ECC", "HANA", "SELECT", "WHERE", "FROM", "JOIN", "REPORT", "WRITE", "DATA",
              "LOOP", "ORDER", "GROUP", "INTO", "TABLE", "COUNT", "PLN", "EUR", "USD", "GBP", "CHF"}


def has_data_signal(text: str, table_exists=None) -> bool:
    """Typed values (an agreement, a year, a number, a quoted text), table
    or field names, or a program request: the question is about this
    system's data. Four-character identifiers are tables (KNB1) and
    transactions (FD10) alike; with `table_exists` (the dictionary index)
    only real tables count."""
    from curie.memory import shape as sh
    from curie.memory.evaluate import asks_for_program

    text = text or ""
    if asks_for_program(text):
        return True
    for v in sh.question_values(text):
        # Codes ("GUI", "FD10") and short numbers ("R/3 4.5B") are not
        # data by themselves; an agreement, a date or a quoted text is.
        if v.type == "code":
            continue
        if v.type == "number" and len(re.sub(r"\D", "", v.text)) < 3:
            continue
        return True
    for w in IDENT.findall(text):
        if w in IDENT_STOP:
            continue
        if len(w) == 4 and table_exists is not None and not table_exists(w):
            continue
        return True
    return False


def is_general(text: str, table_exists=None) -> bool:
    """A question about SAP or about Curie in general: how-to, what is,
    which transaction, who are you; nothing in it names data of this
    system. Such a turn takes the light path: no tools, the system card
    only, a short answer."""
    text = (text or "").strip()
    if not text or not GENERAL.search(text):
        return False
    return not has_data_signal(text, table_exists)


def compose_question(question: str, refinements: list[str]) -> str:
    """Fallback wording when the model cannot rewrite the question."""
    question = (question or "").strip()
    extra = [r.strip() for r in refinements if r.strip()]
    if not extra:
        return question
    return f"{question.rstrip('?')} ({'; '.join(extra)})"
