"""Result-level acceptance corpus: real documents indexed via build_schema(),
real queries run through parse_user_query(), matched-document-ID sets
asserted — not intermediate ASTs or query strings. This is paperless-ngx's
analogue of whoosh-compat's own tests/emitter/test_acceptance_e2e.py.

Supersedes test_query.py's TestParseUserQuery result-level cases and the
now-deleted test_date_grammar_parity.py.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

import pytest

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


class TestUnregisteredIdFieldFoldsToLiteralText:
    """tag_id, owner_id, etc. are intentionally excluded from the
    FieldRegistry - whoosh-compat parity leniency folds them into a literal
    text search rather than raising a diagnostic/400 (see docs/usage.md's
    advanced-search section). Prove the fold is inert against real data, not
    just that parsing doesn't raise."""

    def test_tag_id_query_matches_nothing(
        self,
        backend: TantivyBackend,
        indexed_documents: dict[str, int],
    ) -> None:
        matched = _matched_ids(backend, "tag_id:5")
        assert matched == set()
