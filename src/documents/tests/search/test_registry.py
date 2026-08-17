from whoosh_compat import FieldKind

from documents.search._registry import get_field_registry


class TestFieldRegistry:
    def test_internal_id_fields_are_not_registered(self) -> None:
        registry = get_field_registry(None)
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
