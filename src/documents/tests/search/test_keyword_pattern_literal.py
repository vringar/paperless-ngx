"""Wildcard patterns on KEYWORD fields must stay literal.

``checksum`` is the only KEYWORD field: it is indexed with the raw tokenizer,
so its terms are never lowercased, folded or stemmed. Running its wildcard
patterns through the stemming normalizer rewrote hex prefixes ("ceded" ->
"cede") and returned documents whose checksum did not start with what the user
typed, which for an identity field is a wrong answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from documents.models import Document
from documents.search._registry import get_field_registry

if TYPE_CHECKING:
    from collections.abc import Callable

    from whoosh_compat import FieldRegistry

    from documents.search._backend import TantivyBackend

pytestmark = [pytest.mark.search, pytest.mark.django_db]

CEDEF00D = "cedef00ddeadbeef0123456789abcdef01234567"
CEDEDEAD = "cededeadbeef567801234567" + "89abcdef01234567"


def _normalizer(registry: FieldRegistry, name: str) -> Callable[[str], str]:
    ref = registry.make_ref(name)
    assert ref is not None
    resolved = registry.resolve(ref)
    assert resolved is not None
    assert resolved.spec.pattern_normalizer is not None
    return resolved.spec.pattern_normalizer


class TestKeywordPatternNormalizer:
    @pytest.mark.parametrize(
        "run",
        [
            pytest.param("ceded", id="stems_to_cede"),
            pytest.param("added", id="stems_to_ad"),
            pytest.param("cafed", id="stems_to_cafe"),
        ],
    )
    def test_keyword_runs_are_folded_not_stemmed(self, run: str) -> None:
        normalize = _normalizer(get_field_registry("en"), "checksum")
        assert normalize(run) == run

    def test_text_runs_are_still_stemmed(self) -> None:
        normalize = _normalizer(get_field_registry("en"), "title")
        assert normalize("Running") == "run"


class TestChecksumPrefixQueries:
    @pytest.fixture
    def indexed(self, backend: TantivyBackend) -> None:
        for i, checksum in enumerate((CEDEF00D, CEDEDEAD)):
            doc = Document.objects.create(
                title=f"Checksum doc {i}",
                content="invoices for the quarter",
                checksum=checksum,
                archive_serial_number=940 + i,
            )
            backend.add_or_update(doc)

    def _ids(self, backend: TantivyBackend, query: str) -> set[int]:
        return set(backend.search_ids(query, user=None))

    def test_prefix_matches_only_the_document_that_starts_with_it(
        self,
        backend: TantivyBackend,
        indexed: None,
    ) -> None:
        matched = self._ids(backend, "checksum:ceded*")
        expected = Document.objects.get(checksum=CEDEDEAD).pk
        assert matched == {expected}

    def test_text_prefix_still_reaches_the_stemmed_index(
        self,
        backend: TantivyBackend,
        indexed: None,
    ) -> None:
        assert len(self._ids(backend, "invoice*")) == 2
