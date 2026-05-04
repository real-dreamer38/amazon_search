"""
Cross-Border Matching Engine — Vision (image) similarity providers.

MockVisionMatcher   : deterministic mock for unit tests (no API calls).
GeminiVisionMatcher : production matcher using Gemini 1.5 Flash via google-genai SDK.
                      Downloads both images, sends them as inline data, and asks the
                      model to score visual similarity on a [0.0, 1.0] scale using a
                      structured JSON response_schema.  Fail-open: any exception logs
                      a warning and returns 0.0 so the pipeline is never hard-blocked
                      by a transient API error.
"""
from __future__ import annotations

import json
import logging

import httpx
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

_PROMPT = (
    "두 상품 이미지의 패키징, 텍스트, 브랜드 로고를 비교하여 동일 상품인지 판별하라. "
    "similarity_score는 0.0(완전히 다른 상품)에서 1.0(동일 상품)까지의 실수로 표현하라."
)

_RESPONSE_SCHEMA = types.Schema(
    type="OBJECT",
    properties={
        "similarity_score": types.Schema(type="NUMBER"),
        "reasoning": types.Schema(type="STRING"),
        "is_same_product": types.Schema(type="BOOLEAN"),
    },
    required=["similarity_score", "reasoning", "is_same_product"],
)


class MockVisionMatcher:
    """Returns a fixed score regardless of input URLs — for testing only."""

    def __init__(self, fixed_score: float = 0.97):
        if not 0.0 <= fixed_score <= 1.0:
            raise ValueError(f"score must be in [0.0, 1.0], got {fixed_score}")
        self._score = fixed_score

    def compare(self, image_url_a: str, image_url_b: str) -> float:
        logger.debug(
            "MockVisionMatcher: %.3f for (%s, %s)", self._score, image_url_a, image_url_b
        )
        return self._score


class GeminiVisionMatcher:
    """
    Production image matcher backed by Gemini 1.5 Flash (google-genai SDK).

    Both image URLs are fetched via httpx and sent as inline bytes to Gemini.
    The model responds with structured JSON (response_schema) containing a
    similarity_score, reasoning, and is_same_product flag.

    On any exception (network error, API error, JSON parse failure) the method
    logs a warning and returns 0.0 so downstream stages are never hard-blocked.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gemini-1.5-flash",
        http_timeout: float = 30.0,
    ):
        self._model = model
        self._genai = genai.Client(api_key=api_key)
        self._http = httpx.Client(timeout=http_timeout, follow_redirects=True)

    def compare(self, image_url_a: str, image_url_b: str) -> float:
        try:
            bytes_a = self._fetch(image_url_a)
            bytes_b = self._fetch(image_url_b)

            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_RESPONSE_SCHEMA,
            )

            contents = [
                types.Part(inline_data=types.Blob(data=bytes_a, mime_type="image/jpeg")),
                types.Part(inline_data=types.Blob(data=bytes_b, mime_type="image/jpeg")),
                types.Part(text=_PROMPT),
            ]

            response = self._genai.models.generate_content(
                model=self._model,
                contents=contents,
                config=config,
            )

            result = json.loads(response.text)
            score = float(result["similarity_score"])
            return max(0.0, min(1.0, score))

        except Exception as exc:
            logger.warning(
                "GeminiVisionMatcher: comparison failed for (%s, %s): %s",
                image_url_a,
                image_url_b,
                exc,
            )
            return 0.0

    def _fetch(self, url: str) -> bytes:
        resp = self._http.get(url)
        resp.raise_for_status()
        return resp.content

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GeminiVisionMatcher":
        return self

    def __exit__(self, *_) -> None:
        self.close()
