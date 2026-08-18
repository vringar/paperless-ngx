from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Final

import regex
import tantivy
import whoosh_compat as wc
from django.conf import settings
from whoosh_compat.emitters.tantivy_ import emit as tantivy_emit
from whoosh_compat.errors import Diagnostic
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import UnsupportedQueryError

from documents.search._registry import get_field_registry
from documents.search._tokenizer import simple_search_tokens

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Sequence
    from datetime import tzinfo

    from django.contrib.auth.base_user import AbstractBaseUser


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


logger = logging.getLogger("paperless.search")

# Maximum seconds any single regex substitution may run.
# Prevents ReDoS on adversarial user-supplied query strings.
_REGEX_TIMEOUT: Final[float] = 1.0

# Matches CJK/Hangul characters so queries can be routed to bigram fields.
# Uses Unicode properties to cover all blocks including Extension B+ planes.
_CJK_RE: Final = regex.compile(r"[\p{Han}\p{Hiragana}\p{Katakana}\p{Hangul}]+")


def _has_cjk(text: str) -> bool:
    """Return True if text contains any CJK characters."""
    return bool(_CJK_RE.search(text))


def extract_cjk_text(text: str) -> str:
    """Join the CJK runs in ``text`` for indexing into bigram (char-ngram) fields.

    Mirrors the query side (``_build_cjk_query``): only CJK runs are ever searched
    against the bigram fields, so only CJK runs are worth indexing there. Latin
    text fed to a character-bigram field is never matched and only bloats the
    index and slows indexing/merge. Returns "" when there is no CJK text.
    """
    return " ".join(_CJK_RE.findall(text))


def _build_cjk_query(
    index: tantivy.Index,
    raw_query: str,
    fields: list[str],
) -> tantivy.Query | None:
    """Build a bigram-field query from the CJK runs in ``raw_query``.

    Only the CJK character runs are extracted and parsed; ASCII field prefixes,
    boolean operators and date keywords are discarded. This keeps the CJK clause
    plain-text and consistent across query/simple modes (no leaked ``field:``
    semantics, no parse failures from spaced ``-``/``+``), and avoids feeding
    Latin tokens into the character-bigram matcher (which would produce spurious
    matches against unrelated Latin text). Returns None when there is no CJK
    text or the parse fails.
    """
    cjk_text = " ".join(_CJK_RE.findall(raw_query))
    if not cjk_text:
        return None
    try:
        return index.parse_query(cjk_text, fields)
    except Exception:
        return None


def _try_parse_fuzzy_query(
    index: tantivy.Index,
    raw_query: str,
) -> tantivy.Query | None:
    """Build the fuzzy blend clause from ``raw_query``, or None if it can't.

    The fuzzy blend hands ``raw_query`` directly to tantivy's own query
    parser (there's no clean AST-level fuzzy equivalent to whoosh-compat's
    parse tree, and fuzzy matching was always an approximate, secondary,
    0.1-boosted clause). But raw_query is whoosh grammar, not tantivy
    grammar: it can contain date keywords (``today``), whoosh ranges
    (``[2005 to 2009]``), or bracket-class wildcards (``202[0-1]*``) that
    tantivy's parser rejects with a ValueError. Rather than let that escape
    parse_user_query and fail the query's EXACT clause too (see paperless-
    ngx's whoosh-compat migration regression), degrade gracefully: skip the
    fuzzy clause and keep the exact/CJK clauses. Only ValueError is caught
    — a broad except here would also hide real bugs.
    """
    try:
        return index.parse_query(
            raw_query,
            DEFAULT_SEARCH_FIELDS,
            field_boosts=_FIELD_BOOSTS,
            fuzzy_fields={f: (True, 1, True) for f in DEFAULT_SEARCH_FIELDS},
        )
    except ValueError:
        logger.debug(
            "Skipping fuzzy search clause: raw query is not valid tantivy "
            "query syntax: %r",
            raw_query,
        )
        return None


def build_permission_filter(
    schema: tantivy.Schema,
    user: AbstractBaseUser,
    viewer_group_ids: Iterable[int] = (),
) -> tantivy.Query:
    """
    Build a query filter for user document permissions.

    Creates a query that matches only documents visible to the specified user
    according to paperless-ngx permission rules:
    - Public documents (no owner) are visible to all users
    - Private documents are visible to their owner
    - Documents explicitly shared with the user are visible
    - Documents shared with one of the user's current groups are visible

    Args:
        schema: Tantivy schema for field validation
        user: User to check permissions for
        viewer_group_ids: Current group memberships for the user

    Returns:
        Tantivy query that filters results to visible documents
    """
    owner_any = tantivy.Query.exists_query("owner_id")
    no_owner = tantivy.Query.boolean_query(
        [
            (tantivy.Occur.Must, tantivy.Query.all_query()),
            (tantivy.Occur.MustNot, owner_any),
        ],
    )
    owned = tantivy.Query.term_query(schema, "owner_id", user.pk)
    shared = tantivy.Query.term_query(schema, "viewer_id", user.pk)
    group_shared = [
        tantivy.Query.term_query(schema, "viewer_group_id", group_id)
        for group_id in viewer_group_ids
    ]
    return tantivy.Query.disjunction_max_query(
        [no_owner, owned, shared, *group_shared],
    )


DEFAULT_SEARCH_FIELDS = [
    "title",
    "content",
    "correspondent",
    "document_type",
    "tag",
]
SIMPLE_SEARCH_FIELDS = ["simple_title", "simple_content"]
TITLE_SEARCH_FIELDS = ["simple_title"]
_CJK_ALL_FIELDS: Final[list[str]] = [
    "bigram_content",
    "bigram_title",
    "bigram_correspondent",
    "bigram_document_type",
    "bigram_tag",
]
_CJK_CONTENT_FIELDS: Final[list[str]] = ["bigram_content"]
_CJK_TITLE_FIELDS: Final[list[str]] = ["bigram_title"]
_FIELD_BOOSTS = {"title": 2.0}
_SIMPLE_FIELD_BOOSTS = {"simple_title": 2.0}


def _simple_query_tokens(raw_query: str) -> list[str]:
    # Tokenize and fold via the same analyzer used to index simple_title /
    # simple_content, so query terms fold identically to the indexed terms
    # (single source of truth for ASCII folding).
    return simple_search_tokens(raw_query)


def _build_simple_token_query(
    index: tantivy.Index,
    fields: list[str],
    token: str,
    *,
    allow_infix: bool,
) -> tantivy.Query:
    escaped = regex.escape(token)
    # The simple analyzer keeps punctuation inside whitespace-delimited terms.
    # Boundary-constrained query tokens may therefore begin either at the indexed
    # term boundary or after punctuation within a term (for example,
    # ``medical-history``). This avoids matching a numeric token such as ``6``
    # in the middle of ``16``.
    pattern = (
        f".*{escaped}.*"
        if allow_infix
        else (
            f"({escaped}.*|"
            rf".*[\x20-\x2f\x3a-\x40\x5b-\x60\x7b-\x7e]{escaped}.*)"
        )
    )
    field_queries: list[tuple[tantivy.Occur, tantivy.Query]] = []
    for field in fields:
        query = tantivy.Query.regex_query(index.schema, field, pattern)
        boost = _SIMPLE_FIELD_BOOSTS.get(field, 1.0)
        if boost > 1.0:
            query = tantivy.Query.boost_query(query, boost)
        field_queries.append((tantivy.Occur.Should, query))

    if len(field_queries) == 1:
        return field_queries[0][1]
    return tantivy.Query.boolean_query(field_queries)


def parse_user_query(
    index: tantivy.Index,
    raw_query: str,
    tz: tzinfo,
) -> tantivy.Query:
    """
    Parse user query through whoosh-compat, then blend in fuzzy/CJK clauses.

    1. wc.parse() against the shared FieldRegistry (whoosh grammar -> AST).
    2. Any diagnostics (bad dates/numbers) map to SearchQueryError subclasses
       and raise — the view returns HTTP 400 with every offending field
       listed, not just the first.
    3. emit() turns the AST into a tantivy.Query directly (no string
       round-trip). UnsupportedQueryError (a construct that parses but can't
       execute against tantivy, e.g. a text-field range) also maps to a 400.
    4. Optional fuzzy blend (ADVANCED_FUZZY_SEARCH_THRESHOLD) re-parses
       raw_query directly via index.parse_query — there's no clean AST-level
       fuzzy equivalent, and fuzzy matching was always an approximate,
       secondary clause. raw_query still carries whoosh grammar (date
       keywords, bracket-class wildcards, etc.) that tantivy's own parser
       cannot parse; when that happens the fuzzy clause is skipped rather
       than letting the ValueError escape and fail the whole query (see
       _try_parse_fuzzy_query).
    5. Optional CJK bigram clause — unchanged from before this migration,
       never went through the pre-whoosh-compat translation layer either.
    """
    registry = get_field_registry(settings.SEARCH_LANGUAGE)
    result = wc.parse(
        raw_query,
        registry=registry,
        default_fields=DEFAULT_SEARCH_FIELDS,
        field_boosts=_FIELD_BOOSTS,
        tz=tz,
    )
    if result.diagnostics:
        raise _diagnostics_to_error(result.diagnostics)

    try:
        exact = tantivy_emit(result.ast, index=index, registry=registry)
    except UnsupportedQueryError as e:
        raise SearchQueryError(str(e)) from e

    cjk_query = (
        _build_cjk_query(index, raw_query, _CJK_ALL_FIELDS)
        if _has_cjk(raw_query)
        else None
    )

    clauses: list[tuple[tantivy.Occur, tantivy.Query]] = [
        (tantivy.Occur.Should, exact),
    ]

    threshold = settings.ADVANCED_FUZZY_SEARCH_THRESHOLD
    if threshold is not None:
        fuzzy = _try_parse_fuzzy_query(index, raw_query)
        if fuzzy is not None:
            clauses.append(
                (tantivy.Occur.Should, tantivy.Query.boost_query(fuzzy, 0.1)),
            )

    if cjk_query is not None:
        clauses.append((tantivy.Occur.Should, cjk_query))

    if len(clauses) == 1:
        return exact
    return tantivy.Query.boolean_query(clauses)


def _diagnostics_to_error(diagnostics: tuple[Diagnostic, ...]) -> SearchQueryError:
    errors = [_single_diagnostic_to_error(d) for d in diagnostics]
    return errors[0] if len(errors) == 1 else MultipleSearchQueryErrors(errors)


def _single_diagnostic_to_error(d: Diagnostic) -> SearchQueryError:
    # d.field is a FieldRef, not a str: str(d.field) gives the canonical
    # dotted name (an aliased query, e.g. type:, reports document_type).
    field_name = str(d.field) if d.field is not None else None
    if d.kind is DiagnosticKind.BAD_DATE:
        return InvalidDateQuery(field_name, d.raw_value)
    if d.kind is DiagnosticKind.BAD_NUMBER:
        return InvalidNumberQuery(field_name, d.raw_value)
    # TOO_DEEP and UNSUPPORTED_PATTERN (e.g. a wildcard on asn/page_count/
    # num_notes, or on a custom_fields.*/notes.* subpath) fall through to
    # the generic message; consider whether either warrants its own typed
    # subclass if callers ever need to distinguish them programmatically.
    return SearchQueryError(d.message)


def parse_simple_query(
    index: tantivy.Index,
    raw_query: str,
    fields: list[str],
    cjk_fields: list[str] | None = None,
) -> tantivy.Query:
    """
    Parse a plain-text query using Tantivy over a restricted field set.

    Query string is escaped and normalized to be treated as "simple" text query.
    When cjk_fields is provided and the query contains CJK characters, an
    additional Should clause searches those bigram-tokenized fields, which match
    CJK substrings the simple analyzer can't (long whitespace-free runs are
    dropped by remove_long).
    """
    tokens = _simple_query_tokens(raw_query)

    clauses: list[tuple[tantivy.Occur, tantivy.Query]] = []
    if tokens:
        # Match every query token, regardless of its position in the document.
        # Each token may occur in any of the requested fields, so text mode also
        # finds documents whose matches are split between title and content.
        token_queries = [
            (
                tantivy.Occur.Must,
                _build_simple_token_query(
                    index,
                    fields,
                    token,
                    # Preserve historical infix matching for single-token
                    # searches. In multi-token searches, constrain numeric
                    # tokens to boundaries to avoid partial-number overlap.
                    # This depends on token content, not query order.
                    allow_infix=len(tokens) == 1 or not token.isdecimal(),
                ),
            )
            for token in tokens
        ]
        simple_query = (
            token_queries[0][1]
            if len(token_queries) == 1
            else tantivy.Query.boolean_query(token_queries)
        )
        clauses.append((tantivy.Occur.Should, simple_query))

    if cjk_fields and _has_cjk(raw_query):
        cjk_q = _build_cjk_query(index, raw_query, cjk_fields)
        if cjk_q is not None:
            clauses.append((tantivy.Occur.Should, cjk_q))

    if not clauses:
        return tantivy.Query.empty_query()
    if len(clauses) == 1:
        return clauses[0][1]
    return tantivy.Query.boolean_query(clauses)


def parse_simple_text_highlight_query(
    index: tantivy.Index,
    raw_query: str,
) -> tantivy.Query:
    """Build a snippet-friendly query for simple text searches.

    Simple search matching uses regex queries but for compatibility with Tantivy
    SnippetGenerator we build a plain term query over the content field instead.
    """

    # Strip Tantivy operator chars before tokenizing: this is a plain-text
    # highlight query, not a structured boolean query, so +/- are separators.
    tokens = _simple_query_tokens(
        regex.sub(r"[-+]", " ", raw_query, timeout=_REGEX_TIMEOUT),
    )
    if not tokens:
        return tantivy.Query.empty_query()

    return index.parse_query(" ".join(tokens), ["content"])


def parse_simple_text_query(
    index: tantivy.Index,
    raw_query: str,
) -> tantivy.Query:
    """
    Parse a plain-text query over title/content for simple search inputs.
    """

    return parse_simple_query(
        index,
        raw_query,
        SIMPLE_SEARCH_FIELDS,
        cjk_fields=_CJK_CONTENT_FIELDS,
    )


def parse_simple_title_query(
    index: tantivy.Index,
    raw_query: str,
) -> tantivy.Query:
    """
    Parse a plain-text query over the title field only.
    """

    return parse_simple_query(
        index,
        raw_query,
        TITLE_SEARCH_FIELDS,
        cjk_fields=_CJK_TITLE_FIELDS,
    )
