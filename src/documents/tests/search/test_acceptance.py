"""Result-level acceptance corpus: real documents indexed via build_schema(),
real queries run through parse_user_query(), matched-document-ID sets
asserted — not intermediate ASTs or query strings. This is paperless-ngx's
analogue of whoosh-compat's own tests/emitter/test_acceptance_e2e.py.

Supersedes test_query.py's TestParseUserQuery result-level cases.
"""

from __future__ import annotations

from datetime import UTC
from datetime import date
from datetime import datetime
from typing import TYPE_CHECKING

import pytest
import time_machine

from documents.models import CustomField
from documents.models import CustomFieldInstance
from documents.models import Document
from documents.models import Note
from documents.models import Tag
from documents.search._query import parse_user_query

if TYPE_CHECKING:
    from documents.search._backend import TantivyBackend

pytestmark = [pytest.mark.search, pytest.mark.django_db]

FROZEN_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


def _matched_ids(backend: TantivyBackend, query: str) -> set[int]:
    return set(backend.search_ids(query, user=None))


@pytest.fixture
def indexed_documents(backend: TantivyBackend) -> dict[str, int]:
    """Index a small fixture set, return {label: doc_id} for corpus queries."""
    docs = {
        "invoice_2020": Document.objects.create(
            title="Invoice 2020",
            content="invoice total due",
            checksum="acc-invoice-2020",
            archive_serial_number=100,
        ),
        "invoice_2021": Document.objects.create(
            title="Invoice 2021",
            content="invoice total due",
            checksum="acc-invoice-2021",
            archive_serial_number=101,
        ),
        "invoice_2023": Document.objects.create(
            title="Invoice 2023",
            content="invoice total due",
            checksum="acc-invoice-2023",
            archive_serial_number=102,
        ),
        "receipt_2022": Document.objects.create(
            title="Receipt 2022",
            content="receipt total due",
            checksum="acc-receipt-2022",
            archive_serial_number=103,
        ),
    }
    for doc in docs.values():
        backend.add_or_update(doc)
    return {label: doc.pk for label, doc in docs.items()}


class TestIssue13568BracketWildcard:
    """paperless-ngx#13568: title:202[0-3]* must keep its character class,
    not fold to a prefix query that silently drops it (whoosh-compat
    DIVERGENCES.md entry 13)."""

    def test_bracket_class_wildcard_matches_only_in_range_years(
        self,
        backend: TantivyBackend,
        indexed_documents: dict[str, int],
    ) -> None:
        # [0-1] (not [0-3]) is deliberate: the fixture's four years are
        # 2020/2021/2022/2023, i.e. their trailing digit is 0/1/2/3
        # respectively - a [0-3] class would match all four and the test
        # would pass even if the character class were silently dropped and
        # folded to an unconstrained "202*" prefix. [0-1] partitions the
        # fixture into a genuine in-range/out-of-range split.
        matched = _matched_ids(backend, "title:202[0-1]*")
        expected = {
            indexed_documents["invoice_2020"],
            indexed_documents["invoice_2021"],
        }
        assert matched == expected, (
            "title:202[0-1]* must match 2020/2021 titles and exclude 2022/2023 "
            "- if this matches everything, the wildcard's character class was "
            "silently dropped (issue #13568's original bug)"
        )


class TestCommaValueLists:
    """whoosh-compat's CommaValuesPlugin splits `tag:foo,bar` into
    `tag:foo AND tag:bar` (DIVERGENCES.md entries 17/36), matching real
    Whoosh's KEYWORD(commas=True) analyzer-time comma splitting - not an OR
    across the listed values. A document must carry every listed tag to
    match."""

    def test_tag_comma_list_matches_only_documents_with_both_tags(
        self,
        backend: TantivyBackend,
    ) -> None:
        tag_foo = Tag.objects.create(name="foo")
        tag_bar = Tag.objects.create(name="bar")
        tag_baz = Tag.objects.create(name="baz")

        doc_both = Document.objects.create(
            title="Both",
            content="x",
            checksum="acc-comma-both",
        )
        doc_both.tags.add(tag_foo, tag_bar)
        doc_foo_only = Document.objects.create(
            title="FooOnly",
            content="x",
            checksum="acc-comma-foo",
        )
        doc_foo_only.tags.add(tag_foo)
        doc_other = Document.objects.create(
            title="Other",
            content="x",
            checksum="acc-comma-other",
        )
        doc_other.tags.add(tag_baz)
        for doc in (doc_both, doc_foo_only, doc_other):
            backend.add_or_update(doc)
        matched = _matched_ids(backend, "tag:foo,bar")
        assert matched == {doc_both.pk}


class TestFieldBoosts:
    def test_title_boost_ranks_title_match_above_content_only_match(
        self,
        backend: TantivyBackend,
    ) -> None:
        title_match = Document.objects.create(
            title="urgent",
            content="nothing else relevant",
            checksum="acc-boost-title",
        )
        content_match = Document.objects.create(
            title="nothing",
            content="urgent matter here",
            checksum="acc-boost-content",
        )
        backend.add_or_update(title_match)
        backend.add_or_update(content_match)
        query = parse_user_query(backend._index, "urgent", UTC)
        searcher = backend._index.searcher()
        results = searcher.search(query, limit=10)
        ranked_ids = [
            searcher.doc(addr).to_dict()["id"][0] for _score, addr in results.hits
        ]
        assert ranked_ids[0] == title_match.pk


class TestJsonSubpaths:
    def test_notes_user_matches_document_with_that_note_author(
        self,
        backend: TantivyBackend,
    ) -> None:
        from django.contrib.auth.models import User

        alice = User.objects.create_user(username="alice")
        doc_with_note = Document.objects.create(
            title="Has note",
            content="x",
            checksum="acc-note-with",
        )
        Note.objects.create(document=doc_with_note, user=alice, note="reminder")
        doc_without = Document.objects.create(
            title="No note",
            content="x",
            checksum="acc-note-without",
        )
        backend.add_or_update(doc_with_note)
        backend.add_or_update(doc_without)
        matched = _matched_ids(backend, "notes.user:alice")
        assert matched == {doc_with_note.pk}

    def test_custom_fields_name_and_value_combine(
        self,
        backend: TantivyBackend,
    ) -> None:
        field = CustomField.objects.create(
            name="Contract Number",
            data_type=CustomField.FieldDataType.STRING,
        )
        other_field = CustomField.objects.create(
            name="Other Field",
            data_type=CustomField.FieldDataType.STRING,
        )
        matching = Document.objects.create(
            title="Matching",
            content="x",
            checksum="acc-cf-matching",
        )
        CustomFieldInstance.objects.create(
            document=matching,
            field=field,
            value_text="policy",
        )
        non_matching = Document.objects.create(
            title="Non-matching",
            content="x",
            checksum="acc-cf-nonmatching",
        )
        CustomFieldInstance.objects.create(
            document=non_matching,
            field=other_field,
            value_text="policy",
        )
        backend.add_or_update(matching)
        backend.add_or_update(non_matching)
        matched = _matched_ids(
            backend,
            'custom_fields.name:"Contract Number" custom_fields.value:policy',
        )
        assert matched == {matching.pk}


class TestMultitokenInNestedOr:
    """whoosh-compat DIVERGENCES.md entry 15: Multitoken.DEFAULT resolves by
    syntactic enclosing group, not the parser's fixed default group. Prove
    it doesn't matter for paperless's actual data/fields."""

    def test_multitoken_tag_value_inside_top_level_or_matches_either_branch(
        self,
        backend: TantivyBackend,
    ) -> None:
        # "multi word tag" is a multitoken field value; nested inside a
        # top-level OR with an unrelated clause.
        doc_a = Document.objects.create(title="A", content="x", checksum="acc-mt-a")
        doc_a.tags.create(name="multi word tag")
        doc_b = Document.objects.create(title="B", content="x", checksum="acc-mt-b")
        doc_b.tags.create(name="unrelated")
        backend.add_or_update(doc_a)
        backend.add_or_update(doc_b)
        matched = _matched_ids(backend, 'tag:"multi word tag" OR title:B')
        assert matched == {doc_a.pk, doc_b.pk}


class TestRfc3339TZDateRange:
    """paperless-ngx#13010: created/added bracket ranges using RFC3339 T/Z
    datetime separators (e.g. `[2026-01-01T00:00:00Z TO ...]`) must keep
    working - this is v2/Whoosh saved-search backward compatibility, not a
    generic ISO-format nicety. whoosh-compat's own grammar previously had no
    support for `T`/`Z` at all (fixed upstream, whoosh-compat commit
    f936143); this proves the fix holds end-to-end against real indexed
    documents and real timezone-sensitive matching, not just that the
    library's date_from() parses the text."""

    def test_t_z_range_matches_only_documents_within_bounds(
        self,
        backend: TantivyBackend,
    ) -> None:
        in_range = Document.objects.create(
            title="In range",
            content="x",
            checksum="acc-rfc3339-in-range",
            added=datetime(2026, 3, 15, 10, 0, tzinfo=UTC),
        )
        out_of_range = Document.objects.create(
            title="Out of range",
            content="x",
            checksum="acc-rfc3339-out-of-range",
            added=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        )
        for doc in (in_range, out_of_range):
            backend.add_or_update(doc)
        matched = _matched_ids(
            backend,
            "added:[2026-01-01T00:00:00Z TO 2026-06-01T00:00:00Z]",
        )
        assert matched == {in_range.pk}

    def test_comma_combined_t_z_ranges_across_two_fields(
        self,
        backend: TantivyBackend,
    ) -> None:
        # The Whoosh v2 comma syntax: two field:[range] expressions joined
        # by a comma must be ANDed together (not passed to Tantivy as a
        # literal comma, which it cannot parse).
        matching = Document.objects.create(
            title="Matches both ranges",
            content="x",
            checksum="acc-rfc3339-comma-match",
            created=date(2026, 3, 15),
            added=datetime(2026, 5, 15, 10, 0, tzinfo=UTC),
        )
        wrong_added = Document.objects.create(
            title="created in range, added out of range",
            content="x",
            checksum="acc-rfc3339-comma-wrong-added",
            created=date(2026, 3, 15),
            added=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        )
        for doc in (matching, wrong_added):
            backend.add_or_update(doc)
        matched = _matched_ids(
            backend,
            "created:[2026-01-01T00:00:00Z TO 2026-06-01T00:00:00Z],"
            "added:[2026-05-01T00:00:00Z TO 2026-06-01T00:00:00Z]",
        )
        assert matched == {matching.pk}

    def test_z_suffixed_bound_is_absolute_utc_not_local_shifted(
        self,
        backend: TantivyBackend,
    ) -> None:
        # A document timestamped exactly at a Z-suffixed range boundary must
        # match under UTC - if Z were (incorrectly) reinterpreted as local
        # wall-clock time and shifted again, this exact-boundary match would
        # silently fail or succeed for the wrong reason.
        at_boundary = Document.objects.create(
            title="At Z boundary",
            content="x",
            checksum="acc-rfc3339-z-boundary",
            added=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        backend.add_or_update(at_boundary)
        matched = _matched_ids(
            backend,
            "added:[2026-01-01T00:00:00Z TO 2026-01-01T00:00:01Z]",
        )
        assert matched == {at_boundary.pk}


class TestUnregisteredIdFieldFoldsToLiteralText:
    """tag_id, owner_id, etc. are intentionally excluded from the
    FieldRegistry: they were always internal index columns (v2 consumed
    them for permission filtering and its own criteria), and their
    queryability as search syntax was an accident of whoosh resolving any
    schema field name. whoosh-compat parity leniency folds them into a
    literal text search rather than raising a diagnostic/400. Prove the
    fold is inert against real data, not just that parsing doesn't
    raise."""

    def test_tag_id_query_matches_nothing(
        self,
        backend: TantivyBackend,
        indexed_documents: dict[str, int],
    ) -> None:
        matched = _matched_ids(backend, "tag_id:5")
        assert matched == set()


class TestFuzzyBlendSurvivesWhooshGrammar:
    """A query mixing whoosh-only grammar (a date keyword) with a typo'd
    free-text word must still fuzzy-match the intended document when
    ADVANCED_FUZZY_SEARCH_THRESHOLD is enabled. The fuzzy clause is built
    from the parsed query's free-text tokens (whoosh_compat's
    free_text_tokens), never from the raw query string, so whoosh grammar
    that tantivy's own parser rejects cannot knock the fuzzy clause out."""

    def test_typo_fuzzy_matches_alongside_date_keyword(
        self,
        backend: TantivyBackend,
        settings,
    ) -> None:
        settings.ADVANCED_FUZZY_SEARCH_THRESHOLD = 0.5
        with time_machine.travel(FROZEN_NOW, tick=False):
            doc = Document.objects.create(
                title="Receipt March",
                content="receipt total due",
                checksum="fuzzy-blend-1",
                archive_serial_number=900,
            )
            backend.add_or_update(doc)
            # Sanity: the exact spelling matches through the exact clause.
            assert doc.pk in _matched_ids(backend, "added:today receipt")
            # The regression: the misspelling (one transposition) only
            # matches via the fuzzy clause, and "added:today" is
            # whoosh-only grammar tantivy's parser rejects, so raw-string
            # fuzzy parsing skips the clause entirely and this returns
            # nothing. The typo is deliberate; keep codespell away from it.
            typo_query = "added:today reciept"  # codespell:ignore reciept
            assert doc.pk in _matched_ids(backend, typo_query)

    def test_negated_words_do_not_fuzzy_match(
        self,
        backend: TantivyBackend,
        settings,
    ) -> None:
        # A term the user excluded must not resurface through the fuzzy
        # clause. The shape is chosen so this genuinely discriminates: the
        # indexed document contains the NOT'd word but NOT the positive
        # word, so nothing matches the exact clause, and a fuzzy string
        # naively built from ALL words (including the NOT'd one) would
        # make this document the sole hit, normalize its score to 1.0,
        # and survive any threshold. (A shape with an exact-matching
        # sibling document does NOT discriminate: normalization ranks the
        # resurfaced doc far below the exact match and the threshold cuts
        # it even for a naive implementation.)
        settings.ADVANCED_FUZZY_SEARCH_THRESHOLD = 0.5
        with time_machine.travel(FROZEN_NOW, tick=False):
            receipt_only = Document.objects.create(
                title="Receipt Archive",
                content="receipt archived stack",
                checksum="fuzzy-blend-2",
                archive_serial_number=901,
            )
            backend.add_or_update(receipt_only)
            assert _matched_ids(backend, "added:today total NOT receipt") == set()


class TestUnquotedDateKeywordPhrases:
    """The unquoted multi-word date keyword spelling (added:previous month)
    has been honored continuously since the whoosh era, always by an
    app-level assist, never by any parser: v2 rewrote it to a bracket range
    before whoosh saw it, and the deleted _translate.py consumed it itself.
    whoosh-compat scopes the unquoted form out of its parser on purpose
    (its DIVERGENCES.md entry 19) but understands the quoted form natively,
    so paperless quotes the closed phrase vocabulary on date fields before
    parsing. Only quoting happens app-side; every date computation stays in
    whoosh-compat."""

    @pytest.fixture
    def period_documents(self, backend: TantivyBackend) -> dict[str, int]:
        with time_machine.travel(FROZEN_NOW, tick=False):
            in_may = Document.objects.create(
                title="May Doc",
                content="statement",
                checksum="kw-may",
                archive_serial_number=910,
                added=datetime(2026, 5, 20, 12, 0, tzinfo=UTC),
            )
            in_june = Document.objects.create(
                title="June Doc",
                content="statement",
                checksum="kw-june",
                archive_serial_number=911,
                added=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
            )
            for doc in (in_may, in_june):
                backend.add_or_update(doc)
        return {"in_may": in_may.pk, "in_june": in_june.pk}

    @pytest.mark.parametrize(
        "query",
        [
            pytest.param("added:previous month", id="unquoted"),
            pytest.param('added:"previous month"', id="quoted"),
            pytest.param("added:Previous Month", id="unquoted-mixed-case"),
        ],
    )
    def test_unquoted_matches_the_same_documents_as_quoted(
        self,
        backend: TantivyBackend,
        period_documents: dict[str, int],
        query: str,
    ) -> None:
        with time_machine.travel(FROZEN_NOW, tick=False):
            assert _matched_ids(backend, query) == {period_documents["in_may"]}

    @pytest.mark.parametrize(
        "query",
        [
            pytest.param("added:this month", id="this-month"),
            pytest.param("added:this year", id="this-year"),
            pytest.param("added:previous week", id="previous-week"),
            pytest.param("added:previous quarter", id="previous-quarter"),
            pytest.param("added:previous year", id="previous-year"),
            pytest.param("created:previous month", id="created-field"),
            pytest.param("modified:previous month", id="modified-field"),
        ],
    )
    def test_every_phrase_and_date_field_parses_without_error(
        self,
        backend: TantivyBackend,
        period_documents: dict[str, int],
        query: str,
    ) -> None:
        # The whole vocabulary times every date field must at least parse
        # and search cleanly (no SearchQueryError -> no HTTP 400); exact
        # window semantics are whoosh-compat's, pinned in its own suite.
        with time_machine.travel(FROZEN_NOW, tick=False):
            _matched_ids(backend, query)

    def test_text_field_keyword_words_are_not_rewritten(
        self,
        backend: TantivyBackend,
        period_documents: dict[str, int],
    ) -> None:
        # "previous month" after a TEXT field (or unfielded) is ordinary
        # text, not a date phrase: a title actually containing the words
        # matches, and the date-window documents do not.
        with time_machine.travel(FROZEN_NOW, tick=False):
            wordy = Document.objects.create(
                title="Notes from the previous month",
                content="meeting notes",
                checksum="kw-text",
                archive_serial_number=912,
            )
            backend.add_or_update(wordy)
            assert _matched_ids(backend, "title:previous month") == {wordy.pk}


class TestBareJsonFieldPrefixes:
    """The v2 whoosh schema had plural notes/custom_fields TEXT fields, so
    "notes:foo" and "custom_fields:foo" were valid fielded searches in
    released paperless (and at the tantivy translation layer). On the
    whoosh-compat registry they are JSON fields addressable only via
    subpaths, and the bare spelling would demote to a nonsense unfielded
    text search. parse_user_query rewrites the bare prefixes live to the
    same targets migration 0017 chose for the singular whoosh-era
    spellings: notes: -> notes.note:, custom_fields: ->
    custom_fields.value:."""

    def test_bare_notes_prefix_searches_note_text(
        self,
        backend: TantivyBackend,
    ) -> None:
        from django.contrib.auth.models import User

        alice = User.objects.create_user(username="alice")
        with_note = Document.objects.create(
            title="Has note",
            content="x",
            checksum="bare-notes-with",
        )
        Note.objects.create(document=with_note, user=alice, note="crocodile")
        # This document's CONTENT contains the words a demoted text search
        # would match; it must NOT match once the prefix addresses notes.
        decoy = Document.objects.create(
            title="Notes about things",
            content="notes crocodile mention",
            checksum="bare-notes-decoy",
        )
        backend.add_or_update(with_note)
        backend.add_or_update(decoy)
        assert _matched_ids(backend, "notes:crocodile") == {with_note.pk}

    def test_bare_custom_fields_prefix_searches_values(
        self,
        backend: TantivyBackend,
    ) -> None:
        field = CustomField.objects.create(
            name="Policy Number",
            data_type=CustomField.FieldDataType.STRING,
        )
        with_value = Document.objects.create(
            title="Has field",
            content="x",
            checksum="bare-cf-with",
        )
        CustomFieldInstance.objects.create(
            document=with_value,
            field=field,
            value_text="crocodile",
        )
        decoy = Document.objects.create(
            title="Custom things",
            content="custom fields crocodile",
            checksum="bare-cf-decoy",
        )
        backend.add_or_update(with_value)
        backend.add_or_update(decoy)
        assert _matched_ids(backend, "custom_fields:crocodile") == {with_value.pk}

    def test_subpath_spellings_are_untouched(
        self,
        backend: TantivyBackend,
    ) -> None:
        from django.contrib.auth.models import User

        bob = User.objects.create_user(username="bob")
        doc = Document.objects.create(
            title="Bob note",
            content="x",
            checksum="bare-subpath",
        )
        Note.objects.create(document=doc, user=bob, note="remark")
        backend.add_or_update(doc)
        assert _matched_ids(backend, "notes.user:bob") == {doc.pk}
        assert _matched_ids(backend, "notes.note:remark") == {doc.pk}


class TestFieldAliases:
    """type:/path: are registry aliases for document_type:/storage_path:.
    The only other alias coverage is parse-shape; these prove resolution
    end-to-end against a real index."""

    def test_type_alias_and_canonical_name_match_the_same_document(
        self,
        backend: TantivyBackend,
    ) -> None:
        from documents.models import DocumentType

        invoice_type = DocumentType.objects.create(name="invoice")
        # Discriminating shape: document_type is itself a default search
        # field, so if alias resolution ever broke and "type:invoice"
        # demoted to unfielded text, the token would STILL match the typed
        # document through the field value. The decoy carries the query
        # word in content, so a demoted search matches BOTH documents and
        # the exact-set assertions fail. (The title avoids stemming to
        # "type": english stems Typed -> type.)
        typed = Document.objects.create(
            title="First",
            content="quarterly statement",
            checksum="alias-type-1",
            document_type=invoice_type,
        )
        decoy = Document.objects.create(
            title="Second",
            content="invoice mentioned in body",
            checksum="alias-type-2",
        )
        backend.add_or_update(typed)
        backend.add_or_update(decoy)
        assert _matched_ids(backend, "type:invoice") == {typed.pk}
        assert _matched_ids(backend, "document_type:invoice") == {typed.pk}

    def test_path_alias_and_canonical_name_match_the_same_document(
        self,
        backend: TantivyBackend,
    ) -> None:
        from documents.models import StoragePath

        archive = StoragePath.objects.create(name="archive", path="archive/{title}")
        stored = Document.objects.create(
            title="Stored",
            content="quarterly statement",
            checksum="alias-path-1",
            storage_path=archive,
        )
        # storage_path is NOT a default search field today, so a demoted
        # "path:archive" already matches nothing; the content decoy keeps
        # this test discriminating even if it ever joins the defaults.
        loose = Document.objects.create(
            title="Loose",
            content="archive mentioned in body",
            checksum="alias-path-2",
        )
        backend.add_or_update(stored)
        backend.add_or_update(loose)
        assert _matched_ids(backend, "path:archive") == {stored.pk}
        assert _matched_ids(backend, "storage_path:archive") == {stored.pk}


class TestCreatedTimezoneInvariance:
    def test_created_date_matches_regardless_of_active_timezone(
        self,
        backend: TantivyBackend,
    ) -> None:
        # "created" is a date-only field indexed at naive midnight: a
        # document created 2020-06-10 must match created:20200610 whether
        # the active timezone is far ahead of or behind UTC. (The
        # timezone-SENSITIVE datetime fields have their own boundary
        # coverage in test_api_search.py's tz-ahead/tz-behind tests.)
        from django.utils import timezone as django_tz

        doc = Document.objects.create(
            title="Dated",
            content="x",
            checksum="tz-inv-1",
            created=date(2020, 6, 10),
        )
        backend.add_or_update(doc)
        for tzname in ("Pacific/Auckland", "America/New_York"):
            with django_tz.override(tzname):
                assert _matched_ids(backend, "created:20200610") == {doc.pk}, tzname


class TestReversedDateRange:
    def test_reversed_bounds_still_match_the_span(
        self,
        backend: TantivyBackend,
    ) -> None:
        # whoosh's joint disambiguation swaps backwards bounds (both years
        # explicit -> plain swap), and whoosh-compat reproduces it; a saved
        # view with created:[2025 TO 2020] must keep matching the span
        # instead of becoming an empty lo>hi range.
        inside = Document.objects.create(
            title="Inside",
            content="x",
            checksum="rev-range-1",
            created=date(2022, 5, 1),
        )
        outside = Document.objects.create(
            title="Outside",
            content="x",
            checksum="rev-range-2",
            created=date(2019, 5, 1),
        )
        backend.add_or_update(inside)
        backend.add_or_update(outside)
        assert _matched_ids(backend, "created:[2025 TO 2020]") == {inside.pk}
