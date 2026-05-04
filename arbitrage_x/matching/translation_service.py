"""
Cross-Border Matching Engine — Translation service providers.

MockTranslationService  : deterministic mock for unit tests (no API calls).
DeepLTranslationService : production stub via DeepL REST API.
GoogleTranslateService  : production stub via Google Cloud Translation API.

Both production stubs delegate HTTP + retry to RetryClient.
"""
from __future__ import annotations

import logging

from arbitrage_x.ingestion.base import RetryClient

logger = logging.getLogger(__name__)


class MockTranslationService:
    """Returns a fixed Korean string regardless of input — for testing only."""

    def __init__(self, fixed_translation: str = "테스트 번역 결과"):
        self._translation = fixed_translation

    def translate_en_to_ko(self, text: str) -> str:
        logger.debug(
            "MockTranslationService: '%s' → '%s'", text, self._translation
        )
        return self._translation


class DeepLTranslationService:
    """
    DeepL REST API translation (EN → KO).

    Free tier: api-free.deepl.com — set DEEPL_API_KEY in environment.
    Rate-limit (429) and transient network errors are handled by RetryClient.
    """

    _API_URL = "https://api-free.deepl.com/v2/translate"

    def __init__(
        self,
        api_key: str,
        *,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ):
        self._client = RetryClient(
            max_retries=max_retries,
            base_delay=base_delay,
            headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
        )

    def translate_en_to_ko(self, text: str) -> str:
        resp = self._client.post(
            self._API_URL,
            data={"text": text, "source_lang": "EN", "target_lang": "KO"},
        )
        return resp.json()["translations"][0]["text"]

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DeepLTranslationService":
        return self

    def __exit__(self, *_) -> None:
        self.close()


class GoogleTranslateService:
    """
    Google Cloud Translation API v2 (EN → KO).

    Set GOOGLE_TRANSLATE_API_KEY in environment.
    Rate-limit (429) and transient network errors are handled by RetryClient.
    """

    _API_URL = "https://translation.googleapis.com/language/translate/v2"

    def __init__(
        self,
        api_key: str,
        *,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ):
        self._api_key = api_key
        self._client = RetryClient(max_retries=max_retries, base_delay=base_delay)

    def translate_en_to_ko(self, text: str) -> str:
        resp = self._client.post(
            self._API_URL,
            params={"key": self._api_key},
            json={"q": text, "source": "en", "target": "ko", "format": "text"},
        )
        return resp.json()["data"]["translations"][0]["translatedText"]

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GoogleTranslateService":
        return self

    def __exit__(self, *_) -> None:
        self.close()
