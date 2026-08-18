from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


class SearchQueryError(ValueError):
    """
    Base for user-fixable search query errors.

    Carries a message safe to surface to the user (no internal details). The
    view layer catches this and returns an HTTP 400, so any future subclass
    gets the same treatment.
    """


class InvalidDateQuery(SearchQueryError):
    """Raised when a date field value or range bound cannot be parsed."""

    def __init__(self, field: str | None, value: str | None) -> None:
        self.field = field
        self.value = value
        super().__init__(f"Invalid date value {value!r} for field {field!r}.")


class InvalidNumberQuery(SearchQueryError):
    """Raised when a numeric field value or range bound cannot be parsed."""

    def __init__(self, field: str | None, value: str | None) -> None:
        self.field = field
        self.value = value
        super().__init__(f"Invalid numeric value {value!r} for field {field!r}.")


class MultipleSearchQueryErrors(SearchQueryError):
    """Aggregates every user-fixable error from one parse, not just the first."""

    def __init__(self, errors: Sequence[SearchQueryError]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(str(e) for e in self.errors))


def search_query_error_messages(e: SearchQueryError) -> list[str]:
    """The user-facing message list for a SearchQueryError.

    Every offending value's message, not just the first, so the user can
    fix them all in one round-trip. Shared by every view that maps
    SearchQueryError to an HTTP 400.
    """
    if isinstance(e, MultipleSearchQueryErrors):
        return [str(sub) for sub in e.errors]
    return [str(e)]
