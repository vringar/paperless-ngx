"""Whoosh's compact, separator-free date spellings: ``20050304`` and
``20050304153000``.

Both were asserted before the whoosh-compat migration (the old
``test_8digit_created_date_field_always_uses_utc_midnight`` and
``test_14digit_compact_datetime``) and by the deleted ``_translate.py``'s own
suite. Afterwards the 8-digit form survived only incidentally, in one
pre-existing API test, and the 14-digit form was asserted nowhere -- the
regression class where a spelling silently stops matching with nothing to
notice.

The two forms differ in width, not just in length: 8 digits is a calendar-day
window, 14 digits a single instant. The corpus separates them, so a form that
degrades into the other one -- or into a non-match -- fails rather than passing
on the one document that would match either way.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
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


@pytest.fixture
def docs(backend: TantivyBackend) -> dict[str, int]:
    return {
        "instant": _index(
            backend,
            title="On the instant",
            content="x",
            checksum="compact-date-instant",
            added=datetime(2005, 3, 4, 15, 30, tzinfo=UTC),
        ).pk,
        "same_day": _index(
            backend,
            title="Same day, other hour",
            content="x",
            checksum="compact-date-same-day",
            added=datetime(2005, 3, 4, 9, 0, tzinfo=UTC),
        ).pk,
        "next_day": _index(
            backend,
            title="Next day, same hour",
            content="x",
            checksum="compact-date-next-day",
            added=datetime(2005, 3, 5, 15, 30, tzinfo=UTC),
        ).pk,
    }


class TestCompactDateForms:
    def test_eight_digits_is_a_calendar_day_window(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        assert _matched_ids(backend, "added:20050304") == {
            docs["instant"],
            docs["same_day"],
        }

    def test_eight_digits_agrees_with_the_hyphenated_spelling(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        assert _matched_ids(backend, "added:20050304") == _matched_ids(
            backend,
            "added:2005-03-04",
        )

    def test_fourteen_digits_is_a_single_instant(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        # same_day is what tells this apart from the 8-digit form, next_day
        # from a form that ignored the time altogether.
        assert _matched_ids(backend, "added:20050304153000") == {docs["instant"]}

    def test_fourteen_digits_addresses_the_hour_it_names(
        self,
        backend: TantivyBackend,
        docs: dict[str, int],
    ) -> None:
        assert _matched_ids(backend, "added:20050304090000") == {docs["same_day"]}
        assert _matched_ids(backend, "added:20050305153000") == {docs["next_day"]}
