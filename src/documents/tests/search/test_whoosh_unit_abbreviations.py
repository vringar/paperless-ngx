"""Whoosh-style relative date unit abbreviations: yrs/mos/wks/hrs/mins/secs.

Zero assertions existed anywhere in this tree before this file -- the old
v2-era ``TestWhooshUnitAbbreviations`` is gone, and whoosh-compat's own
test corpus only carries these spellings as allowlisted (assertion-
inverted) lines, i.e. lines it knows do not fully match its own grammar's
documented behavior yet accepts anyway. This is the exact class of
regression #13482 was: a spelling silently stops parsing (or silently
stops matching) with nothing anywhere to notice.

Each abbreviation produces a relative, zero-width instant
(``now - N<unit>`` .. same instant), not a span -- pinned separately in
the date-keyword tests. What matters here is that each of the six
spellings resolves to the *correct* instant, checked by indexing one
document at exactly that instant per unit: a wrong offset (an "hrs" typo
that resolves as minutes, say) lands on nothing, or on a sibling
document's instant, rather than passing by accident.
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
def unit_documents(backend: TantivyBackend) -> dict[str, int]:
    """One document at the exact instant each abbreviation should resolve
    to, all indexed together so a wrong offset lands on the wrong (or no)
    document rather than passing coincidentally."""
    with time_machine.travel(FROZEN_NOW, tick=False):
        return {
            "yrs": _index(
                backend,
                title="Years doc",
                content="x",
                checksum="unit-abbrev-yrs",
                added=datetime(2024, 6, 15, 12, 0, tzinfo=UTC),
            ).pk,
            "mos": _index(
                backend,
                title="Months doc",
                content="x",
                checksum="unit-abbrev-mos",
                added=datetime(2026, 3, 15, 12, 0, tzinfo=UTC),
            ).pk,
            "wks": _index(
                backend,
                title="Weeks doc",
                content="x",
                checksum="unit-abbrev-wks",
                added=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            ).pk,
            "hrs": _index(
                backend,
                title="Hours doc",
                content="x",
                checksum="unit-abbrev-hrs",
                added=datetime(2026, 6, 15, 7, 0, tzinfo=UTC),
            ).pk,
            "mins": _index(
                backend,
                title="Minutes doc",
                content="x",
                checksum="unit-abbrev-mins",
                added=datetime(2026, 6, 15, 11, 50, tzinfo=UTC),
            ).pk,
            "secs": _index(
                backend,
                title="Seconds doc",
                content="x",
                checksum="unit-abbrev-secs",
                added=datetime(2026, 6, 15, 11, 59, 30, tzinfo=UTC),
            ).pk,
        }


class TestUnitAbbreviationsResolveToTheCorrectInstant:
    @pytest.mark.parametrize(
        ("query", "label"),
        [
            pytest.param('added:"-2yrs"', "yrs", id="years"),
            pytest.param('added:"-3mos"', "mos", id="months"),
            pytest.param('added:"-2wks"', "wks", id="weeks"),
            pytest.param('added:"-5hrs"', "hrs", id="hours"),
            pytest.param('added:"-10mins"', "mins", id="minutes"),
            pytest.param('added:"-30secs"', "secs", id="seconds"),
        ],
    )
    def test_quoted_abbreviation_matches_only_its_own_instant(
        self,
        backend: TantivyBackend,
        unit_documents: dict[str, int],
        query: str,
        label: str,
    ) -> None:
        with time_machine.travel(FROZEN_NOW, tick=False):
            assert _matched_ids(backend, query) == {unit_documents[label]}

    @pytest.mark.parametrize(
        ("query", "label"),
        [
            pytest.param("added:-2yrs", "yrs", id="years"),
            pytest.param("added:-3mos", "mos", id="months"),
            pytest.param("added:-2wks", "wks", id="weeks"),
            pytest.param("added:-5hrs", "hrs", id="hours"),
            pytest.param("added:-10mins", "mins", id="minutes"),
            pytest.param("added:-30secs", "secs", id="seconds"),
        ],
    )
    def test_bare_unquoted_abbreviation_matches_only_its_own_instant(
        self,
        backend: TantivyBackend,
        unit_documents: dict[str, int],
        query: str,
        label: str,
    ) -> None:
        with time_machine.travel(FROZEN_NOW, tick=False):
            assert _matched_ids(backend, query) == {unit_documents[label]}
