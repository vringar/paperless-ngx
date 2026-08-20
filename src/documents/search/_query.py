from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Final

import regex
import tantivy
import whoosh_compat as wc
from django.conf import settings
from whoosh_compat.emitters.tantivy_ import emit as tantivy_emit
from whoosh_compat.errors import Cause
from whoosh_compat.errors import Diagnostic
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import QueryError

from documents.search._errors import InvalidDateQuery
from documents.search._errors import InvalidNumberQuery
from documents.search._errors import MultipleSearchQueryErrors
from documents.search._errors import SearchQueryError
from documents.search._registry import get_field_registry
from documents.search._tokenizer import simple_search_tokens

if TYPE_CHECKING:
    from datetime import tzinfo

logger = logging.getLogger("paperless.search")

# Maximum seconds any single regex substitution may run.
# Prevents ReDoS on adversarial user-supplied query strings.
_REGEX_TIMEOUT: Final[float] = 1.0

# Matches CJK/Hangul characters so queries can be routed to bigram fields.
# Uses Unicode properties to cover all blocks including Extension B+ planes.
_CJK_RE: Final = regex.compile(r"[\p{Han}\p{Hiragana}\p{Katakana}\p{Hangul}]+")


def _user_facing_emit_message(d: Diagnostic) -> str:
    """A user-safe message for an emit-time QueryError's Diagnostic.

    Built from the Diagnostic's structured fields (kind, field), never from
    d.message: whoosh-compat documents that as developer/log output with no
    stability guarantee, and PATTERN_TOO_COMPLEX embeds the raw backend
    error text in it.
    """
    field = str(d.field) if d.field is not None else None
    if d.kind is DiagnosticKind.EXISTS_REQUIRES_FAST:
        return f"Existence searches (field:*) are not supported for field {field!r}."
    if d.kind is DiagnosticKind.TEXT_RANGE:
        return f"Range searches are not supported for field {field!r}."
    if d.kind is DiagnosticKind.PATTERN_TOO_COMPLEX:
        return f"The wildcard pattern for field {field!r} is too complex."
    if d.kind is DiagnosticKind.SCHEMA_FIELD_MISSING:
        return f"Field {field!r} is not available in the search index."
    logger.warning("Unmapped emit diagnostic %s: %s", d.kind, d.message)
    return "The search query could not be executed."


def _map_emit_error(e: QueryError) -> SearchQueryError:
    """Route an emit-time QueryError by its Diagnostic's Cause.

    INVALID_INPUT/UNSUPPORTED are user-input errors, exactly like a parse
    diagnostic, and map to a 400. INTERNAL means a defect in whoosh-compat
    or in our own AST handling, never the user's query, so the QueryError is
    re-raised to surface the same way views.py already lets QueryParserError
    surface. MISCONFIGURED is deliberately both: the registry and the index
    schema disagree, which only an operator can fix, so it is logged as an
    error, but a request is still waiting and the query cannot run either
    way, so it also returns a 400.
    """
    d = e.diagnostic
    if d.cause is Cause.INTERNAL:
        raise e
    if d.cause is Cause.MISCONFIGURED:
        logger.error(
            "Search index misconfiguration for field %s (%s): %s",
            d.field,
            d.kind.name,
            d.message,
        )
    return SearchQueryError(_user_facing_emit_message(d))


def _has_cjk(text: str) -> bool:
    """Return True if text contains any CJK characters."""
    return bool(_CJK_RE.search(text))


def extract_cjk_text(text: str) -> str:
    """Join the CJK runs in ``text`` for indexing into bigram (char-ngram) fields.

    Mirrors the query side, which extracts the CJK runs of whatever it is
    about to search for (the raw string in simple modes, the parsed query's
    free-text tokens in query mode): only CJK runs are ever searched against
    the bigram fields, so only CJK runs are worth indexing there. Latin text
    fed to a character-bigram field is never matched and only bloats the
    index and slows indexing/merge. Returns "" when there is no CJK text.
    """
    return " ".join(_CJK_RE.findall(text))


def _parse_cjk_text(
    index: tantivy.Index,
    cjk_text: str,
    fields: list[str],
) -> tantivy.Query | None:
    """Parse a plain CJK run string against ``fields``, or None if it won't parse."""
    try:
        return index.parse_query(cjk_text, fields)
    except Exception:
        # Broad on purpose, unlike _try_parse_fuzzy_query's narrower
        # ValueError: cjk_text isn't filtered to a guaranteed-safe token
        # set the way the fuzzy blend's word string is, so the exact
        # failure mode tantivy could raise here isn't pinned down.
        logger.debug(
            "Skipping CJK search clause: could not parse CJK text: %r",
            cjk_text,
        )
        return None


def _build_cjk_query(
    index: tantivy.Index,
    raw_query: str,
    fields: list[str],
) -> tantivy.Query | None:
    """Build a bigram-field query from the CJK runs in ``raw_query``.

    For the simple (TEXT/TITLE) modes, whose input is plain text and carries
    no query grammar to respect. Only the CJK character runs are extracted, so
    a stray ``field:`` prefix or ``-``/``+`` in the input can neither leak
    field semantics nor fail the parse, and no Latin token reaches the
    character-bigram matcher (where it would produce spurious matches against
    unrelated Latin text). Returns None when there is no CJK text or the parse
    fails.
    """
    cjk_text = extract_cjk_text(raw_query)
    if not cjk_text:
        return None
    return _parse_cjk_text(index, cjk_text, fields)


def _build_ast_cjk_query(
    index: tantivy.Index,
    ast: wc.ast.Node,
    registry: wc.FieldRegistry,
) -> tantivy.Query | None:
    """Build the bigram clause of a QUERY-mode search from the parsed AST.

    Same discipline as the fuzzy clause (see _try_parse_fuzzy_query): the CJK
    runs come from whoosh_compat's ``free_text_tokens`` over the parsed tree,
    never from the raw query string, so a term the user negated or restricted
    to a field outside the default search fields contributes nothing, instead
    of resurfacing as a top-level clause matching every bigram field.

    ``free_text_tokens`` reports no field of its own, so the tokens are
    collected one default field at a time: a bare term, which the parser has
    already copied onto every default field, is therefore searched across
    every bigram field, while ``title:東京`` reaches ``bigram_title`` alone.
    Fields whose CJK text is identical (the bare-term case) share a single
    parse over all of their bigram fields at once.

    Raw (``analyzed=False``) tokens are used because the bigram fields have
    their own character-ngram analyzer: the default fields' word analyzers
    have no useful say over a CJK run, and running them first would only
    risk dropping it (remove_long) before the run is ever extracted.
    Returns None when the query has no CJK free text.
    """
    fields_by_text: dict[str, list[str]] = {}
    for field, bigram_field in _CJK_BIGRAM_FIELDS.items():
        tokens = wc.free_text_tokens(
            ast,
            registry=registry,
            fields=[field],
            analyzed=False,
        )
        cjk_text = extract_cjk_text(" ".join(tokens))
        if cjk_text:
            fields_by_text.setdefault(cjk_text, []).append(bigram_field)

    clauses: list[tuple[tantivy.Occur, tantivy.Query]] = [
        (tantivy.Occur.Should, query)
        for cjk_text, bigram_fields in fields_by_text.items()
        if (query := _parse_cjk_text(index, cjk_text, bigram_fields)) is not None
    ]
    return _any_of(clauses) if clauses else None


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
    tokens = wc.free_text_tokens(ast, registry=registry, fields=_DEFAULT_SEARCH_FIELDS)
    words = [t for t in tokens if _WORD_TOKEN_RE.fullmatch(t)]
    if not words:
        return None
    fuzzy_text = " ".join(words)
    try:
        return index.parse_query(
            fuzzy_text,
            _DEFAULT_SEARCH_FIELDS,
            field_boosts=_FIELD_BOOSTS,
            fuzzy_fields={f: (True, 1, True) for f in _DEFAULT_SEARCH_FIELDS},
        )
    except ValueError:
        logger.debug(
            "Skipping fuzzy search clause: token string is not valid "
            "tantivy query syntax: %r",
            fuzzy_text,
        )
        return None


_DEFAULT_SEARCH_FIELDS: Final[list[str]] = [
    "title",
    "content",
    "correspondent",
    "document_type",
    "tag",
]
_SIMPLE_SEARCH_FIELDS: Final[list[str]] = ["simple_title", "simple_content"]
_TITLE_SEARCH_FIELDS: Final[list[str]] = ["simple_title"]
# The bigram (character-ngram) companion of each default search field.
_CJK_BIGRAM_FIELDS: Final[dict[str, str]] = {
    field: f"bigram_{field}" for field in _DEFAULT_SEARCH_FIELDS
}
_CJK_CONTENT_FIELDS: Final[list[str]] = ["bigram_content"]
_CJK_TITLE_FIELDS: Final[list[str]] = ["bigram_title"]
_FIELD_BOOSTS = {"title": 2.0}
_SIMPLE_FIELD_BOOSTS = {"simple_title": 2.0}


def _any_of(clauses: list[tuple[tantivy.Occur, tantivy.Query]]) -> tantivy.Query:
    """Collapse a clause list: none -> empty, one -> itself (no wasted
    single-clause boolean_query wrapping), many -> boolean_query(clauses)."""
    if not clauses:
        return tantivy.Query.empty_query()
    if len(clauses) == 1:
        return clauses[0][1]
    return tantivy.Query.boolean_query(clauses)


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

    return _any_of(field_queries)


def parse_user_query(
    index: tantivy.Index,
    raw_query: str,
    tz: tzinfo,
) -> tantivy.Query:
    """
    Parse user query through whoosh-compat, then blend in fuzzy/CJK clauses.

    1. wc.parse() against the shared FieldRegistry (whoosh grammar -> AST).
       Bare notes:/custom_fields: prefixes resolve to their default subpath
       (notes.note:/custom_fields.value:) directly in the registry, via
       each JSON field's SubpathSpec(default=True).
    2. Any diagnostics (bad dates/numbers) map to SearchQueryError subclasses
       and raise — the view returns HTTP 400 with every offending field
       listed, not just the first.
    3. emit() turns the AST into a tantivy.Query directly (no string
       round-trip). A QueryError is routed by its Diagnostic's Cause
       (_map_emit_error): a construct that parses but can't execute against
       tantivy (e.g. a text-field range) is a 400, a registry/schema
       mismatch is logged and a 400, and an INTERNAL defect is re-raised.
    4. Optional fuzzy blend (ADVANCED_FUZZY_SEARCH_THRESHOLD) builds a
       plain word string from the parsed AST's free-text tokens
       (whoosh_compat.free_text_tokens) and feeds THAT to
       index.parse_query — never raw_query, whose whoosh grammar (date
       keywords, bracket-class wildcards, etc.) tantivy's parser rejects,
       which used to silently knock the fuzzy clause out of any mixed
       query (see _try_parse_fuzzy_query).
    5. Optional CJK bigram clause, built from the same parsed AST for the
       same reason (see _build_ast_cjk_query): a CJK term the query negated
       or fielded must not resurface through it.
    """
    registry = get_field_registry(settings.SEARCH_LANGUAGE)
    result = wc.parse(
        raw_query,
        registry=registry,
        default_fields=_DEFAULT_SEARCH_FIELDS,
        field_boosts=_FIELD_BOOSTS,
        tz=tz,
    )
    if result.diagnostics:
        raise _diagnostics_to_error(result.diagnostics)

    try:
        exact = tantivy_emit(result.ast, index=index, registry=registry)
    except QueryError as e:
        raise _map_emit_error(e) from e

    cjk_query = (
        _build_ast_cjk_query(index, result.ast, registry)
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

    return _any_of(clauses)


# The three whoosh-compat kinds for a wildcard on a field that cannot
# carry one. d.field_kind supplies the discriminator, so naming the field's
# type needs no second trip through the registry.
_PATTERN_ON_KINDS: Final = frozenset(
    {
        DiagnosticKind.PATTERN_ON_NUMERIC,
        DiagnosticKind.PATTERN_ON_BOOLEAN_EXISTS,
        DiagnosticKind.PATTERN_ON_SUBPATH,
    },
)


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
    if d.kind is DiagnosticKind.TOO_DEEP:
        return SearchQueryError("The search query is nested too deeply.")
    if d.kind in _PATTERN_ON_KINDS:
        kind_label = f" ({d.field_kind.name.lower()})" if d.field_kind else ""
        return SearchQueryError(
            f"Wildcard patterns are not supported for field "
            f"{field_name!r}{kind_label}.",
        )
    logger.warning("Unmapped parse diagnostic %s: %s", d.kind, d.message)
    return SearchQueryError("The search query could not be executed.")


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
    tokens = simple_search_tokens(raw_query)

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
        clauses.append((tantivy.Occur.Should, _any_of(token_queries)))

    if cjk_fields and _has_cjk(raw_query):
        cjk_q = _build_cjk_query(index, raw_query, cjk_fields)
        if cjk_q is not None:
            clauses.append((tantivy.Occur.Should, cjk_q))

    return _any_of(clauses)


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
    tokens = simple_search_tokens(
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
        _SIMPLE_SEARCH_FIELDS,
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
        _TITLE_SEARCH_FIELDS,
        cjk_fields=_CJK_TITLE_FIELDS,
    )
