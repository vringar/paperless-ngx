"""One relative unit abbreviation, resolved end to end against the index.

whoosh-compat owns which unit words exist and what offset each resolves to,
and asserts all six (yrs/mos/wks/hrs/mins/secs, quoted and bare) directly in
``tests/test_relative_date_unit_abbreviations.py``. Repeating that matrix here
would only re-prove the library's grammar; "hrs" is kept as a single
representative so that the paperless-side path the library cannot see -- the
``added`` DATETIME fast field, the timezone the query is resolved in and the
range the backend emits -- is still exercised by an abbreviation-spelled
offset rather than only by absolute dates.

The offset is sub-day on purpose: a whole-day or whole-year offset survives a
wrong timezone, five hours does not. The decoy document sits at a different
offset from the same instant, so an offset that resolves to the wrong width
lands on nothing or on the decoy instead of passing by accident.
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
    """One document five hours before the frozen instant, one ten minutes
    before it, both indexed together so a mis-resolved offset lands on the
    wrong document or on none rather than passing coincidentally."""
    with time_machine.travel(FROZEN_NOW, tick=False):
        return {
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
        }


def test_hours_abbreviation_matches_only_its_own_instant(
    backend: TantivyBackend,
    unit_documents: dict[str, int],
) -> None:
    with time_machine.travel(FROZEN_NOW, tick=False):
        assert _matched_ids(backend, 'added:"-5hrs"') == {unit_documents["hrs"]}
