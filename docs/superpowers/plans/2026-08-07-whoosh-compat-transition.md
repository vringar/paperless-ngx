# whoosh-compat Transition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **API reference used by this plan.** The design spec's API reference
> (top of `docs/superpowers/specs/2026-08-07-whoosh-compat-transition-design.md`)
> has the full shape; the points that matter most while executing the tasks
> below: the one resolver is `registry.make_ref(raw) -> FieldRef | None` (a
> `None` return is how an unknown field or unknown subpath reports itself)
> plus `registry.resolve(ref) -> ResolvedField | None` — read `.spec` off the
> result for the `FieldSpec`, `.json_path` for the subpath. `Diagnostic.field`
> is a `FieldRef`, not a string: call `str(d.field)` (or `d.field.name`)
> before passing it to `InvalidDateQuery`/`InvalidNumberQuery`, whose own
> constructors expect `str | None`. `emit()`'s signature is `emit(node, *,
index, registry)`, with no `schema` parameter: every `tantivy_emit(...)`
> call site in this plan drops `schema=...`. `DiagnosticKind` has four
> members: `BAD_DATE`, `BAD_NUMBER`, `TOO_DEEP`, `UNSUPPORTED_PATTERN` (the
> last fires for a wildcard/prefix pattern on a numeric field, a
> `BOOLEAN_EXISTS` field, or a JSON subpath, e.g. `custom_fields.value:abc*`).
>
> Verify against the whoosh-compat checkout rather than this plan wherever the
> two disagree. The library is the source of truth.

**Goal:** Replace paperless-ngx's hand-rolled query-translation layer (`_translate.py`/`_dates.py`) with whoosh-compat, a typed Whoosh-grammar parser that emits programmatic `tantivy.Query` objects directly.

**Architecture:** A shared field-definition table (`_fields.py`) drives both the Tantivy schema (`_schema.py`) and a new `whoosh_compat.FieldRegistry` (`_registry.py`), eliminating drift between what's indexed and what's query-addressable. `parse_user_query()` in `_query.py` becomes `wc.parse() -> diagnostics check -> emit()`, replacing the old regex scan/render pipeline. Landed as four sequential PRs with no feature flag; safety comes from a result-level acceptance test corpus and a date-grammar parity audit against the code being deleted.

**Tech Stack:** Django 5.2, pytest (no Django TestCase), `uv`, tantivy-py 0.26, whoosh-compat (local path dependency at `../whoosh-compat` for the duration of this plan).

**Spec:** `docs/superpowers/specs/2026-08-07-whoosh-compat-transition-design.md` — read it before starting; this plan implements it task-by-task and does not repeat its rationale.

## Global Constraints

- Run all Python commands via `uv run` from `src/` (never bare `python`/`pytest`).
- Tests: pytest fixtures/classes only, no `Django TestCase`. New test subjects get their own file, not appended to an existing shared one.
- Single test file during development: `uv run pytest path/to/test_file.py --override-ini="addopts="` (disables xdist).
- `whoosh-compat` stays a `path = "../whoosh-compat"` dependency (`[tool.uv.sources]`) through every task in this plan — do not switch to a PyPI/git pin until explicitly instructed in Task 16.
- No feature flag, no dual runtime path, no shadow-compare logging — decided against in the spec.
- Never carry forward a bare `except Exception:` fallback around query translation/parsing — an integration bug must surface, not silently degrade.
- Every commit in this plan follows the repo's existing commit-message conventions (conventional-commit-style prefix, e.g. `feat:`, `refactor:`, `test:`, `docs:`).

## Delegation guidance

Each task below has a **Suggested executor** line (`agentType`, `model`) for whoever dispatches this plan via `subagent-driven-development` or the `Agent`/`Workflow` tools. These are defaults, not hard requirements — override if the dispatcher has better information. General pattern used throughout: mechanical, narrowly-specified tasks (data tables, deletions, docs, dependency edits) go to a cheap/fast model; tasks requiring judgment about correctness against two systems at once (schema/registry parity, diagnostics mapping, query building, acceptance-corpus authoring) go to a higher-effort model. `python-pro` fits most of this plan (typed Python, dataclasses, parser/query internals); `django-developer` fits the two tasks that are primarily about DRF request/response/view behavior.

---

## Phase A — PR 1: Shared field-definition table

### Task 1: Create `_fields.py` with `PublicField` and `PUBLIC_FIELDS`

**Suggested executor:** `agentType: general-purpose`, `model: haiku` — a fully-specified data table, no design judgment required.

**Files:**

- Create: `src/documents/search/_fields.py`
- Test: `src/documents/tests/search/test_fields.py`

**Interfaces:**

- Produces: `PublicField` (frozen dataclass), `PUBLIC_FIELDS: tuple[PublicField, ...]` — consumed by Task 2 (`_schema.py`) and Task 4 (`_registry.py`).

- [ ] **Step 1: Write the failing test**

```python
# src/documents/tests/search/test_fields.py
from whoosh_compat import FieldKind

from documents.search._fields import PUBLIC_FIELDS
from documents.search._fields import PublicField


class TestPublicFields:
    def test_every_field_has_a_whoosh_compat_kind(self) -> None:
        for field in PUBLIC_FIELDS:
            assert isinstance(field.kind, FieldKind)

    def test_names_are_unique(self) -> None:
        names = [f.name for f in PUBLIC_FIELDS]
        assert len(names) == len(set(names))

    def test_json_fields_have_subpaths(self) -> None:
        for field in PUBLIC_FIELDS:
            if field.kind is FieldKind.JSON:
                assert field.subpaths, f"{field.name} is JSON but has no subpaths"

    def test_non_json_fields_have_no_subpaths(self) -> None:
        for field in PUBLIC_FIELDS:
            if field.kind is not FieldKind.JSON:
                assert field.subpaths == ()

    def test_document_type_alias_is_type(self) -> None:
        field = next(f for f in PUBLIC_FIELDS if f.name == "document_type")
        assert field.aliases == ("type",)

    def test_storage_path_alias_is_path(self) -> None:
        field = next(f for f in PUBLIC_FIELDS if f.name == "storage_path")
        assert field.aliases == ("path",)

    def test_tag_allows_comma_values(self) -> None:
        field = next(f for f in PUBLIC_FIELDS if f.name == "tag")
        assert field.comma_values is True

    def test_notes_subpaths(self) -> None:
        field = next(f for f in PUBLIC_FIELDS if f.name == "notes")
        assert field.subpaths == ("user", "note")

    def test_custom_fields_subpaths(self) -> None:
        field = next(f for f in PUBLIC_FIELDS if f.name == "custom_fields")
        assert field.subpaths == ("name", "value")

    def test_no_internal_id_fields_present(self) -> None:
        # tag_id/owner_id/viewer_id/etc. are permission-filter-only fields,
        # never user-query-addressable (see design spec, "Field surface").
        names = {f.name for f in PUBLIC_FIELDS}
        assert not any(name.endswith("_id") for name in names)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src && uv run pytest documents/tests/search/test_fields.py -v --override-ini="addopts="`
Expected: FAIL with `ModuleNotFoundError: No module named 'documents.search._fields'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/documents/search/_fields.py
from __future__ import annotations

from dataclasses import dataclass

from whoosh_compat import FieldKind


@dataclass(frozen=True, slots=True)
class PublicField:
    """One query-syntax-addressable field, shared by the Tantivy schema
    builder (_schema.py) and the whoosh-compat FieldRegistry (_registry.py).

    Internal-only schema fields with no query-syntax meaning of their own
    (sort shadow fields, bigram CJK fields, simple_title/simple_content,
    autocomplete_word, notes_text) are NOT represented here — they stay
    hardcoded in _schema.py's build_schema().
    """

    name: str
    kind: FieldKind
    aliases: tuple[str, ...] = ()
    comma_values: bool = False
    date_only: bool = False
    fast: bool = False
    subpaths: tuple[str, ...] = ()  # JSON kind only


PUBLIC_FIELDS: tuple[PublicField, ...] = (
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

- [ ] **Step 4: Run test to verify it passes**

Run: `cd src && uv run pytest documents/tests/search/test_fields.py -v --override-ini="addopts="`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add src/documents/search/_fields.py src/documents/tests/search/test_fields.py
git commit -m "feat(search): add shared PUBLIC_FIELDS table"
```

---

### Task 2: Refactor `build_schema()` to consume `PUBLIC_FIELDS`

**Suggested executor:** `agentType: python-pro`, `model: sonnet` — must not change `build_schema()`'s output; requires care reading tantivy-py's `SchemaBuilder` API correctly.

**Files:**

- Modify: `src/documents/search/_schema.py:22-115` (the `build_schema()` function body)
- Test: `src/documents/tests/search/test_schema.py` (existing file — extend, do not replace)

**Interfaces:**

- Consumes: `PUBLIC_FIELDS`, `PublicField` from `documents.search._fields` (Task 1).
- Produces: `build_schema()` — signature and return type (`tantivy.Schema`) unchanged; this task only changes how its body is constructed internally.

- [ ] **Step 1: Write the failing test**

Read the existing `src/documents/tests/search/test_schema.py` first to see what's already covered (do not duplicate). Add this new test proving field-for-field equivalence with the pre-refactor hardcoded list:

```python
# appended to src/documents/tests/search/test_schema.py
from documents.search._fields import PUBLIC_FIELDS


class TestSchemaMatchesPublicFields:
    def test_every_public_field_is_in_the_schema(self) -> None:
        schema = build_schema()
        schema_field_names = {f.name for f in schema}
        for field in PUBLIC_FIELDS:
            assert field.name in schema_field_names, (
                f"{field.name} is in PUBLIC_FIELDS but missing from build_schema()"
            )

    def test_asn_page_count_num_notes_are_fast_unsigned_fields(self) -> None:
        # Spot-check kind-derived construction for the U64 fields.
        schema = build_schema()
        doc = tantivy.Document()
        doc.add_unsigned("id", 1)
        doc.add_text("checksum", "x")
        doc.add_unsigned("asn", 42)
        doc.add_unsigned("page_count", 3)
        doc.add_unsigned("num_notes", 0)
        doc.add_date("created", datetime(2020, 1, 1, tzinfo=UTC))
        doc.add_date("modified", datetime(2020, 1, 1, tzinfo=UTC))
        doc.add_date("added", datetime(2020, 1, 1, tzinfo=UTC))
        index = tantivy.Index(schema)
        register_tokenizers(index, None)
        writer = index.writer()
        writer.add_document(doc)
        writer.commit()
        index.reload()
        searcher = index.searcher()
        results = searcher.search(tantivy.Query.term_query(schema, "asn", 42), limit=1)
        assert len(results.hits) == 1
```

(Add `import tantivy`, `from datetime import UTC, datetime`, and
`from documents.search._tokenizer import register_tokenizers` to the file's
existing imports if not already present.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src && uv run pytest documents/tests/search/test_schema.py -v --override-ini="addopts="`
Expected: FAIL — `test_every_public_field_is_in_the_schema` and/or the new fast-field test currently pass already (the fields already exist pre-refactor) — if so, this step's real purpose is the _next_ step's regression check. Confirm the test passes against the CURRENT (pre-refactor) `build_schema()` first, so you know it's a valid baseline, then proceed to refactor and re-run to confirm it still passes after.

- [ ] **Step 3: Refactor `build_schema()`**

Replace the per-field `sb.add_text_field`/`sb.add_unsigned_field`/`sb.add_date_field`/`sb.add_json_field` calls for the 16 `PUBLIC_FIELDS` entries (title, content, correspondent, document_type, storage_path, original_filename, tag, checksum, asn, page_count, num_notes, created, modified, added, notes, custom_fields) with a loop driven by `PUBLIC_FIELDS`. Leave every other field (`id`, sort shadow fields, bigram fields, `simple_title`/`simple_content`, `autocomplete_word`, the `*_id` permission fields, `notes_text`) exactly as they are today — untouched, still hardcoded.

```python
# src/documents/search/_schema.py
from whoosh_compat import FieldKind

from documents.search._fields import PUBLIC_FIELDS


def build_schema() -> tantivy.Schema:
    sb = tantivy.SchemaBuilder()

    sb.add_unsigned_field("id", stored=True, indexed=True, fast=True)

    for field in PUBLIC_FIELDS:
        if field.kind is FieldKind.TEXT:
            sb.add_text_field(field.name, stored=True, tokenizer_name="paperless_text")
        elif field.kind is FieldKind.KEYWORD:
            sb.add_text_field(field.name, stored=True, tokenizer_name="raw")
        elif field.kind is FieldKind.U64:
            sb.add_unsigned_field(
                field.name,
                stored=True,
                indexed=True,
                fast=field.fast,
            )
        elif field.kind in (FieldKind.DATE, FieldKind.DATETIME):
            sb.add_date_field(
                field.name,
                stored=True,
                indexed=True,
                fast=field.fast,
            )
        elif field.kind is FieldKind.JSON:
            sb.add_json_field(field.name, stored=True, tokenizer_name="paperless_text")
            if field.name == "notes":
                # Plain-text companion for snippet generation — tantivy's
                # SnippetGenerator does not support JSON fields. Schema-only,
                # no query-syntax meaning, not in PUBLIC_FIELDS.
                sb.add_text_field(
                    "notes_text",
                    stored=True,
                    tokenizer_name="paperless_text",
                )

    # Shadow sort fields - fast, not stored/indexed
    for field in ("title_sort", "correspondent_sort", "type_sort"):
        sb.add_text_field(
            field,
            stored=False,
            tokenizer_name="simple_analyzer",
            fast=True,
        )

    # CJK support - not stored, indexed only
    sb.add_text_field("bigram_content", stored=False, tokenizer_name="bigram_analyzer")
    sb.add_text_field("bigram_title", stored=False, tokenizer_name="bigram_analyzer")
    sb.add_text_field(
        "bigram_correspondent",
        stored=False,
        tokenizer_name="bigram_analyzer",
    )
    sb.add_text_field(
        "bigram_document_type",
        stored=False,
        tokenizer_name="bigram_analyzer",
    )
    sb.add_text_field("bigram_tag", stored=False, tokenizer_name="bigram_analyzer")

    # Simple substring search support for title/content - not stored, indexed only
    sb.add_text_field(
        "simple_title",
        stored=False,
        tokenizer_name="simple_search_analyzer",
    )
    sb.add_text_field(
        "simple_content",
        stored=False,
        tokenizer_name="simple_search_analyzer",
    )

    sb.add_text_field("autocomplete_word", stored=False, tokenizer_name="raw")

    for field in (
        "correspondent_id",
        "document_type_id",
        "storage_path_id",
        "tag_id",
        "owner_id",
        "viewer_id",
        "viewer_group_id",
    ):
        sb.add_unsigned_field(field, stored=False, indexed=True, fast=True)

    return sb.build()
```

Note the iteration order above changes vs. today's hardcoded order (`PUBLIC_FIELDS` groups all 16 shared fields together instead of interleaving them with sort/bigram/simple fields as today's code does). Tantivy's `Schema` is not order-sensitive for lookup (`schema.get_field(name)`/iteration by name), so this is safe — confirmed by the byte-identical-output requirement in Step 4 checking field presence/properties, not declaration order. If Step 4 finds an order-dependent behavior, stop and re-read `test_schema.py`'s existing assertions before proceeding.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd src && uv run pytest documents/tests/search/test_schema.py -v --override-ini="addopts="`
Expected: PASS, including every pre-existing assertion in `test_schema.py` (not just the two added in Step 1) — this is the byte-identical-output proof the spec calls for.

Also run the full search test suite as a broader regression check before committing:

Run: `cd src && uv run pytest documents/tests/search/ documents/tests/test_api_search.py -v`
Expected: PASS (no changes to indexing/query behavior yet — this refactor must be invisible to every other test)

- [ ] **Step 5: Commit**

```bash
git add src/documents/search/_schema.py src/documents/tests/search/test_schema.py
git commit -m "refactor(search): derive build_schema() from shared PUBLIC_FIELDS table"
```

---

## Phase B — PR 2: whoosh-compat dependency + `FieldRegistry`

### Task 3: Commit the whoosh-compat dependency addition

**Suggested executor:** `agentType: general-purpose`, `model: haiku` — mechanical, the change already exists uncommitted in the working tree from an earlier smoke test.

**Files:**

- Modify: `pyproject.toml` (already has uncommitted changes from an earlier session — verify, don't re-author)

**Interfaces:**

- Produces: `whoosh_compat` importable in `src/`, resolved from `path = "../whoosh-compat"`.

- [ ] **Step 1: Verify the existing uncommitted change**

Run: `git diff pyproject.toml`

Confirm it shows exactly two additions: `"whoosh-compat[tantivy]",` in the `dependencies` list (alphabetically between `whitenoise` and `zxing-cpp`), and a `whoosh-compat = { path = "../whoosh-compat" }` entry under `[tool.uv.sources]` with a `# TODO: switch to a pinned git source once whoosh-compat has a tagged release` comment above it. If the diff looks different or is empty, re-add both pieces manually before proceeding — do not skip this step on the assumption it's already correct.

- [ ] **Step 2: Update the TODO comment to reflect the actual plan**

The comment currently says "once whoosh-compat has a tagged release" — update it per the spec's "Dependency pinning" section (PyPI release expected around PR 3, git SHA as fallback):

```toml
# TODO: switch to a pinned PyPI version once whoosh-compat releases
# (expected around this repo's PR 3 in the transition plan); fall back to a
# pinned git commit SHA if that release slips. See
# docs/superpowers/specs/2026-08-07-whoosh-compat-transition-design.md.
whoosh-compat = { path = "../whoosh-compat" }
```

- [ ] **Step 3: Verify it resolves**

Run: `cd src && uv sync`
Expected: resolves cleanly, `whoosh-compat==0.1.0.dev0` installed from the local path (as already proven in an earlier session).

Run: `cd src && uv run python -c "import whoosh_compat; print(whoosh_compat.__file__)"`
Expected: prints a path under `.venv/.../site-packages/whoosh_compat/__init__.py`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add whoosh-compat as a local-path dependency"
```

---

### Task 4: Build `_registry.py` — `get_field_registry()`

**Suggested executor:** `agentType: python-pro`, `model: sonnet` — the analyzer-wiring seam is the trickiest correctness point in the whole plan; needs to match the spec's "Analyzer wiring" section precisely.

**Files:**

- Create: `src/documents/search/_registry.py`
- Test: `src/documents/tests/search/test_registry.py`

**Interfaces:**

- Consumes: `PUBLIC_FIELDS` from `documents.search._fields` (Task 1); `_paperless_text`, `ascii_fold` from `documents.search._tokenizer` (existing module — `_paperless_text` is currently private/unexported, this task must export it, see Step 3).
- Produces: `get_field_registry(language: str | None) -> whoosh_compat.FieldRegistry` — consumed by Task 10 (`_query.py`'s `parse_user_query`).

- [ ] **Step 1: Write the failing test**

```python
# src/documents/tests/search/test_registry.py
from whoosh_compat import FieldKind

from documents.search._registry import get_field_registry


class TestFieldRegistry:
    def test_internal_id_fields_are_not_registered(self) -> None:
        registry = get_field_registry(None)
        for name in ("tag_id", "owner_id", "viewer_id", "correspondent_id",
                     "document_type_id", "storage_path_id", "viewer_group_id"):
            assert name not in registry

    def test_type_alias_resolves_to_document_type(self) -> None:
        registry = get_field_registry(None)
        ref = registry.make_ref("type")
        assert ref is not None
        resolved = registry.resolve(ref)
        assert resolved is not None
        assert resolved.spec.name == "document_type"

    def test_path_alias_resolves_to_storage_path(self) -> None:
        registry = get_field_registry(None)
        ref = registry.make_ref("path")
        assert ref is not None
        resolved = registry.resolve(ref)
        assert resolved is not None
        assert resolved.spec.name == "storage_path"

    def test_notes_json_subpaths_resolve(self) -> None:
        registry = get_field_registry(None)
        ref = registry.make_ref("notes.user")
        assert ref is not None
        resolved = registry.resolve(ref)
        assert resolved is not None
        assert resolved.spec.name == "notes"
        assert resolved.json_path == "user"
        assert resolved.is_subpath is True

    def test_custom_fields_json_subpaths_resolve(self) -> None:
        registry = get_field_registry(None)
        for raw in ("custom_fields.name", "custom_fields.value"):
            ref = registry.make_ref(raw)
            assert ref is not None
            assert registry.resolve(ref) is not None

    def test_unregistered_json_subpath_does_not_resolve(self) -> None:
        registry = get_field_registry(None)
        # An unregistered subpath is not even a valid FieldRef: make_ref
        # returns None for a dotted name whose subpath isn't registered
        # (it doesn't produce a ref for resolve() to then reject).
        assert registry.make_ref("notes.bogus") is None

    def test_tag_is_comma_values(self) -> None:
        registry = get_field_registry(None)
        ref = registry.make_ref("tag")
        assert ref is not None
        resolved = registry.resolve(ref)
        assert resolved is not None
        assert resolved.spec.comma_values is True

    def test_created_is_date_kind(self) -> None:
        registry = get_field_registry(None)
        ref = registry.make_ref("created")
        assert ref is not None
        resolved = registry.resolve(ref)
        assert resolved is not None
        assert resolved.spec.kind is FieldKind.DATE
        assert resolved.spec.date_only is True

    def test_analyzer_lowercases_and_ascii_folds(self) -> None:
        # title uses the paperless_text analyzer: simple -> remove_long ->
        # lowercase -> ascii_fold [-> stemmer]. With no language configured
        # (None), no stemmer runs, so "Café" folds to the single token "cafe".
        registry = get_field_registry(None)
        ref = registry.make_ref("title")
        assert ref is not None
        resolved = registry.resolve(ref)
        assert resolved is not None
        assert resolved.spec.analyzer is not None
        assert resolved.spec.analyzer("Café") == ["cafe"]

    def test_checksum_analyzer_is_identity_single_token(self) -> None:
        # checksum uses the raw tokenizer at index time (no splitting).
        registry = get_field_registry(None)
        ref = registry.make_ref("checksum")
        assert ref is not None
        resolved = registry.resolve(ref)
        assert resolved is not None
        assert resolved.spec.analyzer("ABC-123") == ["ABC-123"]

    def test_pattern_normalizer_is_ascii_fold_only_no_stemming(self) -> None:
        registry = get_field_registry(None)
        ref = registry.make_ref("title")
        assert ref is not None
        resolved = registry.resolve(ref)
        assert resolved is not None
        assert resolved.spec.pattern_normalizer is not None
        # "running" must NOT be stemmed to "run" by the pattern normalizer,
        # only case/accent-folded — even with English stemming configured.
        registry_en = get_field_registry("en")
        ref_en = registry_en.make_ref("title")
        assert ref_en is not None
        resolved_en = registry_en.resolve(ref_en)
        assert resolved_en is not None
        assert resolved_en.spec.pattern_normalizer("Running") == "running"

    def test_registry_is_cached_per_language(self) -> None:
        a = get_field_registry("en")
        b = get_field_registry("en")
        assert a is b

    def test_registry_rebuilds_on_language_change(self) -> None:
        a = get_field_registry("en")
        b = get_field_registry("de")
        assert a is not b
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src && uv run pytest documents/tests/search/test_registry.py -v --override-ini="addopts="`
Expected: FAIL with `ModuleNotFoundError: No module named 'documents.search._registry'`

- [ ] **Step 3: Export `_paperless_text` from `_tokenizer.py`**

`_paperless_text(language: str | None) -> tantivy.TextAnalyzer` already exists in `src/documents/search/_tokenizer.py:82` but is private (leading underscore, not imported elsewhere outside the module). Rename it to a public name so `_registry.py` can import it — this is the only change to `_tokenizer.py` in this task:

In `src/documents/search/_tokenizer.py`, rename `_paperless_text` to `paperless_text_analyzer` (both the `def` and its one call site inside `register_tokenizers()`, at line 74: `index.register_tokenizer("paperless_text", _paperless_text(language))` becomes `index.register_tokenizer("paperless_text", paperless_text_analyzer(language))`).

- [ ] **Step 4: Write minimal implementation**

```python
# src/documents/search/_registry.py
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
                pattern_normalizer=ascii_fold,
                date_only=field.date_only,
                fast=field.fast,
                subpaths=field.subpaths,
            ),
        )

    registry = FieldRegistry(specs)
    _registry_cache[language] = registry
    return registry
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd src && uv run pytest documents/tests/search/test_registry.py -v --override-ini="addopts="`
Expected: PASS (13 tests)

Also confirm the rename in `_tokenizer.py` didn't break anything:

Run: `cd src && uv run pytest documents/tests/search/test_tokenizer.py documents/tests/search/test_backend.py -v --override-ini="addopts="`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/documents/search/_registry.py src/documents/search/_tokenizer.py src/documents/tests/search/test_registry.py
git commit -m "feat(search): add whoosh-compat FieldRegistry construction"
```

---

### Task 5: JSON dict-key coupling test

**Suggested executor:** `agentType: python-pro`, `model: haiku` — small, mechanical, single assertion pair.

**Files:**

- Test: `src/documents/tests/search/test_registry.py` (extend from Task 4)

**Interfaces:**

- Consumes: `PUBLIC_FIELDS` from `documents.search._fields`; reads `_build_tantivy_doc`'s source in `documents.search._backend` (does not import/execute the JSON-building logic — asserts against the literal key sets, see below).

- [ ] **Step 1: Write the failing test**

This test guards against the `notes`/`custom_fields` JSON keys used in `_backend.py::_build_tantivy_doc` (write side) silently drifting from `PUBLIC_FIELDS`' `subpaths` (query side) — the two are hand-written independently (see spec's "JSON document population stays separate from `subpaths`"), so nothing else catches this class of bug.

```python
# appended to src/documents/tests/search/test_registry.py
class TestJsonSubpathCoupling:
    def test_notes_dict_keys_match_public_fields_subpaths(self) -> None:
        # _backend.py's _build_tantivy_doc builds:
        #   doc.add_json("notes", {"note": ..., "user": ...})
        # These literal keys must match PUBLIC_FIELDS' "notes" subpaths exactly.
        notes_field = next(f for f in PUBLIC_FIELDS if f.name == "notes")
        assert set(notes_field.subpaths) == {"note", "user"}

    def test_custom_fields_dict_keys_match_public_fields_subpaths(self) -> None:
        # _backend.py's _build_tantivy_doc builds:
        #   doc.add_json("custom_fields", {"name": ..., "value": ...})
        custom_fields_field = next(f for f in PUBLIC_FIELDS if f.name == "custom_fields")
        assert set(custom_fields_field.subpaths) == {"name", "value"}
```

(This is a deliberately literal, low-tech assertion — hardcoding the expected key sets rather than importing `_backend.py` and introspecting it, since the dict keys in `_build_tantivy_doc` are string literals inside a method body with no independent symbol to import. If a future change to `_build_tantivy_doc`'s JSON keys isn't mirrored here, this test still passes falsely — note that limit in the test's docstring rather than treating it as solved.)

Add a one-line docstring to the class making that limitation explicit:

```python
class TestJsonSubpathCoupling:
    """Guards PUBLIC_FIELDS' JSON subpaths against drifting from the literal
    dict keys _backend.py::_build_tantivy_doc writes. These assertions
    hardcode the expected key sets rather than introspecting _build_tantivy_doc
    (its dict keys are string literals with no importable symbol) — if someone
    changes _build_tantivy_doc's JSON keys without updating this test too, it
    will pass despite the drift. Best-effort, not a structural guarantee.
    """
```

- [ ] **Step 2: Run test to verify it fails**

These will actually PASS immediately since `PUBLIC_FIELDS` was already authored correctly in Task 1 — that's fine. Run: `cd src && uv run pytest documents/tests/search/test_registry.py::TestJsonSubpathCoupling -v --override-ini="addopts="` and confirm PASS. This task exists to add the regression guard, not to fix a currently-broken behavior — skip Steps 3-4's "write code to make it pass" (there is no implementation change) and proceed to commit.

- [ ] **Step 3: Commit**

```bash
git add src/documents/tests/search/test_registry.py
git commit -m "test(search): guard JSON subpath/dict-key coupling between _fields.py and _backend.py"
```

---

## Phase C — PR 3: Date-grammar parity audit

### Task 6: Write `test_date_grammar_parity.py`

**Suggested executor:** `agentType: python-pro`, `model: sonnet` — needs care transcribing every keyword/unit exactly, and reading the grammar closely enough to phrase each case correctly.

**Files:**

- Create: `src/documents/tests/search/test_date_grammar_parity.py` (transitional — deleted in Task 14)

**Interfaces:**

- Consumes: `_DATE_KEYWORDS` from `documents.search._dates`; `_UNIT_ALIASES` from `documents.search._translate` (both still present at this point in the plan — deleted only in Task 14); `get_field_registry` from `documents.search._registry` (Task 4); `wc.parse` from `whoosh_compat`.

- [ ] **Step 1: Write the test**

This test has no "make it pass" implementation step of its own — it's a coverage audit against a system that already exists. Read `src/documents/search/_dates.py` and `src/documents/search/_translate.py` in full before writing this (both already read earlier in this session) to transcribe every accepted keyword/unit exactly — do not paraphrase or abbreviate the list. Every case here asks only "does whoosh-compat accept this input at all" (the real migration-safety question — an existing saved view must not start failing to parse); it never asserts on the bounds or AST shape whoosh-compat parses a keyword to, since that's whoosh-compat's own differential-testing responsibility against a real whoosh oracle, not paperless's to re-verify against the legacy code being deleted.

```python
# src/documents/tests/search/test_date_grammar_parity.py
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
    assert result.diagnostics == (), f"{token!r} produced diagnostics: {result.diagnostics}"


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
```

Coverage only, deliberately: each test above asks "does whoosh-compat
accept this keyword/form at all" (the real migration-safety question —
does an existing saved view stop parsing), never "does it compute the
same bounds/AST shape whoosh-compat would compute on its own." The
latter is whoosh-compat's own differential-testing responsibility
against a real whoosh oracle (`tests/differential/`,
`tests/test_parser_dates.py` in that repo), not something to re-verify
here against `_translate.py` as a second, weaker oracle. If a specific
keyword's actual search _behavior_ needs confidence beyond "it parses,"
express that as a real-document, matched-ID case in the Task 12
acceptance corpus instead.

- [ ] **Step 2: Run the test suite and record results**

Run: `cd src && uv run pytest documents/tests/search/test_date_grammar_parity.py -v --override-ini="addopts="`

- [ ] **Step 3: Commit (regardless of pass/fail — see Task 7 for the fix loop)**

```bash
git add src/documents/tests/search/test_date_grammar_parity.py
git commit -m "test(search): add transitional date-grammar parity audit against whoosh-compat"
```

---

### Task 7: Resolve any parity gaps

**Suggested executor:** `agentType: python-pro`, `model: sonnet` — diagnosing a grammar mismatch requires reading whoosh-compat's `dateparse.py` grammar definitions directly.

**Files:**

- Modify (only if gaps found): files under `/tank/users/trenton/projects/paperless/whoosh-compat/src/whoosh_compat/parser/` (a **different repo**, not this one — no paperless-ngx file changes in this task unless the test itself needs a correction)

**Interfaces:**

- N/A — this task's deliverable is "Task 6's test suite is fully green," achieved either by fixing a real gap upstream or by correcting a mistaken assumption in the test itself.

- [ ] **Step 1: Triage each failure from Task 6**

For each failing case, determine which side is wrong:

- If whoosh-compat genuinely doesn't accept a keyword/unit/form paperless's legacy code accepts: this is a real grammar gap. Read `whoosh-compat/src/whoosh_compat/parser/dateparse.py`'s grammar definitions (the `English` class and its `Sequence`/`Combo`/`Choice`/`Bag`/`Regex` elements) to find where the missing vocabulary needs to be added, following the existing pattern for similar keywords.
- If a specific keyword's _bounds_ differ (e.g. a timezone or off-by-one-day mismatch) rather than a missing-diagnostic failure: check whether it's whoosh-compat DIVERGENCES.md entry 12 (the real-Whoosh date-range timezone bug, intentionally not reproduced) before assuming it's a defect — the legacy `_translate.py` oracle may itself be the side that's "wrong" relative to intended behavior in that specific documented case.

- [ ] **Step 2: For each real gap, fix it in the whoosh-compat repo**

Working directory: `/tank/users/trenton/projects/paperless/whoosh-compat`. Follow that repo's own test conventions (`tests/` — unit tests for the grammar addition, differential tests against real Whoosh if the gap is grammar-shaped). This is a separate repo with its own git history — commit there, not in paperless-ngx.

Since paperless-ngx's `pyproject.toml` uses `path = "../whoosh-compat"`, any fix committed in the whoosh-compat checkout is picked up by paperless-ngx's next `uv sync`/test run automatically — no version bump or re-pin needed mid-plan.

- [ ] **Step 3: Re-run Task 6's suite until green**

Run: `cd src && uv run pytest documents/tests/search/test_date_grammar_parity.py -v --override-ini="addopts="`
Expected: PASS, all cases.

- [ ] **Step 4: Commit any paperless-ngx-side test corrections**

Only if Step 1 found test-side mistakes (not upstream gaps):

```bash
git add src/documents/tests/search/test_date_grammar_parity.py
git commit -m "test(search): fix date-grammar parity test assumptions"
```

If no paperless-ngx changes were needed (all fixes were upstream in whoosh-compat), skip this step — there's nothing to commit here.

---

## Phase D — PR 4: Wire it in, delete the old path

### Task 8: Add `InvalidNumberQuery` and `MultipleSearchQueryErrors`; move exceptions to `_query.py`

**Suggested executor:** `agentType: python-pro`, `model: haiku` — mechanical move plus two small, fully-specified classes.

**Files:**

- Modify: `src/documents/search/_query.py` (add exception classes near the top)
- Modify: `src/documents/search/__init__.py` (update imports/`__all__`)
- Test: `src/documents/tests/search/test_query.py` (extend)

**Interfaces:**

- Produces: `SearchQueryError`, `InvalidDateQuery`, `InvalidNumberQuery`, `MultipleSearchQueryErrors` — all now defined in `documents.search._query` instead of `documents.search._translate`. Consumed by Task 9 (`parse_user_query`), Task 10 (`views.py`), Task 12 (acceptance tests).

- [ ] **Step 1: Write the failing test**

```python
# appended to src/documents/tests/search/test_query.py
from documents.search._query import InvalidDateQuery
from documents.search._query import InvalidNumberQuery
from documents.search._query import MultipleSearchQueryErrors
from documents.search._query import SearchQueryError


class TestSearchQueryErrors:
    def test_invalid_date_query_is_a_search_query_error(self) -> None:
        err = InvalidDateQuery("created", "notadate")
        assert isinstance(err, SearchQueryError)
        assert err.field == "created"
        assert err.value == "notadate"
        assert "created" in str(err)
        assert "notadate" in str(err)

    def test_invalid_number_query_is_a_search_query_error(self) -> None:
        err = InvalidNumberQuery("asn", "notanumber")
        assert isinstance(err, SearchQueryError)
        assert err.field == "asn"
        assert err.value == "notanumber"
        assert "asn" in str(err)
        assert "notanumber" in str(err)

    def test_multiple_search_query_errors_aggregates(self) -> None:
        sub_errors = [
            InvalidDateQuery("created", "notadate"),
            InvalidNumberQuery("asn", "notanumber"),
        ]
        err = MultipleSearchQueryErrors(sub_errors)
        assert isinstance(err, SearchQueryError)
        assert err.errors == tuple(sub_errors)
        assert "created" in str(err)
        assert "asn" in str(err)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src && uv run pytest documents/tests/search/test_query.py::TestSearchQueryErrors -v --override-ini="addopts="`
Expected: FAIL — `ImportError: cannot import name 'InvalidNumberQuery'` (and `MultipleSearchQueryErrors`; `SearchQueryError`/`InvalidDateQuery` currently live in `_translate.py`, not `_query.py`, so this also fails on that import today)

- [ ] **Step 3: Move and extend the exception classes**

Move `SearchQueryError` and `InvalidDateQuery` from `_translate.py` (lines 325-341 today) into `_query.py`, near the top of the file (after imports, before `logger = ...`), and add the two new classes:

```python
# added near the top of src/documents/search/_query.py
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
```

Do NOT delete `_translate.py`'s copies yet in this task — leave `_translate.py`'s `SearchQueryError`/`InvalidDateQuery` in place for now (still imported by `_dates.py`'s callers and `_translate.py` itself internally); Task 14 deletes the whole module. To avoid two divergent definitions existing simultaneously, change `_translate.py`'s two class definitions into re-exports of the new location instead of a second definition:

```python
# in src/documents/search/_translate.py, replace the class definitions
# (the block currently at lines ~325-341) with:
from documents.search._query import InvalidDateQuery  # noqa: F401 (re-exported for existing importers until Task 14)
from documents.search._query import SearchQueryError  # noqa: F401
```

Move this replacement to right after `_translate.py`'s existing import block (it needs to come after `_translate.py`'s own imports, before its first use of `SearchQueryError`/`InvalidDateQuery`, e.g. `translate_scalar`'s use of `InvalidDateQuery` at line 359).

- [ ] **Step 4: Update `__init__.py`**

```python
# src/documents/search/__init__.py
from documents.search._backend import SearchHit
from documents.search._backend import SearchIndexLockError
from documents.search._backend import SearchMode
from documents.search._backend import TantivyBackend
from documents.search._backend import TantivyRelevanceList
from documents.search._backend import WriteBatch
from documents.search._backend import get_backend
from documents.search._backend import reset_backend
from documents.search._query import InvalidDateQuery
from documents.search._query import InvalidNumberQuery
from documents.search._query import MultipleSearchQueryErrors
from documents.search._query import SearchQueryError
from documents.search._schema import needs_rebuild
from documents.search._schema import wipe_index

__all__ = [
    "InvalidDateQuery",
    "InvalidNumberQuery",
    "MultipleSearchQueryErrors",
    "SearchHit",
    "SearchIndexLockError",
    "SearchMode",
    "SearchQueryError",
    "TantivyBackend",
    "TantivyRelevanceList",
    "WriteBatch",
    "get_backend",
    "needs_rebuild",
    "reset_backend",
    "wipe_index",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd src && uv run pytest documents/tests/search/test_query.py::TestSearchQueryErrors documents/tests/search/test_translate.py -v --override-ini="addopts="`
Expected: PASS — including `test_translate.py`'s existing tests, unaffected since `InvalidDateQuery`/`SearchQueryError` behave identically (same classes, just imported from a new home via re-export).

Run the full search suite:

Run: `cd src && uv run pytest documents/tests/search/ documents/tests/test_api_search.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/documents/search/_query.py src/documents/search/_translate.py src/documents/search/__init__.py src/documents/tests/search/test_query.py
git commit -m "refactor(search): move SearchQueryError family to _query.py, add InvalidNumberQuery/MultipleSearchQueryErrors"
```

---

### Task 9: Confirm `Diagnostic.field`/`raw_value` shape before wiring error mapping

**Suggested executor:** `agentType: general-purpose`, `model: haiku` — a status/shape check, not implementation.

**Files:** none in paperless-ngx; read-only check against the whoosh-compat checkout.

**Interfaces:** N/A — gate for Task 10.

Task 10's error-mapping code needs to match `Diagnostic`'s actual field
shapes exactly, since `field` is a `FieldRef`, not a plain `str | None`.
Confirm both `Diagnostic`'s fields and `DiagnosticKind`'s members directly
against the checkout before writing that code.

- [ ] **Step 1: Confirm `Diagnostic`'s fields and `field`'s type**

Run: `cd src && uv run python -c "
from whoosh_compat.errors import Diagnostic
import dataclasses
fields = {f.name: f.type for f in dataclasses.fields(Diagnostic)}
print(fields)
"`
Expected: a `field` key present. `Diagnostic.field` is typed `FieldRef | None`, not `str | None` — Task 10's `_single_diagnostic_to_error` must call `str(d.field)` (or `d.field.name`) to get a name, never pass the `FieldRef` straight into `InvalidDateQuery`/`InvalidNumberQuery`, whose own constructors expect `str | None`.

- [ ] **Step 2: Confirm `DiagnosticKind`'s members**

Run: `cd src && uv run python -c "from whoosh_compat.errors import DiagnosticKind; print(list(DiagnosticKind))"`
Expected: `BAD_DATE`, `BAD_NUMBER`, `TOO_DEEP`, `UNSUPPORTED_PATTERN`. Task 10's `_single_diagnostic_to_error` branches on `BAD_DATE`/`BAD_NUMBER` and falls through to a generic `SearchQueryError(d.message)` for the other two — confirm that fallthrough is still adequate (or add typed handling) now that `UNSUPPORTED_PATTERN` is reachable for a wildcard on `asn`/`page_count`/`num_notes` or on `custom_fields.value`/`notes.user`-shaped subpaths.

No commit in paperless-ngx for this task — it's a read-only confirmation gating Task 10.

---

### Task 10: Rewrite `parse_user_query()`

**Suggested executor:** `agentType: python-pro`, `model: sonnet` — the core logic change of this entire plan; highest correctness stakes.

**Files:**

- Modify: `src/documents/search/_query.py:176-253` (the `parse_user_query()` function; `_diagnostics_to_error`/`_single_diagnostic_to_error` are new helper functions in the same file)
- Test: `src/documents/tests/search/test_query.py` (existing `TestParseUserQuery` class — read it in full before starting, see design spec's testing notes on what folds into Task 12's acceptance module vs. what's checked here)

**Interfaces:**

- Consumes: `get_field_registry` (Task 4, `_registry.py`); `SearchQueryError`/`InvalidDateQuery`/`InvalidNumberQuery`/`MultipleSearchQueryErrors` (Task 8, now in this same file); `wc.parse`, `whoosh_compat.emitters.tantivy_.emit`, `whoosh_compat.errors.Diagnostic`, `whoosh_compat.errors.DiagnosticKind`, `whoosh_compat.errors.UnsupportedQueryError` from the `whoosh_compat` package.
- Produces: `parse_user_query(index, raw_query, tz) -> tantivy.Query` — signature unchanged, callers in `_backend.py` need no changes.

- [ ] **Step 1: Write the failing tests**

Add these to `test_query.py`'s existing `TestParseUserQuery` class (do not remove the class's existing tests yet — this task's job is to make the rewritten `parse_user_query` satisfy both the old behavioral tests AND these new ones; Task 12 later curates which of the old ones move into the permanent acceptance module):

```python
# added to TestParseUserQuery in src/documents/tests/search/test_query.py
from documents.search._query import InvalidNumberQuery
from documents.search._query import MultipleSearchQueryErrors


class TestParseUserQuery:
    # ... existing fixture and tests stay ...

    def test_invalid_number_raises_invalid_number_query(
        self,
        query_index: tantivy.Index,
    ) -> None:
        with pytest.raises(InvalidNumberQuery) as exc_info:
            parse_user_query(query_index, "asn:notanumber", UTC)
        assert exc_info.value.field == "asn"
        assert exc_info.value.value == "notanumber"

    def test_multiple_bad_fields_raise_multiple_search_query_errors(
        self,
        query_index: tantivy.Index,
    ) -> None:
        with pytest.raises(MultipleSearchQueryErrors) as exc_info:
            parse_user_query(
                query_index,
                "created:notadate AND asn:notanumber",
                UTC,
            )
        assert len(exc_info.value.errors) == 2
        kinds = {type(e) for e in exc_info.value.errors}
        assert kinds == {InvalidDateQuery, InvalidNumberQuery}

    def test_document_type_query_via_type_alias_matches(
        self,
        query_index: tantivy.Index,
    ) -> None:
        # Field alias handling now goes through the FieldRegistry, not
        # FIELD_ALIASES string substitution — prove it still resolves.
        q = parse_user_query(query_index, "type:invoice", UTC)
        assert isinstance(q, tantivy.Query)

    def test_asn_field_is_query_addressable(
        self,
        query_index: tantivy.Index,
    ) -> None:
        q = parse_user_query(query_index, "asn:42", UTC)
        assert isinstance(q, tantivy.Query)

    def test_checksum_field_is_query_addressable(
        self,
        query_index: tantivy.Index,
    ) -> None:
        q = parse_user_query(query_index, "checksum:abc123", UTC)
        assert isinstance(q, tantivy.Query)

    def test_unregistered_id_field_folds_to_literal_text_not_error(
        self,
        query_index: tantivy.Index,
    ) -> None:
        # tag_id is intentionally excluded from the FieldRegistry — whoosh-compat
        # parity leniency folds it into literal text, not a diagnostic/400.
        q = parse_user_query(query_index, "tag_id:5", UTC)
        assert isinstance(q, tantivy.Query)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src && uv run pytest documents/tests/search/test_query.py::TestParseUserQuery -v --override-ini="addopts="`
Expected: FAIL on the six new tests (`InvalidNumberQuery`/`MultipleSearchQueryErrors` behavior doesn't exist yet in `parse_user_query`).

- [ ] **Step 3: Rewrite `parse_user_query()`**

Replace `src/documents/search/_query.py`'s current `parse_user_query()` body (lines 176-253) and the module's imports at the top:

```python
# imports to add near the top of src/documents/search/_query.py
import whoosh_compat as wc
from whoosh_compat.errors import Diagnostic
from whoosh_compat.errors import DiagnosticKind
from whoosh_compat.errors import UnsupportedQueryError
from whoosh_compat.emitters.tantivy_ import emit as tantivy_emit

from documents.search._registry import get_field_registry
```

Remove the now-unused imports of `SearchQueryError`/`translate_query` from `documents.search._translate` (they were imported at the top of `_query.py` today; `SearchQueryError` is now defined in this same file per Task 8, and `translate_query` is no longer called).

```python
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
       secondary clause.
    5. Optional CJK bigram clause — unchanged from before this migration,
       never went through the old translate_query() either.
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
        fuzzy = index.parse_query(
            raw_query,
            DEFAULT_SEARCH_FIELDS,
            field_boosts=_FIELD_BOOSTS,
            fuzzy_fields={f: (True, 1, True) for f in DEFAULT_SEARCH_FIELDS},
        )
        clauses.append((tantivy.Occur.Should, tantivy.Query.boost_query(fuzzy, 0.1)))

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
    # the generic message; see Task 9's note on whether either warrants its
    # own typed subclass.
    return SearchQueryError(d.message)
```

Note `raw_query` is passed to `index.parse_query(...)` for the fuzzy blend exactly as `query_str` (the translated string) was before — the only change is the source string (`raw_query` instead of a translated one), consistent with the design spec's explicit decision on fuzzy handling.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src && uv run pytest documents/tests/search/test_query.py::TestParseUserQuery -v --override-ini="addopts="`
Expected: PASS on all tests in the class — both the pre-existing ones (still exercising real behavior, now via the new code path) and the six new ones from Step 1. If any pre-existing test fails, read it carefully: some (e.g. ones directly asserting on `translate_query`'s intermediate string output, if any crept into this class) may need updating to assert on the `tantivy.Query`/exception result instead — that's expected per the design spec's test-migration plan, not a regression to chase blindly.

Run the full search + API test suite:

Run: `cd src && uv run pytest documents/tests/search/ documents/tests/test_api_search.py -v`
Expected: PASS. `test_api_search.py` failures here are meaningful — read Task 12's notes on why before assuming they're pre-existing/unrelated.

- [ ] **Step 5: Commit**

```bash
git add src/documents/search/_query.py src/documents/tests/search/test_query.py
git commit -m "feat(search): route parse_user_query through whoosh-compat"
```

---

### Task 11: Update `views.py` for multi-error responses

**Suggested executor:** `agentType: django-developer`, `model: haiku` — small, DRF-focused, fully specified.

**Files:**

- Modify: `src/documents/views.py:2560-2564` (the `except SearchQueryError as e:` handler in `UnifiedSearchViewSet.list()`)
- Test: `src/documents/tests/test_api_search.py` (extend — see Task 13 for the fuller expansion; this task adds just the one handler-level test)

**Interfaces:**

- Consumes: `MultipleSearchQueryErrors` from `documents.search` (Task 8).

- [ ] **Step 1: Write the failing test**

Read `src/documents/tests/test_api_search.py`'s existing `test_search_added_invalid_date` test first (around line 765) to match its fixture/request style exactly.

```python
# appended to TestDocumentSearchApi in src/documents/tests/test_api_search.py
def test_search_multiple_bad_fields_returns_all_messages(self) -> None:
    response = self.client.get(
        "/api/documents/",
        {"query": "created:notadate AND asn:notanumber"},
    )
    self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
    messages = response.data["query"]
    self.assertEqual(len(messages), 2)
    self.assertTrue(any("created" in m for m in messages))
    self.assertTrue(any("asn" in m for m in messages))
```

(Match the exact import/style already used at the top of the file for
`status` — check whether it's already imported as `from rest_framework import
status` before adding a duplicate import.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src && uv run pytest documents/tests/test_api_search.py::TestDocumentSearchApi::test_search_multiple_bad_fields_returns_all_messages -v --override-ini="addopts="`
Expected: FAIL — today's handler only ever puts one message in the list (`[str(e)]`), so with two bad fields, `len(messages) == 2` fails against the single-error message today (note: this only fails once Task 10 has landed and `MultipleSearchQueryErrors` can actually be raised end-to-end; if Task 10 isn't done yet, this fails at test collection with an import path issue — do Task 10 first).

- [ ] **Step 3: Update the exception handler**

In `src/documents/views.py`, the existing block (around line 2560):

```python
        except SearchQueryError as e:
            # User-fixable query error (e.g. an unparsable date): surface the
            # specific message so the user can correct it, rather than a generic
            # 400 or silently empty results.
            raise ValidationError({"query": [str(e)]}) from e
```

becomes:

```python
        except SearchQueryError as e:
            # User-fixable query error(s) (e.g. unparsable dates/numbers):
            # surface every offending field's message, not just the first,
            # so the user can fix them all in one round-trip.
            from documents.search import MultipleSearchQueryErrors

            messages = (
                [str(sub) for sub in e.errors]
                if isinstance(e, MultipleSearchQueryErrors)
                else [str(e)]
            )
            raise ValidationError({"query": messages}) from e
```

(The local import matches the existing style in this method — `SearchHit`, `SearchQueryError`, `TantivyBackend`, etc. are all imported locally inside `list()` a few lines above, at line 2366-2370, rather than at module scope; follow that pattern for consistency rather than moving it to the top of the file.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd src && uv run pytest documents/tests/test_api_search.py::TestDocumentSearchApi::test_search_multiple_bad_fields_returns_all_messages documents/tests/test_api_search.py::TestDocumentSearchApi::test_search_added_invalid_date -v --override-ini="addopts="`
Expected: PASS on both (the pre-existing single-error test must still pass — `[str(e)]` for a plain `InvalidDateQuery` is unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/documents/views.py src/documents/tests/test_api_search.py
git commit -m "feat(api): surface every search query error, not just the first"
```

---

### Task 12: Result-level acceptance corpus

**Suggested executor:** `agentType: python-pro`, `model: sonnet` — the highest-value correctness gate in the plan; needs to faithfully port `TestParseUserQuery`'s existing cases plus the new corpus the spec calls for.

**Files:**

- Create: `src/documents/tests/search/test_acceptance.py`
- Modify: `src/documents/tests/search/test_query.py` (remove the internals-only classes — see Step 4)

**Interfaces:**

- Consumes: `parse_user_query` (Task 10), `get_field_registry` (Task 4), `build_schema`/`register_tokenizers` (existing).

- [ ] **Step 1: Write the acceptance module**

This ports `TestParseUserQuery`'s existing result-level cases (already read in full during Task 10) plus the corpus the design spec calls for: the issue #13568 bracket-wildcard query, comma lists, boosts, JSON subpaths, and the one `Multitoken`-nested-in-`OR` case.

`src/documents/tests/search/conftest.py` already provides a `backend` fixture (in-memory `TantivyBackend`, opened/closed per test) and an `index` fixture (module-scoped raw `tantivy.Index`) — this file reuses `backend` rather than redefining it. Document/note/custom-field creation follows the exact pattern already used throughout `test_api_search.py` (verified there, e.g. `test_search` at line 50, `test_search_custom_field_ordering` at line 290, and the `Note.objects.create` block around line 1595): plain `Document.objects.create(...)`/`Note.objects.create(...)`/`CustomField.objects.create(...)`/`CustomFieldInstance.objects.create(...)` Django ORM calls, then `backend.add_or_update(doc)` (there is no `document_factory`/`note_factory` pytest-fixture layer in this codebase for search tests — `documents/tests/factories.py`'s `DocumentFactory` exists but isn't used by any existing search test, so this file matches the pattern that actually is used rather than introducing a new one). `CustomFieldInstance` stores typed values per `CustomField.FieldDataType` — a `STRING`-type field's value lives in `value_text`, not a generic `value` column (confirmed in `documents/models.py`'s `CustomFieldInstance._VALUE_FIELDS_BY_DATA_TYPE` mapping).

```python
# src/documents/tests/search/test_acceptance.py
"""Result-level acceptance corpus: real documents indexed via build_schema(),
real queries run through parse_user_query(), matched-document-ID sets
asserted — not intermediate ASTs or query strings. This is paperless-ngx's
analogue of whoosh-compat's own tests/emitter/test_acceptance_e2e.py.

Supersedes test_query.py's TestParseUserQuery result-level cases and the
now-deleted test_date_grammar_parity.py (Task 14).
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

import pytest
import time_machine

from documents.models import CustomField
from documents.models import CustomFieldInstance
from documents.models import Document
from documents.models import Note
from documents.search._backend import TantivyBackend
from documents.search._query import parse_user_query

pytestmark = [pytest.mark.search, pytest.mark.django_db]

FROZEN_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


def _matched_ids(backend: TantivyBackend, query: str) -> set[int]:
    return set(backend.search_ids(query, user=None))


@pytest.fixture
def indexed_documents(backend: TantivyBackend) -> dict[str, int]:
    """Index a small fixture set, return {label: doc_id} for corpus queries."""
    docs = {
        "invoice_2020": Document.objects.create(
            title="Invoice 2020",
            content="invoice total due",
            checksum="acc-invoice-2020",
            archive_serial_number=100,
        ),
        "invoice_2021": Document.objects.create(
            title="Invoice 2021",
            content="invoice total due",
            checksum="acc-invoice-2021",
            archive_serial_number=101,
        ),
        "invoice_2023": Document.objects.create(
            title="Invoice 2023",
            content="invoice total due",
            checksum="acc-invoice-2023",
            archive_serial_number=102,
        ),
        "receipt_2022": Document.objects.create(
            title="Receipt 2022",
            content="receipt total due",
            checksum="acc-receipt-2022",
            archive_serial_number=103,
        ),
    }
    for doc in docs.values():
        backend.add_or_update(doc)
    return {label: doc.pk for label, doc in docs.items()}


class TestIssue13568BracketWildcard:
    """paperless-ngx#13568: title:202[0-3]* must keep its character class,
    not fold to a prefix query that silently drops it (whoosh-compat
    DIVERGENCES.md entry 13)."""

    def test_bracket_class_wildcard_matches_only_in_range_years(
        self,
        backend: TantivyBackend,
        indexed_documents: dict[str, int],
    ) -> None:
        matched = _matched_ids(backend, "title:202[0-3]*")
        expected = {
            indexed_documents["invoice_2020"],
            indexed_documents["invoice_2021"],
        }
        assert matched == expected, (
            "title:202[0-3]* must match 2020/2021 titles and exclude 2022/2023 "
            "- if this matches everything, the wildcard's character class was "
            "silently dropped (issue #13568's original bug)"
        )


class TestCommaValueLists:
    def test_tag_comma_list_matches_documents_with_either_tag(
        self,
        backend: TantivyBackend,
    ) -> None:
        doc_a = Document.objects.create(title="A", content="x", checksum="acc-comma-a")
        doc_a.tags.create(name="foo")
        doc_b = Document.objects.create(title="B", content="x", checksum="acc-comma-b")
        doc_b.tags.create(name="bar")
        doc_c = Document.objects.create(title="C", content="x", checksum="acc-comma-c")
        doc_c.tags.create(name="baz")
        for doc in (doc_a, doc_b, doc_c):
            backend.add_or_update(doc)
        matched = _matched_ids(backend, "tag:foo,bar")
        assert matched == {doc_a.pk, doc_b.pk}


class TestFieldBoosts:
    def test_title_boost_ranks_title_match_above_content_only_match(
        self,
        backend: TantivyBackend,
    ) -> None:
        title_match = Document.objects.create(
            title="urgent",
            content="nothing else relevant",
            checksum="acc-boost-title",
        )
        content_match = Document.objects.create(
            title="nothing",
            content="urgent matter here",
            checksum="acc-boost-content",
        )
        backend.add_or_update(title_match)
        backend.add_or_update(content_match)
        query = parse_user_query(backend._index, "urgent", UTC)
        searcher = backend._index.searcher()
        results = searcher.search(query, limit=10)
        ranked_ids = [
            searcher.doc(addr).to_dict()["id"][0] for _score, addr in results.hits
        ]
        assert ranked_ids[0] == title_match.pk


class TestJsonSubpaths:
    def test_notes_user_matches_document_with_that_note_author(
        self,
        backend: TantivyBackend,
    ) -> None:
        from django.contrib.auth.models import User

        alice = User.objects.create_user(username="alice")
        doc_with_note = Document.objects.create(
            title="Has note",
            content="x",
            checksum="acc-note-with",
        )
        Note.objects.create(document=doc_with_note, user=alice, note="reminder")
        doc_without = Document.objects.create(
            title="No note",
            content="x",
            checksum="acc-note-without",
        )
        backend.add_or_update(doc_with_note)
        backend.add_or_update(doc_without)
        matched = _matched_ids(backend, "notes.user:alice")
        assert matched == {doc_with_note.pk}

    def test_custom_fields_name_and_value_combine(
        self,
        backend: TantivyBackend,
    ) -> None:
        field = CustomField.objects.create(
            name="Contract Number",
            data_type=CustomField.FieldDataType.STRING,
        )
        other_field = CustomField.objects.create(
            name="Other Field",
            data_type=CustomField.FieldDataType.STRING,
        )
        matching = Document.objects.create(
            title="Matching",
            content="x",
            checksum="acc-cf-matching",
        )
        CustomFieldInstance.objects.create(document=matching, field=field, value_text="policy")
        non_matching = Document.objects.create(
            title="Non-matching",
            content="x",
            checksum="acc-cf-nonmatching",
        )
        CustomFieldInstance.objects.create(
            document=non_matching,
            field=other_field,
            value_text="policy",
        )
        backend.add_or_update(matching)
        backend.add_or_update(non_matching)
        matched = _matched_ids(
            backend,
            'custom_fields.name:"Contract Number" custom_fields.value:policy',
        )
        assert matched == {matching.pk}


class TestMultitokenInNestedOr:
    """whoosh-compat DIVERGENCES.md entry 15: Multitoken.DEFAULT resolves by
    syntactic enclosing group, not the parser's fixed default group. Prove
    it doesn't matter for paperless's actual data/fields."""

    def test_multitoken_tag_value_inside_top_level_or_matches_either_branch(
        self,
        backend: TantivyBackend,
    ) -> None:
        # "multi word tag" is a multitoken field value; nested inside a
        # top-level OR with an unrelated clause.
        doc_a = Document.objects.create(title="A", content="x", checksum="acc-mt-a")
        doc_a.tags.create(name="multi word tag")
        doc_b = Document.objects.create(title="B", content="x", checksum="acc-mt-b")
        doc_b.tags.create(name="unrelated")
        backend.add_or_update(doc_a)
        backend.add_or_update(doc_b)
        matched = _matched_ids(backend, 'tag:"multi word tag" OR title:B')
        assert matched == {doc_a.pk, doc_b.pk}
```

Not ported forward: `test_query.py`'s existing `TestParseUserQuery.test_advanced_search_queries_do_not_raise` (a parametrized `isinstance(..., tantivy.Query)` check over a handful of advanced query shapes) is dropped rather than carried into this module. It never indexes a document or asserts a matched-ID set, so keeping it here would be the one class in this file testing nothing paperless-specific — the underlying guarantee it's checking (a diagnostics-free parse never raises anything but `UnsupportedQueryError` at emit) is whoosh-compat's own tested contract, not paperless's to re-verify with generic query strings. Task 10's kept tests already cover paperless's own specific "does not raise" behaviors (fuzzy mode, dash-query robustness); every other query shape either becomes a real result-level case above or is trusted to whoosh-compat's own suite.

The module-level `pytestmark = [pytest.mark.search, pytest.mark.django_db]` (confirmed as the real convention against `test_backend.py`'s identical line) covers every class in the file.

- [ ] **Step 2: Run the acceptance suite**

Run: `cd src && uv run pytest documents/tests/search/test_acceptance.py -v --override-ini="addopts="`
Expected: PASS, all cases. If `TestIssue13568BracketWildcard` fails, that's the exact regression this whole migration exists to fix (see design spec's Summary and whoosh-compat's DIVERGENCES.md entry 13) — do not weaken the assertion to make it pass; fix the underlying wiring instead.

- [ ] **Step 3: Remove the internals-only classes from `test_query.py`**

Per the design spec's test-migration table, delete these classes entirely from `src/documents/tests/search/test_query.py` (they test `_translate.py`/`_dates.py` internals or intermediate query strings, both gone once Task 14 runs): `TestCreatedDateField`, `TestDateTimeFields`, `TestWhooshQueryRewriting`, `TestYearRangeRewriting`, `TestNonDateFieldsNotRewritten`, `TestPassthrough`, `TestNormalizeQuery`. Also remove `TestParseUserQuery`'s `test_advanced_search_queries_do_not_raise` outright — it never indexed a document or asserted a matched-ID set, checking only that a diagnostics-free parse doesn't raise, which is whoosh-compat's own tested contract, not something paperless needs to re-verify with generic query strings. Keep the rest of `TestParseUserQuery` (the exception-path tests from Task 10, `test_returns_tantivy_query`, `test_fuzzy_mode_does_not_raise`, `test_date_rewriting_applied_before_tantivy_parse`, `test_spaced_dash_queries_do_not_raise`, `test_invalid_date_propagates_not_swallowed`) — those cover paperless-specific behavior (fuzzy mode, dash-query robustness, exception propagation), not generic library coverage.

Keep `TestParseSimpleTextHighlightQuery` and `TestPermissionFilter` in `test_query.py` entirely unchanged — neither ever touched `translate_query`.

- [ ] **Step 4: Run the full search suite**

Run: `cd src && uv run pytest documents/tests/search/ documents/tests/test_api_search.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/documents/tests/search/test_acceptance.py src/documents/tests/search/test_query.py
git commit -m "test(search): add result-level acceptance corpus, trim internals-only test_query.py classes"
```

---

### Task 13: Expand `test_api_search.py` for new fields

**Suggested executor:** `agentType: django-developer`, `model: sonnet` — DRF/API-level, needs to match existing fixture conventions in a 2000-line test file.

**Files:**

- Modify: `src/documents/tests/test_api_search.py` (extend `TestDocumentSearchApi`)

**Interfaces:**

- Consumes: whatever document/fixture factories `TestDocumentSearchApi` already uses (read `setUp()`/existing tests first — do not introduce a different fixture pattern into this file).

- [ ] **Step 1: Read the existing fixture setup**

Read `src/documents/tests/test_api_search.py`'s `TestDocumentSearchApi.setUp()` and `test_search` (line 50) in full before writing new tests — `test_search` is the exact pattern to match: `Document.objects.create(...)` followed by `backend = get_backend(); backend.add_or_update(d)` for each document (there is no separate indexing helper method in this file; `get_backend`/`reset_backend` are already imported at the top from `documents.search`).

- [ ] **Step 2: Write the new tests**

```python
# appended to TestDocumentSearchApi in src/documents/tests/test_api_search.py
def test_search_by_asn(self) -> None:
    doc = Document.objects.create(
        title="Has ASN",
        content="content",
        checksum="asn-checksum",
        archive_serial_number=555,
    )
    backend = get_backend()
    backend.add_or_update(doc)
    response = self.client.get("/api/documents/", {"query": "asn:555"})
    self.assertEqual(response.status_code, status.HTTP_200_OK)
    ids = [r["id"] for r in response.data["results"]]
    self.assertIn(doc.id, ids)

def test_search_by_page_count(self) -> None:
    doc = Document.objects.create(
        title="Multi-page",
        content="content",
        checksum="page-count-checksum",
        page_count=42,
    )
    backend = get_backend()
    backend.add_or_update(doc)
    response = self.client.get("/api/documents/", {"query": "page_count:42"})
    self.assertEqual(response.status_code, status.HTTP_200_OK)
    ids = [r["id"] for r in response.data["results"]]
    self.assertIn(doc.id, ids)

def test_search_by_original_filename(self) -> None:
    doc = Document.objects.create(
        title="Named file",
        content="content",
        checksum="filename-checksum",
        original_filename="quarterly-report.pdf",
    )
    backend = get_backend()
    backend.add_or_update(doc)
    response = self.client.get(
        "/api/documents/",
        {"query": "original_filename:quarterly-report.pdf"},
    )
    self.assertEqual(response.status_code, status.HTTP_200_OK)
    ids = [r["id"] for r in response.data["results"]]
    self.assertIn(doc.id, ids)

def test_search_by_checksum(self) -> None:
    doc = Document.objects.create(
        title="Checksum doc",
        content="content",
        checksum="deadbeef1234",
    )
    backend = get_backend()
    backend.add_or_update(doc)
    response = self.client.get("/api/documents/", {"query": "checksum:deadbeef1234"})
    self.assertEqual(response.status_code, status.HTTP_200_OK)
    ids = [r["id"] for r in response.data["results"]]
    self.assertIn(doc.id, ids)
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `cd src && uv run pytest documents/tests/test_api_search.py::TestDocumentSearchApi -v -k "asn or page_count or original_filename or checksum"`
Expected: PASS on the four new tests.

Run the full file:

Run: `cd src && uv run pytest documents/tests/test_api_search.py -v`
Expected: PASS (all 47 tests: 43 existing + 1 from Task 11 + 4 from this task)

- [ ] **Step 4: Commit**

```bash
git add src/documents/tests/test_api_search.py
git commit -m "test(api): add end-to-end search coverage for asn/page_count/original_filename/checksum"
```

---

### Task 14: Delete the old translation layer

**Suggested executor:** `agentType: general-purpose`, `model: haiku` — mechanical deletion; verify with grep before removing, no design judgment.

**Files:**

- Delete: `src/documents/search/_translate.py`
- Delete: `src/documents/search/_dates.py`
- Delete: `src/documents/tests/search/test_translate.py`
- Delete: `src/documents/tests/search/test_date_grammar_parity.py`

**Interfaces:** N/A — pure removal, gated on nothing else importing these modules.

- [ ] **Step 1: Confirm nothing still imports the modules being deleted**

Run: `cd src && rg -n "from documents\.search\._translate|from documents\.search\._dates|import documents\.search\._translate|import documents\.search\._dates" --type py`

Expected: no results outside the four files being deleted themselves. If anything else shows up, stop — that import needs to be resolved (likely a leftover from Task 8's exception-class move, or a caller that still needs updating) before deleting.

- [ ] **Step 2: Delete the files**

```bash
git rm src/documents/search/_translate.py
git rm src/documents/search/_dates.py
git rm src/documents/tests/search/test_translate.py
git rm src/documents/tests/search/test_date_grammar_parity.py
```

- [ ] **Step 3: Run the full backend test suite**

Run: `cd src && uv run pytest documents/ -x`
Expected: PASS, no import errors, no collection errors.

- [ ] **Step 4: Commit**

```bash
git commit -m "refactor(search): delete _translate.py/_dates.py, superseded by whoosh-compat"
```

---

### Task 15: Update docs and changelog

**Suggested executor:** `agentType: general-purpose`, `model: haiku` — writing, mechanical, content fully specified by the design spec.

**Files:**

- Modify: `docs/usage.md` (advanced search section, around line 888-960)

**Interfaces:** N/A — documentation only.

- [ ] **Step 1: Add the five newly-documented fields**

In `docs/usage.md`, immediately after the existing "Matching specific tags, correspondents or types" example block (around line 888-889), add:

```markdown
Matching by archive metadata:
```

asn:100
page_count:12
checksum:a1b2c3d4
original_filename:invoice.pdf

```

- `asn` matches a document's Archive Serial Number.
- `page_count` matches a document's page count.
- `checksum` matches the checksum of the original document file (not the
  archived/processed version).
- `original_filename` matches the filename of the document as originally
  consumed.
```

Place this new block after the existing "Matching dates" section (after line ~897, before "Matching inexact words") so undocumented-field additions sit alongside the other field-matching examples rather than at the end of the page.

- [ ] **Step 2: Add the changelog-relevant callout**

This isn't a docs-page addition — it goes in the PR description for whichever PR merges Task 14's deletion (this task doesn't create a separate changelog file; paperless-ngx uses release-drafter/PR-title-based changelog generation per `CLAUDE.md`, not per-PR changelog fragments). Note it here for whoever writes that PR's description:

> **Behavior change**: `tag_id`, `owner_id`, `viewer_id`, `correspondent_id`, `document_type_id`, `storage_path_id`, `type_id`, and `path_id` are no longer recognized advanced-search field prefixes. A query using one of these (e.g. from an old saved view) no longer errors — it silently searches for the literal text instead, most likely returning zero results. These were never documented in `docs/usage.md`.

No file changes for this step — it's instructions for the PR author, not a commit.

- [ ] **Step 3: Verify the docs build**

Run: `cd /tank/users/trenton/projects/paperless/paperless-ngx && uv run --group docs mkdocs build --strict` (if the `docs` dependency group is set up for local builds; if this command isn't available in this environment, skip and instead visually proofread the added Markdown block for correct fence/heading nesting against the surrounding content).

- [ ] **Step 4: Commit**

```bash
git add docs/usage.md
git commit -m "docs: document asn/page_count/checksum/original_filename advanced search fields"
```

---

### Task 16: Swap the dependency pin

**Suggested executor:** `agentType: general-purpose`, `model: haiku` — mechanical pyproject/lockfile edit, condition already known by this point in the plan.

**Files:**

- Modify: `pyproject.toml`

**Interfaces:** N/A.

- [ ] **Step 1: Check whether whoosh-compat has a PyPI release**

Run: `! pip index versions whoosh-compat 2>&1 || echo "not yet released"` (via the `!` shell-passthrough, since this needs live network access this session may not have — if unavailable, ask the user directly whether the release happened, per the plan's "primary vs. fallback" split in the design spec).

- [ ] **Step 2a: If released — pin the PyPI version**

In `pyproject.toml`, change:

```toml
"whoosh-compat[tantivy]",
```

to:

```toml
"whoosh-compat[tantivy]==<the released version>",
```

and remove the `whoosh-compat = { path = "../whoosh-compat" }` entry (and its TODO comment) from `[tool.uv.sources]` entirely.

- [ ] **Step 2b: If not yet released — pin a git commit SHA**

```bash
cd /tank/users/trenton/projects/paperless/whoosh-compat && git rev-parse HEAD
```

In `pyproject.toml`, change the dependency line to:

```toml
"whoosh-compat[tantivy] @ git+https://github.com/stumpylog/whoosh-compat@<sha-from-above>",
```

and remove the `[tool.uv.sources]` path override the same way as Step 2a.

- [ ] **Step 3: Re-sync and verify**

Run: `cd src && uv sync`
Expected: resolves cleanly against the new pinned source (PyPI or git), not the local path.

Run: `cd src && uv run pytest documents/tests/search/ documents/tests/test_api_search.py -v`
Expected: PASS — proves the pinned dependency behaves identically to the local checkout used throughout this plan.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: pin whoosh-compat to a released version, drop local path dependency"
```
