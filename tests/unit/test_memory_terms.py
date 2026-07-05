"""Turkish recall tokenizer: stemming, normalization, term expansion."""

from __future__ import annotations

from akana.memory.terms import (
    normalize_recall_query,
    recall_search_terms,
    turkish_possessive_stem,
)


def test_possessive_stem_known_and_suffix() -> None:
    assert turkish_possessive_stem("adım") == "ad"
    assert turkish_possessive_stem("ismim") == "isim"
    assert turkish_possessive_stem("projelerim") == "proje"
    assert turkish_possessive_stem("ab") == "ab"  # too short, untouched
    assert turkish_possessive_stem("kahve") == "kahve"  # no possessive suffix


def test_normalize_strips_recall_boilerplate() -> None:
    assert normalize_recall_query("ne hatırlıyorsun benim adım hakkında") != ""
    # "benim X ne?" -> subject stem
    assert normalize_recall_query("benim adım ne?") == "ad"
    assert "remember" not in normalize_recall_query("do you remember my coffee").lower()


def test_recall_terms_includes_raw_and_dedups() -> None:
    terms = recall_search_terms("kahve")
    assert "kahve" in terms
    assert len(terms) == len({t.lower() for t in terms})  # no dupes


def test_recall_terms_expands_aliases() -> None:
    # asking about "adım" should also probe isim/name keys
    terms = {t.lower() for t in recall_search_terms("benim adım ne?")}
    assert "ad" in terms
    assert "isim" in terms or "name" in terms


def test_recall_terms_empty() -> None:
    assert recall_search_terms("") == []
    assert recall_search_terms("   ") == []
    assert recall_search_terms("a") == []  # single char below min length
