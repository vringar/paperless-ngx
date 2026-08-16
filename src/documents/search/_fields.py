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
