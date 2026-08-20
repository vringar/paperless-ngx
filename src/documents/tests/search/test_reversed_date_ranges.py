"""Reversed date ranges swap their bounds back into order.

Measured end to end (FROZEN_NOW = 2026-06-15T12:00:00Z):

    added:[now-1h to now+1h]          -> 2h window, correct order
    added:[now+1h to now-1h]          -> the same 2h window, SWAPPED
    added:[2020-01-01 to 2019-01-01]  -> 366 days, SWAPPED to the forward order
    added:[2019-01-01 to 2020-01-01]  -> 366 days (same result either way)

Both range kinds behave the same way, matching whoosh's own behavior.
Relative (``now±``) reversed ranges used to differ: whoosh-compat's date
grammar added a day to the upper bound instead of swapping, producing a
much wider window than either the forward or a swapped reading gives. That
was a library-level inconsistency between the two range kinds, not an
application-level rewrite paperless performs (there is still no
reversed-range handling in documents/search/_query.py), and it was fixed in
whoosh-compat's date grammar rather than in a pre-parse rewrite on this
side (see also the CJK/pre-parse-rewrites docstrings elsewhere in this
suite for the same discipline). Both cases are pinned below so a future
divergence between them is visible again.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

import pytest
import time_machine

from documents.models import Document

if TYPE_CHECKING:
    from documents.search._backend import TantivyBackend

pytestmark = [pytest.mark.search, pytest.mark.django_db]

FROZEN_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


def _matched_ids(backend: TantivyBackend, query: str) -> set[int]:
    return set(backend.search_ids(query, user=None))


def _index(backend: TantivyBackend, **kwargs: object) -> Document:
    doc = Document.objects.create(**kwargs)
    backend.add_or_update(doc)
    return doc


class TestRelativeReversedRangeSwaps:
    @pytest.fixture
    def docs(self, backend: TantivyBackend) -> dict[str, int]:
        with time_machine.travel(FROZEN_NOW, tick=False):
            return {
                # Inside the 2h window [11:00, 13:00] that both the forward
                # and the reversed spelling resolve to.
                "in_forward_window": _index(
                    backend,
                    title="Forward window doc",
                    content="x",
                    checksum="reversed-relative-forward",
                    added=FROZEN_NOW,
                ).pk,
                # Outside that window, but inside the wider window the
                # reversed spelling produced when it day-bumped instead of
                # swapping. It must not match either query now.
                "outside_the_window": _index(
                    backend,
                    title="Later same-week doc",
                    content="x",
                    checksum="reversed-relative-outside",
                    added=datetime(2026, 6, 16, 8, 0, tzinfo=UTC),
                ).pk,
            }

    def test_forward_range_matches_only_the_two_hour_window(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        with time_machine.travel(FROZEN_NOW, tick=False):
            assert _matched_ids(backend, "added:[now-1h to now+1h]") == {
                docs["in_forward_window"],
            }

    def test_reversed_range_swaps_to_the_same_window(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        # The same set as the forward query, and in particular not the
        # document that only a day-bumped upper bound would have reached.
        with time_machine.travel(FROZEN_NOW, tick=False):
            assert _matched_ids(backend, "added:[now+1h to now-1h]") == {
                docs["in_forward_window"],
            }


class TestAbsoluteReversedRangeSwaps:
    @pytest.fixture
    def docs(self, backend: TantivyBackend) -> dict[str, int]:
        return {
            "in_range": _index(
                backend,
                title="In-range doc",
                content="x",
                checksum="reversed-absolute-in-range",
                added=datetime(2019, 6, 1, tzinfo=UTC),
            ).pk,
            "out_of_range": _index(
                backend,
                title="Out-of-range doc",
                content="x",
                checksum="reversed-absolute-out-of-range",
                added=datetime(2018, 6, 1, tzinfo=UTC),
            ).pk,
        }

    def test_forward_range_matches_the_in_range_document(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        assert _matched_ids(
            backend,
            "added:[2019-01-01 to 2020-01-01]",
        ) == {docs["in_range"]}

    def test_reversed_range_swaps_to_match_the_same_document(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        assert _matched_ids(
            backend,
            "added:[2020-01-01 to 2019-01-01]",
        ) == {docs["in_range"]}
