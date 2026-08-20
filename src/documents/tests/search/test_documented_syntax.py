"""Pins the search syntax that ``docs/usage.md`` promises users.

Every query here appears verbatim, or as a direct paraphrase, in the
"Document searches" section of ``docs/usage.md``. Each case indexes real
documents and asserts on matched document IDs rather than on the parsed
query, because a query that parses cleanly is not necessarily a query that
means what the documentation says it means: ``added:now - 3 days`` parses
without a single diagnostic and then matches nothing.

The negative cases matter as much as the positive ones. They pin the
behaviours the docs explicitly warn about, so that if any of them ever
starts working the warning can be removed deliberately rather than being
left standing as a lie.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

import pytest
import time_machine

from documents.models import Document
from documents.models import DocumentType
from documents.models import Note
from documents.models import StoragePath
from documents.models import Tag

if TYPE_CHECKING:
    from collections.abc import Generator

    from django.contrib.auth.models import User

    from documents.search._backend import TantivyBackend

pytestmark = [pytest.mark.search, pytest.mark.django_db]

# A Monday, so that "next monday"/"last monday" land a clean week either side.
FROZEN_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)

# The checksum used in the docs' `checksum:` example.
DOC_CHECKSUM = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


def _matched_ids(backend: TantivyBackend, query: str) -> set[int]:
    return set(backend.search_ids(query, user=None))


def _index(backend: TantivyBackend, **kwargs: object) -> Document:
    doc = Document.objects.create(**kwargs)
    backend.add_or_update(doc)
    return doc


class TestLogicalExpressions:
    @pytest.fixture
    def docs(self, backend: TantivyBackend) -> dict[str, int]:
        return {
            "secret": _index(
                backend,
                title="Invoice one",
                content="invoice secret contents",
                checksum="doc-syntax-secret",
            ).pk,
            "plain": _index(
                backend,
                title="Invoice two",
                content="invoice ordinary contents",
                checksum="doc-syntax-plain",
            ).pk,
        }

    def test_not_excludes_a_term(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        assert _matched_ids(backend, "invoice NOT secret") == {docs["plain"]}

    def test_leading_hyphen_requires_the_term_instead_of_excluding_it(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        # The docs warn about exactly this: separators are stripped at index
        # time, so "-secret" is the term "secret" and the query is an AND.
        assert _matched_ids(backend, "invoice -secret") == {docs["secret"]}

    def test_or_inside_parentheses_matches_either_branch(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        matched = _matched_ids(backend, "invoice AND (secret OR ordinary)")
        assert matched == {docs["secret"], docs["plain"]}


class TestPhraseSearch:
    def test_quoted_phrase_requires_the_words_in_order(
        self,
        backend: TantivyBackend,
    ) -> None:
        doc = _index(
            backend,
            title="Phrase",
            content="the quick brown fox jumps",
            checksum="doc-syntax-phrase",
        )
        assert _matched_ids(backend, '"quick brown fox"') == {doc.pk}
        assert _matched_ids(backend, '"brown quick fox"') == set()


class TestFieldAliases:
    def test_type_is_an_alias_for_document_type(
        self,
        backend: TantivyBackend,
    ) -> None:
        doc_type = DocumentType.objects.create(name="invoice")
        doc = _index(
            backend,
            title="Typed",
            content="body",
            checksum="doc-syntax-typed",
            document_type=doc_type,
        )
        assert _matched_ids(backend, "type:invoice") == {doc.pk}
        assert _matched_ids(backend, "document_type:invoice") == {doc.pk}

    def test_path_is_an_alias_for_storage_path(
        self,
        backend: TantivyBackend,
    ) -> None:
        storage_path = StoragePath.objects.create(
            name="archive",
            path="archive/{{ title }}",
        )
        doc = _index(
            backend,
            title="Pathed",
            content="body",
            checksum="doc-syntax-pathed",
            storage_path=storage_path,
        )
        assert _matched_ids(backend, "path:archive") == {doc.pk}
        assert _matched_ids(backend, "storage_path:archive") == {doc.pk}


class TestTagCommaList:
    def test_comma_list_requires_every_listed_tag(
        self,
        backend: TantivyBackend,
    ) -> None:
        bills = Tag.objects.create(name="bills")
        unpaid = Tag.objects.create(name="unpaid")
        archived = Tag.objects.create(name="archived")

        both = Document.objects.create(
            title="Both tags",
            content="body",
            checksum="doc-syntax-tag-both",
        )
        both.tags.add(bills, unpaid)
        backend.add_or_update(both)

        one = Document.objects.create(
            title="One tag",
            content="body",
            checksum="doc-syntax-tag-one",
        )
        one.tags.add(bills, archived)
        backend.add_or_update(one)

        assert _matched_ids(backend, "tag:bills,unpaid") == {both.pk}
        assert _matched_ids(backend, "tag:bills") == {both.pk, one.pk}


class TestArchiveMetadataFields:
    @pytest.fixture
    def doc(self, backend: TantivyBackend, admin_user: User) -> Document:
        doc = Document.objects.create(
            title="Metadata",
            content="body",
            checksum=DOC_CHECKSUM,
            archive_serial_number=100,
            page_count=12,
            original_filename="invoice.pdf",
        )
        Note.objects.create(document=doc, user=admin_user, note="a note")
        backend.add_or_update(doc)
        return doc

    @pytest.mark.parametrize(
        "query",
        [
            "asn:100",
            "asn:[50 to 150]",
            "page_count:12",
            "page_count:[10 to 20]",
            "num_notes:1",
            "num_notes:[1 to 5]",
            "original_filename:invoice.pdf",
            f"checksum:{DOC_CHECKSUM}",
            "checksum:9f86d081*",
        ],
    )
    def test_documented_metadata_query_matches(
        self,
        backend: TantivyBackend,
        doc: Document,
        query: str,
    ) -> None:
        assert _matched_ids(backend, query) == {doc.pk}

    @pytest.mark.parametrize(
        "query",
        [
            # The docs say only a complete, lowercase checksum matches.
            "checksum:9f86d081",
            f"checksum:{DOC_CHECKSUM.upper()}",
        ],
    )
    def test_partial_or_uppercase_checksum_matches_nothing(
        self,
        backend: TantivyBackend,
        doc: Document,
        query: str,
    ) -> None:
        assert _matched_ids(backend, query) == set()


class TestDocumentedDateForms:
    @pytest.fixture(autouse=True)
    def frozen_now(self) -> Generator[None, None, None]:
        with time_machine.travel(FROZEN_NOW, tick=False):
            yield

    @pytest.fixture
    def dated(self, backend: TantivyBackend) -> dict[str, int]:
        stamps = {
            "today": datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
            "yesterday": datetime(2026, 6, 14, 9, 0, tzinfo=UTC),
            "tomorrow": datetime(2026, 6, 16, 9, 0, tzinfo=UTC),
            "next_monday": datetime(2026, 6, 22, 10, 0, tzinfo=UTC),
            "last_monday": datetime(2026, 6, 8, 10, 0, tzinfo=UTC),
            "january": datetime(2026, 1, 10, 10, 0, tzinfo=UTC),
            "old": datetime(2005, 3, 4, 15, 30, tzinfo=UTC),
        }
        return {
            label: _index(
                backend,
                title=label,
                content="dated body",
                checksum=f"doc-syntax-date-{label}",
                added=stamp,
            ).pk
            for label, stamp in stamps.items()
        }

    @pytest.mark.parametrize(
        ("query", "label"),
        [
            ("added:today", "today"),
            ("added:yesterday", "yesterday"),
            ("added:tomorrow", "tomorrow"),
            ('added:"next monday"', "next_monday"),
            ('added:"last monday"', "last_monday"),
            ("added:january", "january"),
            ("added:2005-03-04", "old"),
            ("added:2005-03", "old"),
            ("added:[2005-01-01 to 2005-12-31]", "old"),
            ("added:[2005 to 2009]", "old"),
        ],
    )
    def test_documented_date_form_matches_its_day_or_month(
        self,
        backend: TantivyBackend,
        dated: dict[str, int],
        query: str,
        label: str,
    ) -> None:
        assert _matched_ids(backend, query) == {dated[label]}

    @pytest.mark.parametrize(
        "query",
        [
            # Zero-width: these resolve to a single instant, not a span, so
            # nothing in a realistic corpus lands on them. The docs warn
            # about them rather than presenting them as usable.
            "added:now",
            "added:noon",
            "added:midnight",
            'added:"-3 days"',
            'added:"-1 week"',
            # The trap: this reports no diagnostics but parses as
            # And(added:now, "3", "days") - added:now plus stray text.
            "added:now - 3 days",
            # A time-of-day component is split off and searched as text.
            "added:2005-03-04T15:30:00Z",
        ],
    )
    def test_forms_the_docs_warn_about_match_nothing(
        self,
        backend: TantivyBackend,
        dated: dict[str, int],
        query: str,
    ) -> None:
        assert _matched_ids(backend, query) == set()
