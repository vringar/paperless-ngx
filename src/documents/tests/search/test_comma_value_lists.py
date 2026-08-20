"""Comma-separated value lists, at result level.

``tag:foo,bar`` is a value list only for fields that opt into
``comma_values`` (``tag`` is the only one today). Nothing in the tree runs
a ``tag:`` fielded query against real documents; this covers foo-only,
bar-only and both-tags documents against a real index, plus a decoy
proving a non-comma_values field (``correspondent``) does NOT treat a
comma as a list separator -- ``correspondent:foo,bar`` searches for the
literal text "foo,bar", so a correspondent literally named that way
matches while foo-only/bar-only correspondents do not.

The list semantics are AND (a document must carry every listed value),
not OR: a foo-only or bar-only document does not match ``tag:foo,bar``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from documents.models import Correspondent
from documents.models import Document
from documents.models import Tag

if TYPE_CHECKING:
    from documents.search._backend import TantivyBackend

pytestmark = [pytest.mark.search, pytest.mark.django_db]


def _matched_ids(backend: TantivyBackend, query: str) -> set[int]:
    return set(backend.search_ids(query, user=None))


class TestTagCommaValueListIsConjunctive:
    @pytest.fixture
    def docs(self, backend: TantivyBackend) -> dict[str, int]:
        foo = Tag.objects.create(name="foo")
        bar = Tag.objects.create(name="bar")
        baz = Tag.objects.create(name="baz")

        foo_only = Document.objects.create(
            title="Foo only",
            content="x",
            checksum="comma-tag-foo-only",
        )
        foo_only.tags.set([foo])
        backend.add_or_update(foo_only)

        bar_only = Document.objects.create(
            title="Bar only",
            content="x",
            checksum="comma-tag-bar-only",
        )
        bar_only.tags.set([bar])
        backend.add_or_update(bar_only)

        both = Document.objects.create(
            title="Both tags",
            content="x",
            checksum="comma-tag-both",
        )
        both.tags.set([foo, bar])
        backend.add_or_update(both)

        neither = Document.objects.create(
            title="Neither tag",
            content="x",
            checksum="comma-tag-neither",
        )
        neither.tags.set([baz])
        backend.add_or_update(neither)

        return {
            "foo_only": foo_only.pk,
            "bar_only": bar_only.pk,
            "both": both.pk,
            "neither": neither.pk,
        }

    def test_only_the_document_carrying_both_tags_matches(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        assert _matched_ids(backend, "tag:foo,bar") == {docs["both"]}

    def test_foo_only_document_does_not_match(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        assert docs["foo_only"] not in _matched_ids(backend, "tag:foo,bar")

    def test_bar_only_document_does_not_match(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        assert docs["bar_only"] not in _matched_ids(backend, "tag:foo,bar")


class TestNonCommaValuesFieldTreatsCommaAsLiteralText:
    def test_correspondent_comma_is_not_a_value_list(
        self,
        backend: TantivyBackend,
    ) -> None:
        literal = Correspondent.objects.create(name="foo,bar")
        literal_doc = Document.objects.create(
            title="Literal correspondent",
            content="x",
            checksum="comma-correspondent-literal",
            correspondent=literal,
        )
        backend.add_or_update(literal_doc)

        foo_correspondent = Correspondent.objects.create(name="foo")
        foo_doc = Document.objects.create(
            title="Foo correspondent",
            content="x",
            checksum="comma-correspondent-foo",
            correspondent=foo_correspondent,
        )
        backend.add_or_update(foo_doc)

        bar_correspondent = Correspondent.objects.create(name="bar")
        bar_doc = Document.objects.create(
            title="Bar correspondent",
            content="x",
            checksum="comma-correspondent-bar",
            correspondent=bar_correspondent,
        )
        backend.add_or_update(bar_doc)

        # A value-list reading would match foo_doc and/or bar_doc (an OR)
        # or neither (an AND, since no single correspondent carries both
        # values). Either way it would NOT match literal_doc alone.
        assert _matched_ids(backend, "correspondent:foo,bar") == {literal_doc.pk}
