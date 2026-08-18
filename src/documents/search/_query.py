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
from whoosh_compat.errors import QueryEmitError
from whoosh_compat.errors import UnsupportedQueryError

from documents.search._fields import PUBLIC_FIELDS
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


def search_query_error_messages(e: SearchQueryError) -> list[str]:
    """The user-facing message list for a SearchQueryError.

    Every offending value's message, not just the first, so the user can
    fix them all in one round-trip. Shared by every view that maps
    SearchQueryError to an HTTP 400.
    """
    if isinstance(e, MultipleSearchQueryErrors):
        return [str(sub) for sub in e.errors]
    return [str(e)]


logger = logging.getLogger("paperless.search")

# Maximum seconds any single regex substitution may run.
# Prevents ReDoS on adversarial user-supplied query strings.
_REGEX_TIMEOUT: Final[float] = 1.0

# Matches CJK/Hangul characters so queries can be routed to bigram fields.
# Uses Unicode properties to cover all blocks including Extension B+ planes.
_CJK_RE: Final = regex.compile(r"[\p{Han}\p{Hiragana}\p{Katakana}\p{Hangul}]+")

# The closed multi-word date-keyword vocabulary, unchanged since paperless
# v2's rewrite_natural_date_keywords. whoosh-compat's date grammar
# understands every one of these natively, but only as a QUOTED value
# (its DIVERGENCES.md entry 19: unquoted multi-word values split at
# whitespace, faithfully to whoosh); the unquoted spelling has been
# honored continuously since the whoosh era by an app-level assist, so
# _quote_date_keyword_phrases below keeps honoring it by inserting the
# quotes and nothing else. Single-word keywords (today, yesterday) parse
# unquoted already and need no entry.
_DATE_KEYWORD_PHRASES: Final = (
    "previous week",
    "previous month",
    "previous quarter",
    "previous year",
    "this month",
    "this year",
)

# Field names are case-sensitive (matching the parser's own field
# tagging); the keyword phrase is case-insensitive (matching the date
# grammar's leniency for the quoted form). Date fields derived from
# PUBLIC_FIELDS, never hand-listed.
_DATE_KEYWORD_PHRASE_RE: Final = regex.compile(
    r"\b("
    + "|".join(
        regex.escape(f.name)
        for f in PUBLIC_FIELDS
        if f.kind in (wc.FieldKind.DATE, wc.FieldKind.DATETIME)
    )
    + r"):((?i:"
    + "|".join(_DATE_KEYWORD_PHRASES)
    + r"))\b",
)


def _quote_date_keyword_phrases(raw_query: str) -> str:
    """Quote unquoted multi-word date keyword phrases on date fields.

    ``added:previous month`` becomes ``added:"previous month"``; the
    already-quoted spellings don't match the pattern (the colon must be
    followed directly by the phrase), and the same words after a TEXT
    field or standing alone are ordinary text and untouched. Only quoting
    happens here: every date computation stays in whoosh-compat's
    grammar, which parses exactly this phrase vocabulary as quoted
    values. This is deliberately NOT a revival of the deleted
    translation layer, which computed the ranges app-side.
    """
    return _DATE_KEYWORD_PHRASE_RE.sub(
        r'\1:"\2"',
        raw_query,
        timeout=_REGEX_TIMEOUT,
    )


# The v2 whoosh schema had plural notes/custom_fields TEXT fields (notes
# indexed the joined note texts; custom_fields indexed joined
# "name : value" strings), so the bare plural prefixes were valid fielded
# searches in released paperless and at the deleted translation layer. On
# the whoosh-compat registry they are JSON fields addressable only via
# subpaths, and the bare spelling would demote to an unfielded text search
# of the words themselves. Rewrite the prefixes live to the same targets
# migration 0017 chose for the singular whoosh-era spellings (note: ->
# notes.note:, custom_field: -> custom_fields.value:), values untouched.
# Trade-off inherited from that migration: custom_fields.value: drops the
# name-matching half of v2's "name : value" indexing (custom_fields.name:
# remains available for it). Same lookbehind guard as 0017: not preceded
# by a word character or dot, so subpath spellings and words that merely
# end in the prefix are untouched.
_BARE_JSON_PREFIX_RES: Final = (
    (regex.compile(r"(?<![.\w])notes:(?!\.)"), "notes.note:"),
    (regex.compile(r"(?<![.\w])custom_fields:(?!\.)"), "custom_fields.value:"),
)


def _rewrite_bare_json_field_prefixes(raw_query: str) -> str:
    """Rewrite bare ``notes:``/``custom_fields:`` prefixes to their
    subpath equivalents. Prefix substitution only, values untouched."""
    for pattern, replacement in _BARE_JSON_PREFIX_RES:
        raw_query = pattern.sub(replacement, raw_query, timeout=_REGEX_TIMEOUT)
    return raw_query


# whoosh-compat's emit() error messages are written for the HOST: they
# cite the library's own divergence ledger and give registry-configuration
# advice. Neither belongs in a message shown to a searching user.
_DIVERGENCE_REF_RE: Final = regex.compile(r"\s*\(DIVERGENCES\.md entry \d+\)")


def _user_facing_emit_message(exc: Exception) -> str:
    """A user-safe message for a QueryEmitError/UnsupportedQueryError."""
    message = _DIVERGENCE_REF_RE.sub("", str(exc))
    if "fast=True" in message:
        # The exists-check message advises marking the field fast=True, a
        # host configuration action; the user just needs to know the
        # search form is unsupported here.
        return "existence searches (field:*) are not supported for this field"
    return message


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


# A joined fuzzy word string must stay plain words: any token that could
# read as tantivy query grammar (a colon, bracket, quote, operator...) is
# dropped rather than escaped. Today's default-field analyzers only emit
# word characters, so this never fires; it guards a future field whose
# analyzer passes punctuation through (an identity/keyword analyzer).
_WORD_TOKEN_RE = regex.compile(r"\w+")


def _try_parse_fuzzy_query(
    index: tantivy.Index,
    ast: wc.ast.Node,
    registry: wc.FieldRegistry,
) -> tantivy.Query | None:
    """Build the fuzzy blend clause from the parsed query's free-text
    words, or None if it has none.

    The clause is built by handing tantivy's own query parser a plain
    word string (there's no clean AST-level fuzzy equivalent to
    whoosh-compat's parse tree, and fuzzy matching was always an
    approximate, secondary, 0.1-boosted clause). The words come from
    whoosh_compat's ``free_text_tokens`` over the already-parsed AST,
    never from the raw query string: raw whoosh grammar (date keywords,
    ``[2005 to 2009]`` ranges, bracket-class wildcards) is not tantivy
    syntax, and feeding it here used to knock the fuzzy clause out for
    the whole query the moment any such construct appeared alongside a
    typo'd word. The helper also keeps excluded terms out: a ``NOT``'d
    word must not resurface through the fuzzy clause.

    Chosen trade-off: a term explicitly fielded on one of the default
    search fields (``correspondent:acme``) contributes its text to the
    word string UNFIELDED, so the fuzzy clause searches it across all
    default fields rather than just the one the user named. That is
    recall-only widening on a secondary 0.1-boosted clause the score
    threshold already disciplines, accepted in exchange for never feeding
    field syntax to tantivy's parser.

    The ValueError guard stays as insurance (the word string is plain
    tokens, so tantivy accepting it is expected, not assumed): on a parse
    failure the fuzzy clause is skipped and the exact/CJK clauses stand,
    rather than the whole query failing.
    """
    tokens = wc.free_text_tokens(ast, registry=registry, fields=DEFAULT_SEARCH_FIELDS)
    words = [t for t in tokens if _WORD_TOKEN_RE.fullmatch(t)]
    if not words:
        return None
    fuzzy_text = " ".join(words)
    try:
        return index.parse_query(
            fuzzy_text,
            DEFAULT_SEARCH_FIELDS,
            field_boosts=_FIELD_BOOSTS,
            fuzzy_fields={f: (True, 1, True) for f in DEFAULT_SEARCH_FIELDS},
        )
    except ValueError:
        logger.debug(
            "Skipping fuzzy search clause: token string is not valid "
            "tantivy query syntax: %r",
            fuzzy_text,
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

    1. Two small pre-parse rewrites keep historically honored spellings
       working: unquoted multi-word date keyword phrases on date fields
       are quoted (_quote_date_keyword_phrases), and bare
       notes:/custom_fields: prefixes become their subpath equivalents
       (_rewrite_bare_json_field_prefixes). Then wc.parse() against the
       shared FieldRegistry (whoosh grammar -> AST).
    2. Any diagnostics (bad dates/numbers) map to SearchQueryError subclasses
       and raise — the view returns HTTP 400 with every offending field
       listed, not just the first.
    3. emit() turns the AST into a tantivy.Query directly (no string
       round-trip). UnsupportedQueryError (a construct that parses but can't
       execute against tantivy, e.g. a text-field range) also maps to a 400.
    4. Optional fuzzy blend (ADVANCED_FUZZY_SEARCH_THRESHOLD) builds a
       plain word string from the parsed AST's free-text tokens
       (whoosh_compat.free_text_tokens) and feeds THAT to
       index.parse_query — never raw_query, whose whoosh grammar (date
       keywords, bracket-class wildcards, etc.) tantivy's parser rejects,
       which used to silently knock the fuzzy clause out of any mixed
       query (see _try_parse_fuzzy_query).
    5. Optional CJK bigram clause — unchanged from before this migration,
       never went through the pre-whoosh-compat translation layer either.
    """
    registry = get_field_registry(settings.SEARCH_LANGUAGE)
    raw_query = _quote_date_keyword_phrases(raw_query)
    raw_query = _rewrite_bare_json_field_prefixes(raw_query)
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
    except (QueryEmitError, UnsupportedQueryError) as e:
        # emit()'s documented host contract: BOTH of these are user-input
        # errors, exactly like a parse diagnostic, and both map to a 400.
        raise SearchQueryError(_user_facing_emit_message(e)) from e

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
        fuzzy = _try_parse_fuzzy_query(index, result.ast, registry)
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
