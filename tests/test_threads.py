"""Threads: entity references need their antecedent; general questions."""
from curie.memory.threads import entity_in_context, has_data_signal, is_followup, is_general

GUI = "Pokaż mi jak wyświetlić saldo klienta w FI za pomocą SAP GUI"
ESSAY = "Użyj transakcji FD10 (saldo odbiorcy) i FBL5 (pozycje). Konto zbiorcze jest w KNB1-AKONT."
AGREEMENT = "Podaj dane środka trwałego dla umowy 100/2005"


def test_entity_reference_counts_only_with_its_antecedent():
    text = "Czy możesz mi przedstawić jak wyglądała amortyzacja środka trwałego dla tej umowy w FI"
    assert entity_in_context(text, None) is True                 # no context known: as before
    assert entity_in_context(text, GUI + "\n" + ESSAY) is False   # nobody mentioned an agreement
    assert entity_in_context(text, AGREEMENT) is True             # "umowy" ~ "umowy 100/2005"
    assert entity_in_context("Pokaż to jeszcze raz", AGREEMENT) is None
    # The archived case: after the SAP GUI thread it is a new question ...
    assert is_followup(text, context=GUI + "\n" + ESSAY) is False
    # ... after an agreement question it refines the thread, and without
    # context the old behaviour holds.
    assert is_followup(text, context=AGREEMENT) is True
    assert is_followup(text) is True
    # Strong references to the answer stay follow-ups whatever the topic.
    assert is_followup("Skoryguj powyższe wskazówki z uwzględnieniem faktu, że używam SAP R/3 4.5B",
                       context=GUI) is True
    assert is_followup("Tak. Napisz program, który wyznaczy dane środka trwałego", context=GUI) is True
    # Inflection: "tego klienta" after "klient 125812".
    assert is_followup("Pokaż faktury tego klienta", context="Podaj saldo klienta 125812") is True
    assert is_followup("Pokaż faktury tego klienta", context="Ile umów zawarto w 2004?") is False


def test_general_questions_and_data_signals():
    general = [GUI, "Na podstawie wiedzy ogólnej podaj sposób wyznaczenia numeru konta zbiorczego odbiorcy w SAP FI.",
               "Who are you?", "Kim jesteś?", "Co to jest konto zbiorcze?", "Jak działa amortyzacja w FI-AA?",
               "Which transaction shows a vendor balance?", "Explain the difference between FD10 and FBL5"]
    data = ["Jakie jest saldo klienta umowy 100/2005r w SAP FI?", "Co to jest pole AKONT w KNB1?",
            "Ile umów zawarto w 2004?", "Jak wyświetlić umowy z ZUMOWYL?", "Napisz program, który policzy umowy",
            "Explain what 'CARC' means", "Podaj dane dostawcy"]
    tables = {"KNB1", "KNA1", "T001", "ZUMOWYL"}
    exists = lambda n: n in tables   # noqa: E731
    assert all(is_general(t, exists) for t in general), [t for t in general if not is_general(t, exists)]
    assert not any(is_general(t, exists) for t in data), [t for t in data if is_general(t, exists)]
    assert has_data_signal("Podaj saldo klienta 125812") and not has_data_signal("Jak działa SAP GUI?")
    assert not has_data_signal("Explain ABAP SELECT statements")      # keywords are not tables
    # Without a dictionary every four-character identifier counts as a table.
    assert not is_general("Explain the difference between FD10 and FBL5")
    assert has_data_signal("Co to jest KNB1?", exists) and not has_data_signal("Co to jest FD10?", exists)
