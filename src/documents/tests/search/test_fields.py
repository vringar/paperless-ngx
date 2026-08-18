from whoosh_compat import FieldKind

from documents.search._fields import PUBLIC_FIELDS


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
                assert not field.subpaths

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
        assert set(field.subpaths) == {"user", "note"}

    def test_custom_fields_subpaths(self) -> None:
        field = next(f for f in PUBLIC_FIELDS if f.name == "custom_fields")
        assert set(field.subpaths) == {"name", "value"}

    def test_no_internal_id_fields_present(self) -> None:
        # tag_id/owner_id/viewer_id/etc. are permission-filter-only fields,
        # never user-query-addressable (see design spec, "Field surface").
        names = {f.name for f in PUBLIC_FIELDS}
        assert not any(name.endswith("_id") for name in names)
