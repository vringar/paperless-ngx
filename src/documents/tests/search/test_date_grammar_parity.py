"""Transitional coverage audit: every date keyword/unit _dates.py and
_translate.py accept today must still parse cleanly (no diagnostics)
through whoosh-compat, before those modules are deleted (Task 14). This
test is deleted in the same task as the legacy code it audits — superseded by the permanent
result-level acceptance corpus (Task 12).
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

import pytest
import time_machine
import whoosh_compat as wc

from documents.search._dates import _DATE_KEYWORDS
from documents.search._registry import get_field_registry
from documents.search._translate import _UNIT_ALIASES

FROZEN_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def registry():
    return get_field_registry(None)


@pytest.mark.parametrize("keyword", sorted(_DATE_KEYWORDS))
def test_date_keyword_parses_without_diagnostics(keyword, registry) -> None:
    with time_machine.travel(FROZEN_NOW, tick=False):
        result = wc.parse(
            f"created:{keyword}" if " " not in keyword else f'created:"{keyword}"',
            registry=registry,
            default_fields=["content"],
            tz=UTC,
        )
    assert result.diagnostics == (), (
        f"{keyword!r} produced diagnostics: {result.diagnostics}"
    )


@pytest.mark.parametrize(
    ("unit_alias", "sign"),
    [(alias, sign) for alias in sorted(_UNIT_ALIASES) for sign in ("+", "-")],
)
def test_relative_offset_unit_parses_without_diagnostics(
    unit_alias,
    sign,
    registry,
) -> None:
    # Whoosh-era abbreviated units (yrs, mos, wks, hrs, mins, secs, etc.)
    # kept for saved-view back-compat — every key of _UNIT_ALIASES must still
    # parse under whoosh-compat's grammar.
    token = f"{sign}3 {unit_alias}"
    with time_machine.travel(FROZEN_NOW, tick=False):
        result = wc.parse(
            f'created:"{token}"',
            registry=registry,
            default_fields=["content"],
            tz=UTC,
        )
    assert result.diagnostics == (), (
        f"{token!r} produced diagnostics: {result.diagnostics}"
    )


@pytest.mark.parametrize(
    "token",
    ["now", "now-7d", "now+1h", "now-30m", "now+30m"],
)
def test_compact_now_offset_parses_without_diagnostics(token, registry) -> None:
    with time_machine.travel(FROZEN_NOW, tick=False):
        result = wc.parse(
            f"created:{token}",
            registry=registry,
            default_fields=["content"],
            tz=UTC,
        )
    assert result.diagnostics == (), (
        f"{token!r} produced diagnostics: {result.diagnostics}"
    )


@pytest.mark.parametrize(
    "digits",
    ["2020", "202006", "20200615"],
)
def test_digit_precision_forms_parse_without_diagnostics(digits, registry) -> None:
    with time_machine.travel(FROZEN_NOW, tick=False):
        result = wc.parse(
            f"created:{digits}",
            registry=registry,
            default_fields=["content"],
            tz=UTC,
        )
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    "iso",
    ["2020", "2020-06", "2020-06-15"],
)
def test_iso_dash_forms_parse_without_diagnostics(iso, registry) -> None:
    with time_machine.travel(FROZEN_NOW, tick=False):
        result = wc.parse(
            f"created:{iso}",
            registry=registry,
            default_fields=["content"],
            tz=UTC,
        )
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    "range_query",
    [
        "created:[2020 TO 2025]",
        "created:[2020 TO]",
        "created:[TO 2025]",
        "created:[2025 TO 2020]",  # reversed — legacy code swaps bounds
    ],
)
def test_range_forms_parse_without_diagnostics(range_query, registry) -> None:
    with time_machine.travel(FROZEN_NOW, tick=False):
        result = wc.parse(
            range_query,
            registry=registry,
            default_fields=["content"],
            tz=UTC,
        )
    assert result.diagnostics == (), f"{range_query!r}: {result.diagnostics}"
