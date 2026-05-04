"""
Arbitrage-X — Naver Shopping Search Crawler
네이버 쇼핑 검색 API v1 사용.

API 한도: 25,000 req/day (초당 제한 없음)
API key 미설정 시 graceful degradation — 빈 목록 반환.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Optional

from config.settings import NAVER_CLIENT_ID, NAVER_CLIENT_SECRET
from .base import RetryClient
from .schemas import NaverProductListing

logger = logging.getLogger(__name__)

NAVER_SHOP_URL = "https://openapi.naver.com/v1/search/shop.json"
_HTML_TAG = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return html.unescape(_HTML_TAG.sub("", text)).strip()


class NaverShoppingCrawler:
    """
    Naver Shopping Search API 크롤러.
    RetryClient 기반 — 네트워크 오류 시 최대 3회 재시도.
    """

    def __init__(self):
        self._configured = bool(NAVER_CLIENT_ID and NAVER_CLIENT_SECRET)
        self._client = RetryClient(
            headers={
                "X-Naver-Client-Id": NAVER_CLIENT_ID or "",
                "X-Naver-Client-Secret": NAVER_CLIENT_SECRET or "",
            }
        )

    def search(
        self,
        query: str,
        display: int = 20,
        sort: str = "sim",
        # sort options: sim(유사도) | date | asc(가격낮은순) | dsc(가격높은순)
    ) -> list[NaverProductListing]:
        if not self._configured:
            logger.warning(
                "NAVER_CLIENT_ID / NAVER_CLIENT_SECRET not set — skipping Naver search"
            )
            return []

        logger.info("Naver search: %r  display=%d  sort=%s", query, display, sort)

        try:
            resp = self._client.get(
                NAVER_SHOP_URL,
                params={
                    "query": query,
                    "display": min(display, 100),
                    "sort": sort,
                },
            )
            raw_items = resp.json().get("items", [])
        except Exception as exc:
            logger.error("Naver search failed for %r: %s", query, exc)
            return []

        results: list[NaverProductListing] = []
        for item in raw_items:
            low = item.get("lprice") or None
            high = item.get("hprice") or None
            results.append(
                NaverProductListing(
                    title=_strip_html(item.get("title", "")),
                    link=item.get("link", ""),
                    image_url=item.get("image") or None,
                    low_price=float(low) if low else None,
                    high_price=float(high) if high else None,
                    mall_name=item.get("mallName") or None,
                    product_id=item.get("productId") or None,
                    category1=item.get("category1") or None,
                    category2=item.get("category2") or None,
                    category3=item.get("category3") or None,
                )
            )

        logger.info("Naver search %r → %d results", query, len(results))
        return results

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "NaverShoppingCrawler":
        return self

    def __exit__(self, *_) -> None:
        self.close()
