# whoosh-compat transition design

Date: 2026-08-07
Status: approved
Related skill: `whoosh-compat-transition`

> **API reference used by this design.** `FieldRegistry` exposes one
> resolution path: `registry.make_ref(raw) -> FieldRef | None` interprets a
> raw, possibly dotted field string (an unknown field or an unknown subpath
> both return `None`), and `registry.resolve(ref) -> ResolvedField | None`
> looks up the resolved ref. `ResolvedField` carries `.spec` (the
> `FieldSpec`), `.json_path` (the subpath, or `None`), `.is_subpath`, and
> `.dotted_name` — read `resolved.spec.kind`, not `spec.kind` off a bare
> `FieldSpec`. `Diagnostic.field` is a `FieldRef`, not a string: use
> `str(d.field)` for the canonical dotted name, or `d.field.name` for the
> field alone (the name is canonical, so an aliased query like `type:`
> reports `document_type`). Every field-carrying AST leaf holds a `FieldRef`.
> `emit()`'s signature is `emit(node, *, index, registry) -> tantivy.Query`,
> with no `schema` parameter; it calls the library's own `analyze()` pipeline
> stage internally (token analysis, multitoken resolution, zero-token drop)
> before visiting the tree, so this design's call sites never invoke
> `analyze()` themselves. `FieldSpec.subpaths` is stored internally as
> `Mapping[str, SubpathSpec]`, though construction still accepts a plain
> `tuple[str, ...]` as sugar and normalizes it automatically — this design's
> own `PublicField.subpaths: tuple[str, ...]` (below) passes a tuple into
> `FieldSpec(..., subpaths=...)` and needs nothing further.
> `DiagnosticKind` has four members: `BAD_DATE`, `BAD_NUMBER`, `TOO_DEEP`, and
> `UNSUPPORTED_PATTERN`; the error-mapping code below needs cases for all
> four.
>
> A few library behaviors worth knowing before writing code against it:
> `parse()` validates its own configuration eagerly — an empty or unknown
> `default_fields`, or a `field_boosts` key that resolves to neither a known
> field nor an alias, raises `ValueError` at the `parse()` call itself, and an
> alias in either argument resolves normally. A naive `basedate` is rejected
> (`ValueError`) rather than silently read in the host machine's local
> timezone; pass an aware datetime. A wildcard/prefix pattern on a numeric
> (`U64`) field, a `BOOLEAN_EXISTS` field, or a JSON subpath produces a
> parse-time `Diagnostic(kind=UNSUPPORTED_PATTERN)` instead of silently
> mangling to an exact-match term or matching the wrong encoded bytes — this
> is directly relevant to `custom_fields.value`: a user typing
> `custom_fields.value:abc*` gets a diagnostic, not a query that silently
> matches the wrong documents. A bare JSON field name with no subpath
> (`notes:foo`) demotes to an ordinary text search for the literal string,
> the same treatment an unknown field or unknown subpath gets. Registry
> construction validates its input eagerly: exists-target cycles, empty
> field/alias names, duplicate aliases, dotted canonical names,
> invalid-character or empty JSON subpath strings, and a subpath that would
> shadow a registered plain field are all rejected at `FieldRegistry.__init__`
> with an actionable message, not deferred to query time.
>
> Fast-field existence checks against a JSON field are correct, both for
> whole-field existence (`notes:*`) and the per-subpath case
> (`custom_fields.value:*`, which checks only that subpath's own fast
> column). Marking `notes`/`custom_fields` fast is therefore a plain
> paperless-ngx-side index-size/query-cost tradeoff, independent of
> whoosh-compat correctness — worth a maintainer decision, not something this
> document settles.

## Summary

Replace paperless-ngx's hand-maintained query-translation layer
(`src/documents/search/_translate.py`, `src/documents/search/_dates.py`)
with [whoosh-compat](https://github.com/stumpylog/whoosh-compat): a typed
Whoosh-grammar parser that emits programmatically constructed
`tantivy.Query` objects instead of building an intermediate Tantivy query
_string_. The integration point is narrow: `parse_user_query()` in
`src/documents/search/_query.py` is the only function whose implementation
changes; `_backend.py`, `_tokenizer.py`, simple/title search, CJK handling,
and permission filtering are all unaffected.

Delivered as a stack of four paperless-ngx PRs plus one prerequisite change
in whoosh-compat itself (same maintainer, no cross-repo coordination
overhead), landed with no feature flag and no shadow-compare rollout period
— safety comes from a result-level acceptance test corpus and a
date-grammar parity audit instead.

## Architecture

```
raw_query (user-typed)
    │
    ▼
wc.parse(raw_query, registry=FIELD_REGISTRY, default_fields=DEFAULT_SEARCH_FIELDS,
         field_boosts=_FIELD_BOOSTS, tz=tz)
    │
    ▼
ParseResult(ast, diagnostics)
    │
    ├─ diagnostics non-empty? → map ALL diagnostics to SearchQueryError
    │  subclass(es) → HTTP 400 (never just the first diagnostic)
    │
    ▼
emit(ast, index=index, registry=FIELD_REGISTRY)
    │  (calls whoosh-compat's own analyze() pipeline stage internally, then
    │   raises UnsupportedQueryError → mapped to SearchQueryError → 400,
    │   for constructs that parse but can't execute against tantivy)
    ▼
tantivy.Query
    │
    ▼
existing clause assembly in parse_user_query(): Should(exact) + optional
fuzzy re-parse of raw_query + optional CJK bigram query, unchanged from today
    │
    ▼
_apply_permission_filter() in _backend.py wraps the result with
build_permission_filter() — entirely independent of whoosh-compat, unchanged
```

Permission filtering is explicitly out of scope for this migration:
`build_permission_filter()` builds its `tantivy.Query` directly against
`owner_id`/`viewer_id`/`viewer_group_id`, never through the parser or
registry, and those fields are exactly the internal `*_id` fields excluded
from the `FieldRegistry` (see "Field surface" below). Nothing in this
migration's diff touches it.

## PR stack

Each PR is independently buildable, reviewable, and CI-able; later PRs
rebase on earlier ones. No PR depends on whoosh-compat behavior it hasn't
already proven correct in isolation.

1. **Refactor `_schema.py` to a shared field-definition table.** Pure
   refactor — `build_schema()`'s output is byte-identical before and after.
   `test_schema.py` (existing) proves it.
2. **Pin whoosh-compat as a real dependency; build `FieldRegistry`.** New
   `_registry.py` built from the same table PR 1 introduced. Registry unit
   tests only — no wiring into search yet.
3. **Date-grammar parity audit.** A transitional, executable differential
   test using the still-present `_dates.py`/`_translate.py` as the oracle.
   Any gap found is fixed in whoosh-compat directly before this PR closes.
   A whoosh-compat PyPI release is expected around this point (see
   "Dependency pinning").
4. **Wire it in; delete the old path.** Rewrite `parse_user_query()`,
   diagnostics→exception mapping, add the result-level acceptance corpus,
   expand `test_api_search.py`, delete `_translate.py`/`_dates.py`/
   `test_translate.py` and the internals-testing classes in `test_query.py`,
   update `docs/usage.md` and changelog.

`Diagnostic` carries `field: FieldRef | None` and `raw_value: str | None`,
populated at its construction sites (`dateparse.py`'s `_error()`,
`default.py`'s `BAD_NUMBER` sites), so paperless can build typed exceptions
without parsing whoosh-compat's human-readable `message` text. `field` is a
`FieldRef`, not a plain string; see the API reference at the top of this
document.

## Field surface

The `FieldRegistry` covers only query-syntax-addressable fields — a subset
of the full Tantivy schema. Internal-only schema fields with no query-syntax
meaning of their own (`title_sort`/`correspondent_sort`/`type_sort` shadow
sort fields, `bigram_*` CJK fields, `simple_title`/`simple_content`,
`autocomplete_word`, `notes_text`) stay hardcoded `sb.add_*` calls in
`_schema.py`, untouched by the shared table.

**Decision: keep and document all five currently-undocumented-but-working
fields** (`asn`, `page_count`, `num_notes`, `original_filename`,
`checksum`) rather than dropping them — least risk of silently breaking an
existing saved view. `docs/usage.md`'s advanced-search section gets these
added with examples, as part of PR 4.

**Decision: `archive_checksum` stays out of scope.** Unlike `checksum`, it
isn't indexed in the Tantivy schema at all today (confirmed: `_schema.py`
only adds `checksum`; `_build_tantivy_doc` only calls
`doc.add_text("checksum", document.checksum)`). Making it searchable is a
schema-level change (new indexed field, new document population code), not
a parser-migration concern — left as a separate follow-up.

**Decision: internal `*_id` fields (`tag_id`, `correspondent_id`,
`document_type_id`, `storage_path_id`, `owner_id`, `viewer_id`,
`viewer_group_id`) are excluded from the `FieldRegistry` entirely.** They
remain Tantivy-schema-only, used exclusively by `build_permission_filter()`.
Because whoosh-compat folds any unrecognized `field:` prefix into literal
text (Whoosh-parity leniency, confirmed in `FieldsPlugin.do_fieldnames` —
not an error), a saved view typed as `tag_id:5` won't 400: it silently
becomes a text search for the literal string `tag_id:5`, most likely
returning zero results. This is a real behavior change and gets a
**changelog callout**, not just a docs update, since a docs addition alone
wouldn't surface it to someone skimming release notes.

## Shared field-definition table (`_fields.py`)

```python
from whoosh_compat import FieldKind  # reused directly — no parallel enum

@dataclass(frozen=True, slots=True)
class PublicField:
    name: str
    kind: FieldKind
    aliases: tuple[str, ...] = ()
    comma_values: bool = False
    date_only: bool = False
    fast: bool = False
    subpaths: tuple[str, ...] = ()   # JSON kind only

PUBLIC_FIELDS = (
    PublicField("title", FieldKind.TEXT),
    PublicField("content", FieldKind.TEXT),
    PublicField("correspondent", FieldKind.TEXT),
    PublicField("document_type", FieldKind.TEXT, aliases=("type",)),
    PublicField("storage_path", FieldKind.TEXT, aliases=("path",)),
    PublicField("original_filename", FieldKind.TEXT),
    PublicField("tag", FieldKind.TEXT, comma_values=True),
    PublicField("checksum", FieldKind.KEYWORD),
    PublicField("asn", FieldKind.U64, fast=True),
    PublicField("page_count", FieldKind.U64, fast=True),
    PublicField("num_notes", FieldKind.U64, fast=True),
    PublicField("created", FieldKind.DATE, date_only=True, fast=True),
    PublicField("modified", FieldKind.DATETIME, fast=True),
    PublicField("added", FieldKind.DATETIME, fast=True),
    PublicField("notes", FieldKind.JSON, subpaths=("user", "note")),
    PublicField("custom_fields", FieldKind.JSON, subpaths=("name", "value")),
)
```

`build_schema()` derives its `sb.add_*` call and tokenizer from `kind`
(TEXT/KEYWORD → `add_text_field` with `paperless_text`/`raw` tokenizer
respectively; U64 → `add_unsigned_field`; DATE/DATETIME → `add_date_field`;
JSON → `add_json_field`). The `notes_text` snippet-companion field stays a
separate hardcoded line right after the `notes` entry — schema-only
plumbing with no query-syntax meaning.

`_registry.py` maps each `PublicField` to a `whoosh_compat.FieldSpec`,
kept as one flat dataclass (no kind-specific subclassing) to mirror
whoosh-compat's own `FieldSpec` design, which validates kind-conditional
attributes (e.g. JSON requires non-empty `subpaths`) at
`FieldRegistry.__init__` rather than in the type system.

Footnote for whoever writes `_registry.py`: `FieldRegistry.__init__` forces
`date_only=True` on _any_ `FieldKind.DATE` spec regardless of what's
passed, unconditionally — `PublicField.date_only` isn't an independent
knob for DATE fields the way it might look; it only matters in the sense
that `created` sets it explicitly for clarity, while `modified`/`added`
use `FieldKind.DATETIME` instead of relying on that override.

**`PublicField.subpaths` stays `tuple[str, ...]`, not a nested structure.**
Confirmed against whoosh-compat's own `FieldRegistry.make_ref()`: it splits a
dotted query term on the _first_ dot only and matches the remainder as an
exact string against `spec.subpaths` — even the docstring's own
`"metadata.author.name"` example is a single opaque string in the tuple,
not a recursive tree. (`FieldSpec.subpaths` itself now stores a `Mapping[str,
SubpathSpec]` internally, normalized from whatever tuple is passed at
construction; that's an implementation detail of `FieldSpec.__post_init__`,
not something `PublicField`'s own table needs to mirror — passing a plain
tuple into `FieldSpec(..., subpaths=...)` still works exactly as written
here.) A tuple of strings is exactly as expressive as the library it feeds;
inventing richer structure in `PublicField` now would just get flattened
back to strings at the registry-construction boundary. Real recursive
nesting, if ever needed, is new whoosh-compat capability first (the
per-subpath `SubpathSpec` container exists specifically to make that a
later, additive change).

**JSON document population stays separate from `subpaths`.** `subpaths` is
query-side only — it declares which dotted names are legal to type and
which JSON keys the emitter should address. It says nothing about how
`_backend.py::_build_tantivy_doc` builds the JSON documents at index-write
time, and that logic isn't uniform attribute access (`note.user.username`
needs a null guard and isn't `note.user`; `cfi.value_for_search` is a
property, not a literal `value` attribute), so a generic
`getattr(obj, subpath_name)` scheme would silently do the wrong thing for
both. That code stays hand-written, unchanged by this migration. Mitigation
instead: a coupling test (PR 2, alongside the registry unit tests) asserting
the literal JSON keys used in `_build_tantivy_doc`'s `doc.add_json(...)`
calls match `PUBLIC_FIELDS`' `notes`/`custom_fields` `subpaths` exactly, so
drift between the two is caught rather than silently becoming an
unqueryable (or silently unindexed) field.

**JSON subpath queries (`notes.*`, `custom_fields.*`) route through
`index.parse_query()`, not programmatic construction, given paperless's
pinned tantivy version.** Installed `tantivy-py`'s `Query.term_query`
cannot resolve a JSON subpath by exact field name — it raises as if the
field didn't exist. Until
[tantivy-py#716](https://github.com/quickwit-oss/tantivy-py/pull/716) lands
and ships, whoosh-compat's `TantivyEmitter._json_paths_supported()` feature-
detects this per process and falls back to a strictly escaped, single-leaf
`index.parse_query()` call for just that one leaf (whoosh-compat's README/
ARCHITECTURE.md call this out as "the JSON subpath carve-out"). Paperless
pins `tantivy~=0.26.0`, squarely inside the affected range (whoosh-compat's
`tantivy` extra only requires `tantivy>=0.24`, so nothing prevents this
combination). Nothing needs to change in this design because of it — the
carve-out is self-retiring on whoosh-compat's side once tantivy-py catches
up — but the acceptance corpus's `notes.user:`/`custom_fields.name:` cases
(PR 4) are exercising that fallback escaping path specifically, not the
programmatic path every other field goes through, and that's worth knowing
if one of those cases ever behaves oddly around quoting/escaping. A
multi-token JSON subpath value with `Multitoken.AND`/`OR` now gets correct
combinator semantics through this fallback (each token becomes its own
`index.parse_query()`-backed leaf, `Must`/`Should`-combined normally,
instead of collapsing into one space-joined phrase-shaped query); a genuine
quoted phrase on a JSON subpath still cannot carry an explicit slop through
this fallback (silently ignored, `~N` has no effect) until the carve-out
retires. Also worth knowing given `custom_fields.value` is JSON: this
fallback's `index.parse_query()` call gives a JSON subpath term free
numeric/boolean type inference tantivy's own query grammar provides (a
query like `custom_fields.value:100` matches both a stored JSON number `100`
and a stored JSON string `"100"`); the future programmatic path (once
tantivy-py#716 ships) has no equivalent union and would need this
re-evaluated for numeric/boolean custom field values specifically
(whoosh-compat's `DIVERGENCES.md` entry 22 tracks this open question).

**Analyzer wiring**: `FieldSpec.analyzer` reuses the same `tantivy
.TextAnalyzer` objects `_tokenizer.py` already builds (`_paperless_text
(language)`, etc.) — standalone objects not dependent on index
registration, so `_registry.py` calls the same builder functions and binds
`.analyze` directly; `checksum` (KEYWORD, `raw` tokenizer) gets an identity
analyzer (`lambda t: [t]`). `pattern_normalizer` for every field is
`_tokenizer.ascii_fold` (character-fold only, never stemming) per the
skill's explicit instruction. The whole `FieldRegistry` is built once,
cached keyed by `settings.SEARCH_LANGUAGE`, rebuilt on the same trigger
`register_tokenizers()` already uses.

## Error handling

```python
class SearchQueryError(ValueError): ...          # unchanged, base

class InvalidDateQuery(SearchQueryError):         # unchanged
    def __init__(self, field, value): ...

class InvalidNumberQuery(SearchQueryError):       # new
    def __init__(self, field: str | None, value: str | None) -> None:
        self.field = field
        self.value = value
        super().__init__(f"Invalid numeric value {value!r} for field {field!r}.")

class MultipleSearchQueryErrors(SearchQueryError):  # new
    """Aggregates every user-fixable error from one parse, not just the first."""
    def __init__(self, errors: Sequence[SearchQueryError]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(str(e) for e in self.errors))
```

```python
def parse_user_query(index, raw_query, tz):
    registry = get_field_registry(settings.SEARCH_LANGUAGE)
    result = wc.parse(
        raw_query, registry=registry, default_fields=DEFAULT_SEARCH_FIELDS,
        field_boosts=_FIELD_BOOSTS, tz=tz,
    )
    if result.diagnostics:
        raise _diagnostics_to_error(result.diagnostics)   # ALL diagnostics, not [0]

    try:
        exact = tantivy_emit(result.ast, index=index, registry=registry)
    except UnsupportedQueryError as e:
        raise SearchQueryError(str(e)) from e

    # CJK: unchanged — already re-parses raw_query directly via index.parse_query,
    # never went through translate_query, so nothing here changes.
    cjk_query = _build_cjk_query(index, raw_query, _CJK_ALL_FIELDS) if _has_cjk(raw_query) else None

    clauses = [(tantivy.Occur.Should, exact)]
    threshold = settings.ADVANCED_FUZZY_SEARCH_THRESHOLD
    if threshold is not None:
        # Fuzzy re-parses raw_query (not the AST) — no clean AST-level fuzzy
        # equivalent exists; fuzzy matching was always an approximate,
        # secondary clause, so this divergence from the exact-match path is
        # acceptable.
        fuzzy = index.parse_query(raw_query, DEFAULT_SEARCH_FIELDS, field_boosts=_FIELD_BOOSTS,
                                   fuzzy_fields={f: (True, 1, True) for f in DEFAULT_SEARCH_FIELDS})
        clauses.append((tantivy.Occur.Should, tantivy.Query.boost_query(fuzzy, 0.1)))
    if cjk_query is not None:
        clauses.append((tantivy.Occur.Should, cjk_query))

    return exact if len(clauses) == 1 else tantivy.Query.boolean_query(clauses)


def _diagnostics_to_error(diagnostics: tuple[Diagnostic, ...]) -> SearchQueryError:
    errors = [_single_diagnostic_to_error(d) for d in diagnostics]
    return errors[0] if len(errors) == 1 else MultipleSearchQueryErrors(errors)


def _single_diagnostic_to_error(d: Diagnostic) -> SearchQueryError:
    # d.field is a FieldRef, not a string: str(d.field) gives the canonical
    # dotted name (e.g. "created", "custom_fields.value"); an aliased query
    # (type:) reports the field it resolves to (document_type).
    field_name = str(d.field) if d.field is not None else None
    if d.kind is DiagnosticKind.BAD_DATE:
        return InvalidDateQuery(field_name, d.raw_value)
    if d.kind is DiagnosticKind.BAD_NUMBER:
        return InvalidNumberQuery(field_name, d.raw_value)
    # TOO_DEEP (pathological paren nesting) and UNSUPPORTED_PATTERN (a
    # wildcard/prefix pattern on a numeric, BOOLEAN_EXISTS, or JSON-subpath
    # field) both fall through to the generic message; a typed subclass for
    # either isn't warranted unless a caller needs to branch on it.
    return SearchQueryError(d.message)
```

No `except Exception: query_str = raw_query` fallback — per the skill, that
legacy defensive branch is explicitly not carried forward. A bug in the new
path must surface as a real error, not silently degrade to stale behavior.

`views.py`'s existing `except SearchQueryError as e: raise
ValidationError({"query": [str(e)]}) from e` handler gets one added branch
to surface every aggregated message instead of just one:

```python
except SearchQueryError as e:
    messages = [str(sub) for sub in e.errors] if isinstance(e, MultipleSearchQueryErrors) else [str(e)]
    raise ValidationError({"query": messages}) from e
```

`d.field`/`d.raw_value` are populated for `BAD_DATE` and `BAD_NUMBER`
diagnostics; if either is `None` for a diagnostic kind that doesn't populate
them, `_single_diagnostic_to_error`'s fallthrough to
`SearchQueryError(d.message)` still applies.

Deferred, explicitly out of scope for this PR stack: any frontend use of
`startchar`/`endchar` (already present on `Diagnostic` today) to highlight
the offending span in the search box. Backend-only for now, per explicit
decision.

## Testing

Existing test inventory (`src/documents/tests/search/` and
`test_api_search.py`):

| File                                                                                                                                                                                          | Fate                                                                                                                                                                       |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_translate.py`                                                                                                                                                                           | Deleted (PR 4) — subject deleted                                                                                                                                           |
| `test_query.py`: `TestCreatedDateField`, `TestDateTimeFields`, `TestWhooshQueryRewriting`, `TestYearRangeRewriting`, `TestNonDateFieldsNotRewritten`, `TestPassthrough`, `TestNormalizeQuery` | Deleted (PR 4) — test `_translate.py`/`_dates.py` internals or intermediate query strings                                                                                  |
| `test_query.py`: `TestParseUserQuery`                                                                                                                                                         | Reviewed at plan time; result-level assertions folded into the new acceptance module, internals-only assertions dropped                                                    |
| `test_query.py`: `TestParseSimpleTextHighlightQuery`, `TestPermissionFilter`                                                                                                                  | Unchanged — never touched `translate_query`                                                                                                                                |
| `test_schema.py`, `test_tokenizer.py`, `test_backend.py`, `test_lock_backoff.py`, `test_migration_fulltext_query_field_prefixes.py`                                                           | Unchanged                                                                                                                                                                  |
| `test_api_search.py` (`TestDocumentSearchApi`, 43 tests)                                                                                                                                      | **Stays green across every PR in the stack** (hard gate, not just PR 4) — full HTTP+DB+index integration coverage catches wiring mistakes none of the narrower tests would |

New tests per PR:

- **PR 2**: `test_registry.py` — internal `*_id` names rejected; `type`/
  `path` aliases resolve to canonical fields; JSON subpaths match
  `docs/usage.md`; registry construction deterministic per language; the
  `notes`/`custom_fields` dict-key coupling test described above.
- **PR 3**: `test_date_grammar_parity.py` — transitional, parametrized over
  every keyword/unit `_dates.py`/`_translate.py` accept today
  (`_DATE_KEYWORDS`, all of `_UNIT_ALIASES`'s Whoosh-era abbreviations —
  `yrs`/`mos`/`wks`/`hrs`/`mins`/`secs` etc. — digit-precision forms, ISO
  dash forms, `now-7d`/`now+1h`/`now-30m` compact offsets, open/reversed
  ranges). Each case parses through `wc.parse()` against a DATE-kind
  `FieldRegistry` and asserts no diagnostics come back — a coverage check
  only (does whoosh-compat accept this input at all), not a check on the
  bounds or AST shape it parses to, which is whoosh-compat's own
  differential-testing responsibility against a real whoosh oracle, not
  something to re-verify here against `_translate.py` as a second, weaker
  oracle. If the team wants confidence that actual search _behavior_ at a
  given keyword didn't change, that belongs in the PR 4 result-level
  acceptance corpus (real indexed documents at date boundaries, matched-ID
  assertions), not an AST/bounds comparison.
  Deleted again in PR 4 along with the legacy code it audits, superseded by
  the permanent acceptance corpus. This audit is scoped to _parity_ only —
  whoosh-compat's date grammar is a strict superset of what `_dates.py`
  accepts today (e.g. `tomorrow`, `now`, `midnight`, `noon`, weekday names
  like `next monday`), so the migration also grants new date vocabulary for
  free. That's a nice side effect, not something this PR needs to test or
  document beyond noting it in the changelog alongside the other behavior
  changes.
- **PR 4**:
  - Result-level acceptance module (paperless's analogue of whoosh-compat's
    `test_acceptance_e2e.py`): a real index built via `build_schema()`, the
    issue #13568 queries verbatim, real saved-view strings, every date
    keyword/unit, field aliases, comma lists, numeric/date ranges,
    bracket-class wildcards, boosts, JSON subpaths — asserted by matched
    document-ID set, `pytest.param(..., id=...)` per case. Plus a
    multi-diagnostic case (two bad fields → `MultipleSearchQueryErrors`
    with both messages present) and one `Multitoken` case nested inside a
    top-level `OR` (proves DIVERGENCES entry 15 doesn't matter for
    paperless's data, per the skill).
  - `test_api_search.py` expanded: a multi-bad-field query (e.g.
    `created:notadate AND asn:notanumber`) asserting the 400 response's
    `query` list contains both messages; end-to-end searches on the five
    newly-documented fields (`asn:`, `page_count:`, `num_notes:`,
    `original_filename:`, `checksum:`) returning the right documents
    through the real index.

## Dependency pinning

Stays `path = "../whoosh-compat"` in `[tool.uv.sources]` through the whole
PR stack — both repos are being actively co-developed. The final swap
happens at PR 4:

- **Primary plan**: whoosh-compat is released to PyPI around PR 3 (per
  your stated intent), assuming the parity audit doesn't turn up anything
  needing a second round. PR 4 switches to a pinned PyPI version
  (`whoosh-compat[tantivy]==X.Y.Z` in `dependencies`, the
  `[tool.uv.sources]` override removed entirely).
- **Fallback**: if the PyPI release slips past PR 4's start, pin an exact
  git commit SHA instead (`whoosh-compat[tantivy] @ git+https://github.com/
stumpylog/whoosh-compat@<sha>`), per the skill's "pre-1.0: pin an exact
  version or git SHA, upgrades are deliberate" guidance.

The `TODO` comment already sitting in `pyproject.toml` (from the earlier
smoke-test setup) gets updated to reflect this — "release, else pinned SHA"
— rather than committing hard to one path before it's known which applies.

## Explicitly out of scope

- `archive_checksum` indexing/search (separate schema-level follow-up).
- Frontend consumption of `Diagnostic.startchar`/`endchar` for in-box error
  highlighting (backend-only for this PR stack).
- A feature flag or shadow-compare rollout period — explicitly decided
  against; safety comes from the acceptance corpus and parity audit instead.
- Any change to `build_permission_filter()`/`_apply_permission_filter()` —
  confirmed untouched by this migration.
