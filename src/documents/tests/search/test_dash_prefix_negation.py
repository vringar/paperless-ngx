"""Pins the current, deferred-negation behavior of a leading ``-``.

Negation via a bare ``-`` prefix (as opposed to the ``NOT`` keyword) is
deferred to the companion plan's G1 item. Until G1 lands, a leading ``-``
is not negation at all:

- Unfielded (``-taxes``): the separator is stripped at index time, so the
  term becomes an ordinary, *required* positive match on ``taxes`` -- the
  exact inverse of what a user typing ``-taxes`` to exclude a term intends.
- Fielded (``-title:alpha``): the leading hyphen detaches from the field
  clause entirely (the parse tree emits it as its own token), and the
  field clause itself is left as an ordinary positive match. The
  negation is dropped, not inverted.

If G1 changes either of these, the corresponding test below inverts --
that inversion is the intended, visible signal that G1 landed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from documents.models import Document

if TYPE_CHECKING:
    from documents.search._backend import TantivyBackend

pytestmark = [pytest.mark.search, pytest.mark.django_db]


def _matched_ids(backend: TantivyBackend, query: str) -> set[int]:
    return set(backend.search_ids(query, user=None))


def _index(backend: TantivyBackend, **kwargs: object) -> Document:
    doc = Document.objects.create(**kwargs)
    backend.add_or_update(doc)
    return doc


class TestUnfieldedDashBecomesARequiredTerm:
    @pytest.fixture
    def docs(self, backend: TantivyBackend) -> dict[str, int]:
        return {
            "with_taxes": _index(
                backend,
                title="Invoice one",
                content="invoice taxes included",
                checksum="dash-unfielded-with",
            ).pk,
            "without_taxes": _index(
                backend,
                title="Invoice two",
                content="invoice ordinary contents",
                checksum="dash-unfielded-without",
            ).pk,
        }

    def test_dash_prefixed_term_matches_only_the_document_containing_it(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        # If this were real negation, it would match "without_taxes" (the
        # document that does NOT contain "taxes"). It instead matches
        # "with_taxes" -- the document that DOES.
        assert _matched_ids(backend, "invoice -taxes") == {docs["with_taxes"]}

    def test_bare_dash_prefixed_term_alone_is_a_positive_search(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        assert _matched_ids(backend, "-taxes") == {docs["with_taxes"]}


class TestFieldedDashDropsTheNegation:
    @pytest.fixture
    def docs(self, backend: TantivyBackend) -> dict[str, int]:
        return {
            "alpha": _index(
                backend,
                title="Alpha Title",
                content="alpha body",
                checksum="dash-fielded-alpha",
            ).pk,
            "beta": _index(
                backend,
                title="Beta Title",
                content="beta body",
                checksum="dash-fielded-beta",
            ).pk,
        }

    def test_dash_fielded_term_matches_the_field_value_it_names(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        # Real negation would exclude the "beta" document. Dropped
        # negation instead matches it, identically to "title:beta".
        assert _matched_ids(backend, "-title:beta") == {docs["beta"]}
        assert _matched_ids(backend, "title:beta") == _matched_ids(
            backend,
            "-title:beta",
        )

    def test_dash_fielded_term_conjoined_still_requires_both_sides(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        # Discriminating shape: if the dash-fielded clause were dropped
        # from the query entirely (rather than kept as a positive
        # requirement), this would match "alpha" alone. Because the
        # dropped negation still leaves an ordinary AND-ed requirement
        # behind, and no document has both titles, nothing matches.
        assert _matched_ids(backend, "title:alpha -title:beta") == set()
