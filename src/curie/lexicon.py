"""Every language-dependent rule of Curie, in one place.

The runtime reads questions in the users' language: is a prompt a
follow-up or a new question, is it a general how-to, does it ask for a
program, which month is "lutego", which words carry no meaning. Those
rules are word lists, and word lists belong to a language, not to the
code that applies them. This module holds them per ISO 639-1 language;
`CURIE_LANGUAGES` (default: the logon language and English) selects the
ones in use, and the modules build their regexes from the union:

    from curie import lexicon
    FOLLOWUP = lexicon.regex("followup")           # one pattern over the active languages
    MONTHS = lexicon.merged_dict("months")

Adding a language means adding one entry to LANGUAGES below; nothing
else changes. English is always active, because the model, the ABAP
keywords and the SAP texts use it. Customer conventions (a document key
such as "100/2005", codes to ignore) are not language and live in the
configuration (KEY_PATTERN, KEY_ROLES, VALUE_STOP_CODES).

Keys of a language entry:

    followup         regex fragments: the prompt refines the previous answer
    question_start   words that open a new business question
    reference        regex fragments: explicit references to the previous answer
    determiners      words before an entity ("this vehicle", "tej umowy")
    general          regex fragments: how-to, what-is, who-are-you wording
    program_request  regex fragments: the user asks for a program
    months           month names and abbreviations (accents stripped) -> 1..12
    year_suffix      regex fragment that may follow a year ("2005 r.", "2005 roku")
    stopwords        question words and function words the glossary ignores
    markers          words that identify the language (for detection)
    letters          (accented letters, their ASCII stand-ins) for normalisation
"""
from __future__ import annotations

import re

from curie import config

LANGUAGES: dict[str, dict] = {
    "en": {
        "followup": [
            r"\bagain\b", r"\bredo\b", r"\bfix\b", r"\bcorrect(?:ed|ion)?\b", r"\binstead\b",
            r"\bskip\b", r"\bomit\b", r"\bremove\b", r"\badd\b", r"\bchange\b", r"\bthese data\b",
            r"\bthis data\b", r"\bthe same\b", r"\bprevious\b", r"\babove\b", r"\bthat program\b",
            r"\bthe program\b", r"\bwithout the column\b", r"\bsort(?:ed)? by\b", r"\bonly\b",
            r"^\s*yes[.,!]?\s", r"^\s*no[.,!]?\s",
        ],
        "question_start": ["how", "what", "which", "who", "when", "where", "why", "count", "find",
                           "show", "list", "calculate", "check"],
        "reference": [r"\bthese data\b", r"\bthis data\b", r"\bthe same\b", r"\bprevious\b",
                      r"\babove\b", r"\bagain\b", r"^\s*yes[.,!]?\s"],
        "determiners": ["this", "that", "these", "those"],
        "general": [
            r"\bwho are you\b", r"\bwhat are you\b", r"\bwhat is\b", r"\bwhat does\b",
            r"\bhow (?:do|can|to|does|is)\b", r"\bexplain\b", r"\bdescribe\b", r"\bwhich transaction\b",
            r"\bintroduce yourself\b", r"\bsap gui\b", r"\btransaction\b", r"\bt-?code\b",
        ],
        "program_request": [
            r"write\s+(?:me\s+)?(?:a\s+|an\s+)?(?:abap\s+)?(?:program|report)",
            r"create\s+(?:a\s+)?(?:program|report)", r"generate\s+(?:a\s+)?(?:program|report)",
            r"abap\s+code", r"abap\s+program",
        ],
        "months": {
            "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
            "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
            "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
            "december": 12, "dec": 12,
        },
        "year_suffix": "",
        "stopwords": {
            "the", "show", "list", "how", "many", "what", "which", "who", "when", "where", "count", "find",
            "table", "from", "with", "for", "and", "that", "this", "get", "give", "all", "first", "last",
            "records", "record", "contents", "content", "number", "numbers",
        },
        "markers": ["the", "show", "list", "how", "what", "which", "count", "find", "table", "of", "for",
                    "from", "are", "you"],
        "letters": ("", ""),
    },
    "pl": {
        "followup": [
            r"jeszcze raz", r"ponownie", r"popraw", r"poprawi", r"napraw", r"zmie[nń]", r"dodaj",
            r"usu[nń]", r"pomi[nń]", r"zamiast", r"skup si[eę]", r"tych danych", r"te dane",
            r"tego programu", r"ten program", r"powy[zż]sz", r"poprzedni", r"wcze[sś]niejsz",
            r"tak samo", r"to samo", r"tej samej", r"tego samego", r"wygeneruj (?:jeszcze|ponownie|znowu)",
            r"spr[oó]buj", r"rozszerz", r"skr[oó][cć]", r"uwzgl[eę]dnij", r"bez kolumn", r"kolumn[eęy]",
            r"posortuj", r"sortuj", r"w formie", r"jako tabel", r"tylko", r"r[oó]wnie[zż]",
            # "Skoryguj powyższe", "Użyj proszę funkcji", "Zobacz za co była faktura",
            # "Tak. Napisz program", "Jaki to był pojazd". "A teraz pokaż ... dla
            # umowy 200/2005" is deliberately not one: it carries its own values.
            r"skoryguj", r"u[zż]yj", r"zobacz", r"^\s*(?:a|i|oraz)\s+(?:jeszcze|zobacz)",
            r"^\s*tak[.,!]?\s", r"^\s*nie[.,!]?\s",
            r"\bjaki to\b", r"\bjaka to\b", r"\bjakie to\b",
        ],
        "question_start": ["ile", "jakie", "jaki", "jaka", "które", "który", "która", "kto", "kiedy", "czy",
                           "gdzie", "dlaczego", "podaj", "policz", "znajdź", "znajdz", "wyszukaj", "pokaż",
                           "pokaz", "wylicz", "oblicz", "sprawdź", "sprawdz"],
        "reference": [r"tych danych", r"te dane", r"tego programu", r"ten program", r"powy[zż]sz",
                      r"poprzedni", r"wcze[sś]niejsz", r"to samo", r"tak samo", r"tej samej",
                      r"tego samego", r"jeszcze raz", r"ponownie", r"\bjaki to\b", r"\bjaka to\b",
                      r"\bjakie to\b", r"^\s*tak[.,!]?\s"],
        "determiners": ["tej", "tego", "tych", "tym", "tę", "tą"],
        "general": [
            r"\bjak(?:\s+\w+){0,3}\s+(?:wy[sś]wietli|sprawdzi|zobaczy|znale[zź]|ustawi|skonfigurowa|wykona|"
            r"uruchomi|zrobi|dzia[lł]a|wyznaczy|obliczy|wygl[aą]da)",
            r"\bw jaki spos[oó]b\b", r"\bco to (?:jest|znaczy)\b", r"\bco oznacza\b", r"\bwyja[sś]nij\b",
            r"\bopisz\b", r"\bom[oó]w\b", r"\bna podstawie wiedzy og[oó]lnej\b", r"\btransakcj\w*\b",
            r"\bkim jeste[sś]\b", r"\bkto ty\b", r"\bprzedstaw si[eę]\b",
        ],
        "program_request": [
            r"napisz\s+(?:mi\s+|proszę\s+)?program", r"napisz\s+raport", r"stw[oó]rz\s+program",
            r"utw[oó]rz\s+(?:program|raport)", r"wygeneruj\s+(?:program|raport)", r"program\s+abap",
            r"kod\s+abap",
        ],
        "months": {
            "styczen": 1, "stycznia": 1, "styczniu": 1, "sty": 1,
            "luty": 2, "lutego": 2, "lutym": 2, "lut": 2,
            "marzec": 3, "marca": 3, "marcu": 3, "mar": 3,
            "kwiecien": 4, "kwietnia": 4, "kwietniu": 4, "kwi": 4,
            "maj": 5, "maja": 5, "maju": 5,
            "czerwiec": 6, "czerwca": 6, "czerwcu": 6, "cze": 6,
            "lipiec": 7, "lipca": 7, "lipcu": 7, "lip": 7,
            "sierpien": 8, "sierpnia": 8, "sierpniu": 8, "sie": 8,
            "wrzesien": 9, "wrzesnia": 9, "wrzesniu": 9, "wrz": 9,
            "pazdziernik": 10, "pazdziernika": 10, "pazdzierniku": 10, "paz": 10,
            "listopad": 11, "listopada": 11, "listopadzie": 11, "lis": 11,
            "grudzien": 12, "grudnia": 12, "grudniu": 12, "gru": 12,
        },
        "year_suffix": r"r\.?|\s*roku",
        "stopwords": {
            "ile", "jakie", "jaki", "jaka", "które", "ktore", "który", "ktory", "która", "ktora", "czy",
            "gdzie", "kiedy", "podaj", "pokaż", "pokaz", "wylistuj", "policz", "znajdź", "znajdz", "napisz",
            "program", "programu", "wyświetl", "wyswietl", "oblicz", "wylicz", "sprawdź", "sprawdz", "proszę",
            "prosze", "dla", "oraz", "lub", "albo", "jest", "był", "byla", "były", "byly", "była", "został",
            "zostały", "zostało", "zostala", "zostaly", "zostalo", "ktorych", "których", "tabeli",
            "tabela", "tabelę", "tabele", "tablei", "roku", "okresie", "okres", "liczba", "liczbę", "liczbe",
            "pierwsze", "pierwszych", "wszystkich", "wszystkie", "wartości", "wartosci", "najwyższej",
            "najwiecej", "najwięcej", "podstawie", "według", "wedlug", "zestawienie", "danych", "dane",
            "numerze", "numer", "numery", "numerów", "numerow",
        },
        "markers": ["ile", "jakie", "jaki", "podaj", "pokaż", "pokaz", "dla", "które", "ktore", "czy",
                    "wylistuj", "napisz", "jest", "oraz", "kim"],
        "letters": ("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ"),
    },
}


def languages() -> list[str]:
    """The active languages: CURIE_LANGUAGES, else the logon language and
    English; English always, unknown codes dropped."""
    wanted = [c.strip().lower() for c in (config.CURIE_LANGUAGES or []) if c.strip()]
    out: list[str] = []
    for code in wanted + ["en"]:
        if code in LANGUAGES and code not in out:
            out.append(code)
    return out


def merged_list(key: str) -> list[str]:
    out: list[str] = []
    for code in languages():
        for item in LANGUAGES[code].get(key) or []:
            if item not in out:
                out.append(item)
    return out


def merged_set(key: str) -> set[str]:
    out: set[str] = set()
    for code in languages():
        out |= set(LANGUAGES[code].get(key) or ())
    return out


def merged_dict(key: str) -> dict:
    out: dict = {}
    for code in languages():
        out.update(LANGUAGES[code].get(key) or {})
    return out


def regex(key: str, flags: int = re.IGNORECASE) -> re.Pattern:
    """One pattern over the active languages' fragments of `key`; matches
    nothing when the key is empty."""
    parts = merged_list(key)
    return re.compile("|".join(parts) if parts else r"(?!x)x", flags)


def words_regex(key: str, flags: int = re.IGNORECASE) -> re.Pattern:
    """A pattern matching any of the words of `key` at the start of the text."""
    words = merged_list(key)
    return re.compile(r"^\s*(?:" + "|".join(re.escape(w) for w in words) + r")\b" if words else r"(?!x)x",
                      flags)


def year_suffix() -> str:
    """The alternation that may follow a year, or an empty string."""
    parts = [LANGUAGES[c]["year_suffix"] for c in languages() if LANGUAGES[c].get("year_suffix")]
    return "|".join(parts)


def transliteration() -> dict:
    """str.translate table folding the accented letters of every active
    language to ASCII (for matching words regardless of accents)."""
    src, dst = "", ""
    for code in languages():
        a, b = LANGUAGES[code].get("letters") or ("", "")
        src += a
        dst += b
    return str.maketrans(src, dst)


def fold(text: str) -> str:
    return (text or "").translate(transliteration())


def detect(text: str) -> str:
    """The language a text is written in, among the known ones (all of
    LANGUAGES, not only the active): the upper-cased ISO code, or "" when
    nothing identifies it."""
    text = text or ""
    lower = text.lower()
    best, best_hits = "", 0
    for code, entry in LANGUAGES.items():
        hits = 0
        letters = (entry.get("letters") or ("", ""))[0]
        if letters:
            hits += sum(1 for ch in text if ch in letters)
        for w in entry.get("markers") or []:
            hits += len(re.findall(r"\b" + re.escape(w) + r"\b", lower))
        if hits > best_hits:
            best, best_hits = code, hits
    return best.upper()
