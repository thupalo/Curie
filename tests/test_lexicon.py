"""The lexicon: word lists per language, and installation conventions
that are configuration, not code."""
from curie import config, lexicon
from curie.memory import shape as sh


def test_active_languages_always_include_english(monkeypatch):
    monkeypatch.setattr(config, "CURIE_LANGUAGES", ["PL"])
    assert lexicon.languages() == ["pl", "en"]
    monkeypatch.setattr(config, "CURIE_LANGUAGES", ["xx", "en"])
    assert lexicon.languages() == ["en"]
    monkeypatch.setattr(config, "CURIE_LANGUAGES", [])
    assert lexicon.languages() == ["en"]


def test_word_lists_merge_and_build_patterns(monkeypatch):
    monkeypatch.setattr(config, "CURIE_LANGUAGES", ["pl", "en"])
    assert lexicon.merged_dict("months")["lutego"] == 2 and lexicon.merged_dict("months")["feb"] == 2
    assert lexicon.regex("program_request").search("Napisz program liczący umowy")
    assert lexicon.regex("program_request").search("Write me an ABAP report")
    assert lexicon.words_regex("question_start").match("Ile umów?") and lexicon.words_regex("question_start").match("How many?")
    assert "roku" in lexicon.year_suffix()
    assert lexicon.fold("Umów łącznie") == "Umow lacznie"
    monkeypatch.setattr(config, "CURIE_LANGUAGES", ["en"])
    assert "lutego" not in lexicon.merged_dict("months") and lexicon.year_suffix() == ""
    assert not lexicon.regex("program_request").search("Napisz program")


def test_detection_covers_every_known_language():
    assert lexicon.detect("Ile umów zawarto w 2004 roku?") == "PL"
    assert lexicon.detect("Show me the contents of table T000") == "EN"
    assert lexicon.detect("T000 KNA1") == ""


def test_document_key_is_a_convention_of_the_installation():
    # Another installation: invoices written INV-2005-000123, stored as a
    # year and a ten-digit number.
    sh.configure(r"\bINV-(\d{4})-(\d{1,10})\b", "gjahr:0,belnr:10", [])
    vals = sh.question_values("Pokaż fakturę INV-2005-123 i umowę 100/2005")
    assert [(v.type, v.text) for v in vals if v.type == "key"] == [("key", "INV-2005-123")]
    key = next(v for v in vals if v.type == "key")
    assert key.renderings == {"text": "INV-2005-123", "gjahr": "2005", "belnr": "0000000123"}
    assert "100/2005" not in [v.text for v in vals if v.type == "key"]     # the old convention is gone
    # No key at all: the number/year notation is not a value (digits next
    # to a slash are never a bare year or number), so nothing is extracted.
    sh.configure("", "", [])
    assert sh.question_values("umowa 100/2005") == []
    assert [v.type for v in sh.question_values("umowa 100 z 2005")] == ["number", "year"]
    # A pattern whose groups do not match the roles is switched off.
    sh.configure(r"(\d+)/(\d+)", "one:0", [])
    assert sh.KEY is None
    # Older family notes say "agreement": the alias still fills.
    sh.configure(r"\b(\d{1,7})/(\d{4})\b", "nrum:7,rokum:0", [])
    filled = sh.fill("WHERE nrum = {{nrum}} AND rokum = {{rokum}}.",
                     {"nrum": {"type": "agreement", "role": "nrum", "literal": "'0000100'", "example": "100/2005"},
                      "rokum": {"type": "agreement", "role": "rokum", "literal": "'2005'", "example": "100/2005"}},
                     sh.question_values("umowa 7/2004"))
    assert filled is not None and filled[1] == {"nrum": "0000007", "rokum": "2004"}


def test_stop_codes_come_from_the_configuration():
    sh.configure(r"\b(\d{1,7})/(\d{4})\b", "nrum:7,rokum:0", ["TOW"])
    assert "TOW" in sh.STOP_CODES and "SAP" in sh.STOP_CODES
    assert [v.text for v in sh.question_values("Pomiń kolumny TOW i pokaż PZST")] == ["PZST"]
    sh.configure(r"\b(\d{1,7})/(\d{4})\b", "nrum:7,rokum:0", [])
    assert "TOW" in [v.text for v in sh.question_values("Pomiń kolumny TOW")]
