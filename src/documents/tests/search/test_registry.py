import pytest
from whoosh_compat import FieldKind
from whoosh_compat import FieldRegistry
from whoosh_compat.fields import ResolvedField

from documents.search._fields import PUBLIC_FIELDS
from documents.search._registry import get_field_registry

_BY_NAME = {f.name: f for f in PUBLIC_FIELDS}


@pytest.fixture
def registry() -> FieldRegistry:
    return get_field_registry(None)


def _resolve(registry: FieldRegistry, name: str) -> ResolvedField:
    ref = registry.make_ref(name)
    assert ref is not None, f"{name} is not a valid field ref"
    resolved = registry.resolve(ref)
    assert resolved is not None, f"{name} did not resolve"
    return resolved


class TestFieldRegistry:
    def test_internal_id_fields_are_not_registered(
        self,
        registry: FieldRegistry,
    ) -> None:
        for name in (
            "tag_id",
            "owner_id",
            "viewer_id",
            "correspondent_id",
            "document_type_id",
            "storage_path_id",
            "viewer_group_id",
        ):
            assert name not in registry

    def test_type_alias_resolves_to_document_type(
        self,
        registry: FieldRegistry,
    ) -> None:
        assert _resolve(registry, "type").spec.name == "document_type"

    def test_path_alias_resolves_to_storage_path(self, registry: FieldRegistry) -> None:
        assert _resolve(registry, "path").spec.name == "storage_path"

    def test_notes_json_subpaths_resolve(self, registry: FieldRegistry) -> None:
        resolved = _resolve(registry, "notes.user")
        assert resolved.spec.name == "notes"
        assert resolved.json_path == "user"
        assert resolved.is_subpath is True

    def test_custom_fields_json_subpaths_resolve(self, registry: FieldRegistry) -> None:
        for raw in ("custom_fields.name", "custom_fields.value"):
            _resolve(registry, raw)

    def test_unregistered_json_subpath_does_not_resolve(
        self,
        registry: FieldRegistry,
    ) -> None:
        # An unregistered subpath is not even a valid FieldRef: make_ref
        # returns None for a dotted name whose subpath isn't registered
        # (it doesn't produce a ref for resolve() to then reject).
        assert registry.make_ref("notes.bogus") is None

    def test_tag_is_comma_values(self, registry: FieldRegistry) -> None:
        assert _resolve(registry, "tag").spec.comma_values is True

    def test_created_is_date_kind(self, registry: FieldRegistry) -> None:
        resolved = _resolve(registry, "created")
        assert resolved.spec.kind is FieldKind.DATE
        assert resolved.spec.date_only is True

    def test_analyzer_lowercases_and_ascii_folds(self, registry: FieldRegistry) -> None:
        # title uses the paperless_text analyzer: simple -> remove_long ->
        # lowercase -> ascii_fold [-> stemmer]. With no language configured
        # (None), no stemmer runs, so "Café" folds to the single token "cafe".
        resolved = _resolve(registry, "title")
        assert resolved.spec.analyzer is not None
        assert resolved.spec.analyzer("Café") == ["cafe"]

    def test_checksum_analyzer_is_identity_single_token(
        self,
        registry: FieldRegistry,
    ) -> None:
        # checksum uses the raw tokenizer at index time (no splitting).
        resolved = _resolve(registry, "checksum")
        assert resolved.spec.analyzer is not None
        assert resolved.spec.analyzer("ABC-123") == ["ABC-123"]

    def test_pattern_normalizer_follows_the_registry_language(
        self,
        registry: FieldRegistry,
    ) -> None:
        # Index terms are stemmed, so patterns are too, using the registry's
        # own language: "Running" has to reach the indexed "run". Without a
        # language the index holds surface forms, so it only case/accent-folds.
        resolved = _resolve(registry, "title")
        assert resolved.spec.pattern_normalizer is not None
        assert resolved.spec.pattern_normalizer("Running") == "running"

        resolved_en = _resolve(get_field_registry("en"), "title")
        assert resolved_en.spec.pattern_normalizer is not None
        assert resolved_en.spec.pattern_normalizer("Running") == "run"

    def test_registry_is_cached_per_language(self) -> None:
        a = get_field_registry("en")
        b = get_field_registry("en")
        assert a is b

    def test_registry_rebuilds_on_language_change(self) -> None:
        a = get_field_registry("en")
        b = get_field_registry("de")
        assert a is not b


class TestJsonSubpathCoupling:
    """Guards PUBLIC_FIELDS' JSON subpaths against drifting from the literal
    dict keys _backend.py::_build_tantivy_doc writes. These assertions
    hardcode the expected key sets rather than introspecting _build_tantivy_doc
    (its dict keys are string literals with no importable symbol) — if someone
    changes _build_tantivy_doc's JSON keys without updating this test too, it
    will pass despite the drift. Best-effort, not a structural guarantee.
    """

    def test_notes_dict_keys_match_public_fields_subpaths(self) -> None:
        # _backend.py's _build_tantivy_doc builds:
        #   doc.add_json("notes", {"note": ..., "user": ...})
        # These literal keys must match PUBLIC_FIELDS' "notes" subpaths exactly.
        assert set(_BY_NAME["notes"].subpaths) == {"note", "user"}

    def test_custom_fields_dict_keys_match_public_fields_subpaths(self) -> None:
        # _backend.py's _build_tantivy_doc builds:
        #   doc.add_json("custom_fields", {"name": ..., "value": ...})
        assert set(_BY_NAME["custom_fields"].subpaths) == {"name", "value"}
