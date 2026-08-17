from __future__ import annotations

from whoosh_compat import FieldKind
from whoosh_compat import FieldRegistry
from whoosh_compat import FieldSpec

from documents.search._fields import PUBLIC_FIELDS
from documents.search._tokenizer import ascii_fold
from documents.search._tokenizer import paperless_text_analyzer

_registry_cache: dict[str | None, FieldRegistry] = {}


def _identity_analyzer(text: str) -> list[str]:
    """Analyzer for KEYWORD fields indexed with the raw tokenizer (no splitting)."""
    return [text]


def _pattern_normalizer(text: str) -> str:
    """Normalize wildcard/regex query patterns: lowercase -> ascii_fold.

    Mirrors the lowercase -> ascii_fold steps of the index-time analyzers
    (paperless_text) without stemming, so pattern queries (e.g. "run*")
    match tokens that were folded the same way at index time but are not
    run through a stemmer, which would corrupt wildcard/regex semantics.
    """
    return ascii_fold(text.lower())


def get_field_registry(language: str | None) -> FieldRegistry:
    """Build (or return the cached) FieldRegistry for the given search language.

    Cached keyed by language, rebuilt on the same trigger register_tokenizers()
    uses (settings.SEARCH_LANGUAGE change) — a fresh call with a new language
    builds and caches a new registry rather than mutating the old one.
    """
    if language in _registry_cache:
        return _registry_cache[language]

    text_analyzer = paperless_text_analyzer(language).analyze

    specs = []
    for field in PUBLIC_FIELDS:
        if field.kind is FieldKind.KEYWORD:
            analyzer = _identity_analyzer
        else:
            analyzer = text_analyzer
        specs.append(
            FieldSpec(
                name=field.name,
                kind=field.kind,
                aliases=field.aliases,
                comma_values=field.comma_values,
                analyzer=analyzer,
                pattern_normalizer=_pattern_normalizer,
                date_only=field.date_only,
                fast=field.fast,
                subpaths=field.subpaths,
            ),
        )

    registry = FieldRegistry(specs)
    _registry_cache[language] = registry
    return registry
