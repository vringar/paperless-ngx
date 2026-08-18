import pytest
from whoosh_compat import FieldKind

from documents.search._fields import PUBLIC_FIELDS

BY_NAME = {f.name: f for f in PUBLIC_FIELDS}


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

    @pytest.mark.parametrize(
        ("name", "attr", "expected"),
        [
            pytest.param(
                "document_type",
                "aliases",
                ("type",),
                id="document_type-aliases",
            ),
            pytest.param(
                "storage_path",
                "aliases",
                ("path",),
                id="storage_path-aliases",
            ),
            pytest.param("tag", "comma_values", True, id="tag-comma_values"),
            pytest.param(
                "notes",
                "subpaths",
                {"user", "note"},
                id="notes-subpaths",
            ),
            pytest.param(
                "custom_fields",
                "subpaths",
                {"name", "value"},
                id="custom_fields-subpaths",
            ),
        ],
    )
    def test_field_attributes(self, name: str, attr: str, expected: object) -> None:
        actual = getattr(BY_NAME[name], attr)
        if attr == "subpaths":
            actual = set(actual)
        assert actual == expected

    def test_no_internal_id_fields_present(self) -> None:
        # tag_id/owner_id/viewer_id/etc. are permission-filter-only fields,
        # never user-query-addressable (see design spec, "Field surface").
        names = {f.name for f in PUBLIC_FIELDS}
        assert not any(name.endswith("_id") for name in names)
