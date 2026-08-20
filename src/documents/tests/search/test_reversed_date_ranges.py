"""A reversed relative range, resolved end to end against the index.

whoosh-compat owns the bound swap itself and asserts it directly, for both
the absolute and the relative spelling
(``test_reversed_relative_range_swaps_like_the_absolute_case`` in
``tests/test_parser_dates.py``, DIVERGENCES.md entry 53). Paperless does not
rewrite reversed ranges anywhere; ``documents/search/_query.py`` passes the
query through untouched.

What is kept here is the relative spelling only, because it is the one whose
bounds depend on paperless's own plumbing: ``now±1h`` is resolved against the
timezone paperless hands the parser, so the emitted window is paperless's
result rather than the library's alone. The corpus separates a document
inside the two-hour window from one that is outside it but inside the wider
window a non-swapping reading produces, so a bound that resolves in the wrong
timezone, or a swap that does not happen, matches the wrong set.
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


@pytest.fixture
def docs(backend: TantivyBackend) -> dict[str, int]:
    with time_machine.travel(FROZEN_NOW, tick=False):
        return {
            # Inside the 2h window [11:00, 13:00] that both the forward and
            # the reversed spelling resolve to.
            "in_window": _index(
                backend,
                title="Forward window doc",
                content="x",
                checksum="reversed-relative-forward",
                added=FROZEN_NOW,
            ).pk,
            # Outside that window, but inside the wider window a reversed
            # range that day-bumped its upper bound instead of swapping
            # would reach.
            "outside_the_window": _index(
                backend,
                title="Later same-week doc",
                content="x",
                checksum="reversed-relative-outside",
                added=datetime(2026, 6, 16, 8, 0, tzinfo=UTC),
            ).pk,
        }


def test_reversed_relative_range_matches_the_two_hour_window(
    backend: TantivyBackend,
    docs: dict[str, int],
) -> None:
    with time_machine.travel(FROZEN_NOW, tick=False):
        assert _matched_ids(backend, "added:[now+1h to now-1h]") == {
            docs["in_window"],
        }
