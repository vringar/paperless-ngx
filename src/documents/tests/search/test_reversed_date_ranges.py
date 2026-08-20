"""Reversed date ranges: an internal inconsistency between relative and
absolute bounds, pinned exactly as measured rather than "fixed" here.

Measured end to end (FROZEN_NOW = 2026-06-15T12:00:00Z):

    added:[now-1h to now+1h]          -> 2h window, correct order
    added:[now+1h to now-1h]          -> ~22h window, DAY-BUMPED, not swapped
    added:[2020-01-01 to 2019-01-01]  -> 366 days, SWAPPED to the forward order
    added:[2019-01-01 to 2020-01-01]  -> 366 days (same result either way)

Absolute reversed ranges swap their bounds back into order, matching
whoosh's own behavior. Relative (``now±``) reversed ranges do not swap --
whoosh-compat's date grammar instead adds a day to the upper bound,
producing a much wider window than either the forward or a swapped
reading would give. This is a library-level inconsistency between the two
range kinds, not an application-level rewrite paperless performs (there is
no reversed-range handling in documents/search/_query.py), so it is not
"fixed" here: fixing it belongs in whoosh-compat's date grammar, not in a
pre-parse rewrite on this side (see also the CJK/pre-parse-rewrites
docstrings elsewhere in this suite for the same discipline). Pinned as a
known inconsistency and a library follow-up. When whoosh-compat's date
grammar is fixed to swap consistently, the relative case's test below
inverts -- that inversion is the intended, visible signal.
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


class TestRelativeReversedRangeDayBumpsInsteadOfSwapping:
    @pytest.fixture
    def docs(self, backend: TantivyBackend) -> dict[str, int]:
        with time_machine.travel(FROZEN_NOW, tick=False):
            return {
                # Inside the correct (forward) 2h window [11:00, 13:00],
                # outside the day-bumped window [13:00, next-day 11:00).
                "in_forward_window": _index(
                    backend,
                    title="Forward window doc",
                    content="x",
                    checksum="reversed-relative-forward",
                    added=FROZEN_NOW,
                ).pk,
                # Outside the correct 2h window, inside the day-bumped
                # window the reversed query actually produces.
                "in_daybumped_window": _index(
                    backend,
                    title="Day-bumped window doc",
                    content="x",
                    checksum="reversed-relative-daybumped",
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

    def test_reversed_range_day_bumps_rather_than_swapping(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        # If this swapped like the absolute case below, it would match
        # "in_forward_window" (the same set as the forward query). It
        # instead matches only the day-bumped document -- the documented
        # library inconsistency.
        with time_machine.travel(FROZEN_NOW, tick=False):
            assert _matched_ids(backend, "added:[now+1h to now-1h]") == {
                docs["in_daybumped_window"],
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
