from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from whoosh_compat import FieldKind
from whoosh_compat import FieldRegistry

from documents.search._fields import PUBLIC_FIELDS
from documents.search._tokenizer import ascii_fold
from documents.search._tokenizer import paperless_text_analyzer
from documents.search._tokenizer import stem_pattern_text

if TYPE_CHECKING:
    from collections.abc import Callable

_registry_cache: dict[str | None, FieldRegistry] = {}


def _identity_analyzer(text: str) -> list[str]:
    """Analyzer for KEYWORD fields indexed with the raw tokenizer (no splitting)."""
    return [text]


def _make_pattern_normalizer(language: str | None) -> Callable[[str], str]:
    """Build the wildcard/regex literal-run normalizer for a search language."""

    def _pattern_normalizer(text: str) -> str:
        """Normalize a literal run so it can match indexed terms.

        Index terms go through lowercase -> ascii_fold -> stem, so a pattern
        that skips stemming can never match one: "invoice*" would look for a
        term starting with "invoice" while the index holds "invoic". The run is
        therefore stemmed here too.

        A stem can be longer than the fragment the user typed, though, and a
        longer prefix matches nothing, so the stem is used only when it is no
        longer than the typed run. Length is a proxy for "the stem stayed close
        to what was typed", not a guarantee of wider recall: a stem that
        substitutes rather than truncates ("copy" -> "copi") moves the pattern
        sideways instead of widening it, so "copy*" gains "copies" and loses
        "copyright". test_pattern_stemming.py pins that trade.
        """
        folded = ascii_fold(text.lower())
        stemmed = stem_pattern_text(folded, language)
        return folded if len(stemmed) > len(folded) else stemmed

    return _pattern_normalizer


def get_field_registry(language: str | None) -> FieldRegistry:
    """Build (or return the cached) FieldRegistry for the given search language.

    Cached keyed by language, rebuilt on the same trigger register_tokenizers()
    uses (settings.SEARCH_LANGUAGE change) — a fresh call with a new language
    builds and caches a new registry rather than mutating the old one.
    """
    if language in _registry_cache:
        return _registry_cache[language]

    text_analyzer = paperless_text_analyzer(language).analyze
    pattern_normalizer = _make_pattern_normalizer(language)

    specs = [
        dataclasses.replace(
            field,
            analyzer=_identity_analyzer
            if field.kind is FieldKind.KEYWORD
            else text_analyzer,
            pattern_normalizer=pattern_normalizer,
        )
        for field in PUBLIC_FIELDS
    ]

    registry = FieldRegistry(specs)
    _registry_cache[language] = registry
    return registry
