"""Wildcard patterns must match a stemmed index.

Query patterns are normalized but were not stemmed, while index terms are
stemmed, so the natural spelling of a prefix search matched nothing:
``invoice*`` found no document although ``invoic*`` did. v2's index was
UNSTEMMED (whoosh ``TEXT()`` defaults to ``StandardAnalyzer``), so this
regressed against both baselines.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from documents.models import Document
from documents.search._registry import _make_pattern_normalizer
from documents.search._tokenizer import ascii_fold
from documents.search._tokenizer import paperless_text_analyzer
from documents.search._tokenizer import stem_pattern_text

if TYPE_CHECKING:
    from documents.search._backend import TantivyBackend

pytestmark = [pytest.mark.search, pytest.mark.django_db]

CONTENT = (
    "invoice total due for electricity from both companies, "
    "payments made to the university library, copies attached"
)


def _matched_ids(backend: TantivyBackend, query: str) -> set[int]:
    return set(backend.search_ids(query, user=None))


@pytest.fixture
def indexed_doc(backend: TantivyBackend) -> Document:
    doc = Document.objects.create(
        title="Invoice 2020 productname",
        content=CONTENT,
        checksum="pattern-stemming-1",
        archive_serial_number=900,
    )
    backend.add_or_update(doc)
    return doc


class TestPrefixStemming:
    @pytest.mark.parametrize(
        "query",
        [
            "invoice*",
            "electricity*",
            "companies*",
            "payments*",
            "library*",
            "title:Invoice*",
        ],
    )
    def test_full_word_prefix_matches_its_stem(
        self,
        backend: TantivyBackend,
        indexed_doc: Document,
        query: str,
    ) -> None:
        assert _matched_ids(backend, query) == {indexed_doc.id}

    @pytest.mark.parametrize("query", ["invoic*", "electr*", "payment*"])
    def test_already_stemmed_prefix_still_matches(
        self,
        backend: TantivyBackend,
        indexed_doc: Document,
        query: str,
    ) -> None:
        assert _matched_ids(backend, query) == {indexed_doc.id}

    @pytest.mark.parametrize("query", ["univers*", "librar*"])
    def test_partial_prefix_is_not_lengthened_by_its_stem(
        self,
        backend: TantivyBackend,
        indexed_doc: Document,
        query: str,
    ) -> None:
        """A partial prefix keeps matching: "librar" stems to "librari", which
        is longer than what was typed, so the typed run is kept. Using the
        shorter of the two widens recall rather than failing closed."""
        assert _matched_ids(backend, query) == {indexed_doc.id}

    def test_pattern_past_the_stem_boundary_is_documented_not_fixed(
        self,
        backend: TantivyBackend,
        indexed_doc: Document,
    ) -> None:
        """produ*name cannot match a stemmed index ("productname" is indexed as
        "productnam"); usage.md must not advertise it. Pinned so the limitation
        is deliberate, not accidental."""
        assert _matched_ids(backend, "produ*name") == set()

    def test_stem_substitution_loses_compounds_accepted_trade(
        self,
        backend: TantivyBackend,
        indexed_doc: Document,
    ) -> None:
        """English stemming substitutes as well as truncates: "copy" and
        "copies" both index as "copi", while "copyright" keeps its literal "y".
        So "copy*" reaches the base word and its inflections but no longer
        reaches the compound, which it did before patterns were stemmed. That
        trade is deliberate: the same substitution is what makes "company*" and
        "library*" work at all, and no rule over one normalized string tells the
        two apart. Matching both would need the pattern to be emitted as a
        disjunction of the folded and stemmed forms, which belongs in the
        emitter, not here.
        """
        compound = Document.objects.create(
            title="Copyright notice",
            content="copyright notice for the work",
            checksum="pattern-stemming-2",
            archive_serial_number=901,
        )
        backend.add_or_update(compound)

        assert _matched_ids(backend, "copy*") == {indexed_doc.id}
        assert _matched_ids(backend, "copyright*") == {compound.id}


class TestStemsMatchTheIndexAnalyzer:
    """stem_pattern_text rebuilds paperless_text_analyzer's stemming tail rather
    than sharing it, so a filter added to the index analyzer alone would silently
    stop patterns from reaching the terms it produces.
    """

    @pytest.mark.parametrize(
        "language",
        ["en", "de", "fr", "es", "sv", None, "klingon"],
    )
    @pytest.mark.parametrize(
        "word",
        ["Copies", "copyright", "Companies", "Invoices", "laufen", "casas", "Straße"],
    )
    def test_stem_equals_the_index_term(self, word: str, language: str | None) -> None:
        indexed = paperless_text_analyzer(language).analyze(word)[0]
        assert stem_pattern_text(ascii_fold(word.lower()), language) == indexed


class TestPatternNormalizer:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Invoice", "invoic"),
            ("companies", "compani"),
            # y -> i: same length as typed, and the index only holds the stem
            ("library", "librari"),
            ("invoic", "invoic"),
            ("Universit", "universit"),
            ("Café", "cafe"),
        ],
    )
    def test_shorter_of_the_typed_run_and_its_stem(
        self,
        text: str,
        expected: str,
    ) -> None:
        assert _make_pattern_normalizer("en")(text) == expected

    def test_run_that_yields_no_token_falls_back_to_the_typed_run(self) -> None:
        """A run past the remove_long limit analyzes to zero tokens, so there is
        no stem to substitute and the folded run is used as typed."""
        over_long = "invoices" * 20
        assert _make_pattern_normalizer("en")(over_long) == over_long

    @pytest.mark.parametrize("language", [None, "klingon"])
    def test_unstemmed_language_folds_only(self, language: str | None) -> None:
        """With no stemmer configured, or one this build has no stemmer for, the
        index holds surface forms and the pattern must keep them too."""
        assert _make_pattern_normalizer(language)("Invoices") == "invoices"
