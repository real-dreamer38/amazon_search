"""
Cross-Border Matching Engine — Text similarity utilities.

Combines three signals to handle Korean/English product title comparison:
  1. Token Jaccard  — keyword overlap after whitespace tokenisation
  2. Character bigram Jaccard — language-agnostic; robust to Korean spacing variation
  3. SequenceMatcher ratio — longest-common-subsequence edit distance

Weights: 0.4 / 0.3 / 0.3 (empirically tuned for e-commerce product titles).
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher


def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _char_bigrams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)}


def compute_similarity(text_a: str, text_b: str) -> float:
    """Return a similarity score in [0.0, 1.0] between two product title strings."""
    a = _normalize(text_a)
    b = _normalize(text_b)

    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0

    # Token Jaccard
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    token_union = len(a_tokens | b_tokens)
    token_jaccard = len(a_tokens & b_tokens) / token_union if token_union else 0.0

    # Character bigram Jaccard
    a_bg = _char_bigrams(a)
    b_bg = _char_bigrams(b)
    bg_union = len(a_bg | b_bg)
    bigram_jaccard = len(a_bg & b_bg) / bg_union if bg_union else 0.0

    # Sequence ratio
    seq_ratio = SequenceMatcher(None, a, b).ratio()

    return 0.4 * token_jaccard + 0.3 * bigram_jaccard + 0.3 * seq_ratio
