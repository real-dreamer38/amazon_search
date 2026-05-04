"""Cross-Border Matching Engine — API contracts (Protocol definitions)."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class VisionMatcherProtocol(Protocol):
    def compare(self, image_url_a: str, image_url_b: str) -> float:
        """Compare two product images. Returns similarity score in [0.0, 1.0]."""
        ...


@runtime_checkable
class TranslationServiceProtocol(Protocol):
    def translate_en_to_ko(self, text: str) -> str:
        """Translate an English product title to Korean."""
        ...
