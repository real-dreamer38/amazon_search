"""
Cross-Border Matching Engine — Vision (image) similarity providers.

MockVisionMatcher        : deterministic mock for unit tests (no API calls).
GoogleCloudVisionMatcher : production stub; uses Cloud Vision label/object detection
                           + Jaccard similarity on the returned label sets.
                           Retry logic delegated to RetryClient (exponential backoff).
"""
from __future__ import annotations

import logging

from arbitrage_x.ingestion.base import RetryClient

logger = logging.getLogger(__name__)


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


class GoogleCloudVisionMatcher:
    """
    Production image matcher backed by Google Cloud Vision API.

    Fetches LABEL_DETECTION + OBJECT_LOCALIZATION for each image URL,
    then computes Jaccard similarity on the returned label/object name sets.

    Rate-limit (429) and transient network errors are handled by RetryClient
    with the same exponential-backoff policy used across the rest of the system.
    """

    _VISION_API_URL = "https://vision.googleapis.com/v1/images:annotate"

    def __init__(
        self,
        api_key: str,
        *,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ):
        self._api_key = api_key
        self._client = RetryClient(max_retries=max_retries, base_delay=base_delay)

    def compare(self, image_url_a: str, image_url_b: str) -> float:
        labels_a = self._get_labels(image_url_a)
        labels_b = self._get_labels(image_url_b)

        if not labels_a or not labels_b:
            logger.warning(
                "GoogleCloudVisionMatcher: empty label set for (%s, %s)",
                image_url_a,
                image_url_b,
            )
            return 0.0

        union = len(labels_a | labels_b)
        return len(labels_a & labels_b) / union if union else 0.0

    def _get_labels(self, image_url: str) -> set[str]:
        payload = {
            "requests": [
                {
                    "image": {"source": {"imageUri": image_url}},
                    "features": [
                        {"type": "LABEL_DETECTION", "maxResults": 20},
                        {"type": "OBJECT_LOCALIZATION", "maxResults": 10},
                    ],
                }
            ]
        }
        resp = self._client.post(
            f"{self._VISION_API_URL}?key={self._api_key}",
            json=payload,
        )
        data = resp.json()
        response_block = data.get("responses", [{}])[0]

        labels: set[str] = set()
        for annotation in response_block.get("labelAnnotations", []):
            labels.add(annotation["description"].lower())
        for obj in response_block.get("localizedObjectAnnotations", []):
            labels.add(obj["name"].lower())
        return labels

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GoogleCloudVisionMatcher":
        return self

    def __exit__(self, *_) -> None:
        self.close()
