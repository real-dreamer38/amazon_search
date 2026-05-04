"""
Arbitrage-X — Cross-Border Matching Engine

Determines whether an Amazon US product and a Korean market product are identical
by combining two independent signals:

  1차 (Vision)  : image similarity via VisionMatcherProtocol   → image_score ∈ [0, 1]
  2차 (Text/NLP): title translation + text similarity           → text_score  ∈ [0, 1]

  composite = image_weight × image_score + text_weight × text_score

  is_match ↔ composite >= match_threshold (default 0.95)

Graceful degradation:
  - Vision API failure  → text-only mode (image_weight collapses to 0, text_weight → 1.0)
  - Translation failure → compare original English title against Korean title (lower scores expected)
  - Both fail           → composite from raw en/ko text similarity; almost always NO_MATCH
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from arbitrage_x.matching.interfaces import TranslationServiceProtocol, VisionMatcherProtocol
from arbitrage_x.matching.text_similarity import compute_similarity

logger = logging.getLogger(__name__)


class MatchStatus(str, Enum):
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"


@dataclass
class MatchRequest:
    amazon_asin: str
    amazon_title: str
    amazon_image_url: str
    korean_title: str
    korean_image_url: str


@dataclass
class MatchResult:
    amazon_asin: str
    status: MatchStatus
    is_match: bool
    composite_score: float
    image_score: Optional[float]
    text_score: float
    translated_title: str
    match_threshold: float
    image_weight_used: float
    text_weight_used: float

    def __str__(self) -> str:
        img = f"{self.image_score:.3f}" if self.image_score is not None else "N/A"
        return (
            f"asin={self.amazon_asin} status={self.status.value} "
            f"composite={self.composite_score:.3f} image={img} text={self.text_score:.3f}"
        )


class CrossBorderMatchingEngine:
    """
    Fuses vision and text signals to decide if a US/Korean product pair is identical.

    Default weights (image=0.40, text=0.60) reflect that product titles carry
    stronger discriminative signal than generic product photos for e-commerce matching.
    """

    MATCH_THRESHOLD: float = 0.95
    DEFAULT_IMAGE_WEIGHT: float = 0.40
    DEFAULT_TEXT_WEIGHT: float = 0.60

    def __init__(
        self,
        vision_matcher: VisionMatcherProtocol,
        translation_service: TranslationServiceProtocol,
        *,
        image_weight: float = DEFAULT_IMAGE_WEIGHT,
        text_weight: float = DEFAULT_TEXT_WEIGHT,
        match_threshold: float = MATCH_THRESHOLD,
    ):
        if abs(image_weight + text_weight - 1.0) > 1e-9:
            raise ValueError(
                f"image_weight + text_weight must sum to 1.0, got {image_weight + text_weight:.6f}"
            )
        self._vision = vision_matcher
        self._translation = translation_service
        self.image_weight = image_weight
        self.text_weight = text_weight
        self.match_threshold = match_threshold

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def match(self, request: MatchRequest) -> MatchResult:
        """Score a single Amazon–Korean product pair and return a MatchResult."""
        image_score, actual_image_w, actual_text_w = self._score_vision(request)
        translated_title, text_score = self._score_text(request)

        if image_score is not None:
            composite = actual_image_w * image_score + actual_text_w * text_score
        else:
            composite = text_score

        composite = round(min(max(composite, 0.0), 1.0), 6)
        is_match = composite >= self.match_threshold
        status = MatchStatus.MATCH if is_match else MatchStatus.NO_MATCH

        result = MatchResult(
            amazon_asin=request.amazon_asin,
            status=status,
            is_match=is_match,
            composite_score=composite,
            image_score=round(image_score, 6) if image_score is not None else None,
            text_score=round(text_score, 6),
            translated_title=translated_title,
            match_threshold=self.match_threshold,
            image_weight_used=actual_image_w,
            text_weight_used=actual_text_w,
        )
        logger.info("[MATCHING] %s", result)
        return result

    def batch_match(self, requests: list[MatchRequest]) -> list[MatchResult]:
        return [self.match(r) for r in requests]

    def filter_matches(self, requests: list[MatchRequest]) -> list[MatchResult]:
        """Return only confirmed MATCH results from a batch."""
        results = self.batch_match(requests)
        confirmed = [r for r in results if r.is_match]
        logger.info(
            "[MATCHING] batch %d/%d matched (threshold=%.2f)",
            len(confirmed),
            len(results),
            self.match_threshold,
        )
        return confirmed

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _score_vision(
        self, request: MatchRequest
    ) -> tuple[Optional[float], float, float]:
        """Returns (image_score | None, effective_image_weight, effective_text_weight)."""
        try:
            score = self._vision.compare(
                request.amazon_image_url, request.korean_image_url
            )
            if not 0.0 <= score <= 1.0:
                raise ValueError(f"image_score out of range: {score}")
            return score, self.image_weight, self.text_weight
        except Exception as exc:
            logger.warning(
                "[MATCHING] Vision API failed for asin=%s — text-only fallback. %s: %s",
                request.amazon_asin,
                type(exc).__name__,
                exc,
            )
            return None, 0.0, 1.0

    def _score_text(self, request: MatchRequest) -> tuple[str, float]:
        """Returns (translated_title, text_score)."""
        translated = request.amazon_title
        try:
            translated = self._translation.translate_en_to_ko(request.amazon_title)
        except Exception as exc:
            logger.warning(
                "[MATCHING] Translation API failed for asin=%s — using original title. %s: %s",
                request.amazon_asin,
                type(exc).__name__,
                exc,
            )
        score = compute_similarity(translated, request.korean_title)
        return translated, score
