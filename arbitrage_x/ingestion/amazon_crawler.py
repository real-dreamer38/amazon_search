"""
Arbitrage-X — Amazon SP-API Catalog & Pricing Crawler

SP-API endpoints used:
  GET /catalog/2022-04-01/items              — keyword search (rate: 2 req/s, burst 2)
  GET /catalog/2022-04-01/items/{asin}       — item detail   (rate: 2 req/s, burst 2)
  GET /products/pricing/v0/competitivePrice  — batch pricing (rate: 0.5 req/s, burst 1)
    → batched up to 20 ASINs per call to minimize API usage
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from config.settings import SP_API_MARKETPLACE_ID
from .base import RetryClient, SPAPIAuth
from .schemas import AmazonProductListing

logger = logging.getLogger(__name__)

SP_API_BASE = "https://sellingpartnerapi-na.amazon.com"

# Conservative inter-call delays (well within rate limits)
_CATALOG_SEARCH_DELAY = 0.6    # 2 req/s → 0.5s min; use 0.6s for safety
_CATALOG_DETAIL_DELAY = 0.6
_PRICING_BATCH_DELAY = 2.2     # 0.5 req/s → 2.0s min; use 2.2s for safety
_PRICING_BATCH_SIZE = 20


class AmazonCatalogCrawler:
    """
    Fetches Amazon product listings via SP-API.
    All calls go through RetryClient (exponential backoff).
    Caller is responsible for providing valid SP-API credentials in .env.
    """

    def __init__(self, auth: Optional[SPAPIAuth] = None):
        self._auth = auth or SPAPIAuth()
        self._client = RetryClient()
        self._owns_auth = auth is None

    # ── Auth header ────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "x-amz-access-token": self._auth.get_token(),
            "Accept": "application/json",
        }

    # ── API calls ──────────────────────────────────────────────────────────────

    def _search_catalog(self, keywords: str, page_size: int) -> list[dict]:
        """
        Calls searchCatalogItems and returns raw item list.
        includedData selects only the fields we actually parse — minimizes payload.
        """
        resp = self._client.get(
            f"{SP_API_BASE}/catalog/2022-04-01/items",
            params={
                "marketplaceIds": SP_API_MARKETPLACE_ID,
                "keywords": keywords,
                "includedData": "images,summaries,salesRanks,dimensions",
                "pageSize": min(page_size, 20),
            },
            headers=self._headers(),
        )
        return resp.json().get("items", [])

    def _get_competitive_pricing(self, asins: list[str]) -> dict[str, dict]:
        """
        Batch-fetches competitive pricing for up to 20 ASINs per call.
        Returns {asin: Product-dict-from-SP-API}.
        """
        result: dict[str, dict] = {}
        for i in range(0, len(asins), _PRICING_BATCH_SIZE):
            batch = asins[i : i + _PRICING_BATCH_SIZE]
            try:
                resp = self._client.get(
                    f"{SP_API_BASE}/products/pricing/v0/competitivePrice",
                    params={
                        "MarketplaceId": SP_API_MARKETPLACE_ID,
                        "ItemType": "Asin",
                        "Asins": ",".join(batch),
                    },
                    headers=self._headers(),
                )
                for entry in resp.json().get("payload", []):
                    asin = entry.get("ASIN", "")
                    if asin and entry.get("status") == "Success":
                        result[asin] = entry.get("Product", {})
            except Exception as exc:
                logger.error("Pricing batch error [%s…]: %s", batch[:3], exc)

            if i + _PRICING_BATCH_SIZE < len(asins):
                time.sleep(_PRICING_BATCH_DELAY)

        return result

    # ── Response parsers (static — testable in isolation) ─────────────────────

    @staticmethod
    def _extract_image(item: dict) -> Optional[str]:
        for entry in item.get("images", []):
            for img in entry.get("images", []):
                if img.get("variant") == "MAIN":
                    return img.get("link")
        return None

    @staticmethod
    def _extract_summary(item: dict) -> dict:
        summaries = item.get("summaries", [])
        return summaries[0] if summaries else {}

    @staticmethod
    def _extract_dimensions(item: dict) -> dict:
        """
        Normalizes item dimensions to kg/cm regardless of source unit.
        Returns dict with keys: weight_kg, length_cm, width_cm, height_cm.
        """
        def _to_kg(val: Optional[dict]) -> Optional[float]:
            if not val:
                return None
            v = val.get("value")
            if v is None:
                return None
            unit = val.get("unit", "").lower()
            if unit in ("pounds", "pound", "lb", "lbs"):
                return round(float(v) * 0.453592, 4)
            if unit in ("ounces", "ounce", "oz"):
                return round(float(v) * 0.0283495, 4)
            return round(float(v), 4)  # assume kg

        def _to_cm(val: Optional[dict]) -> Optional[float]:
            if not val:
                return None
            v = val.get("value")
            if v is None:
                return None
            unit = val.get("unit", "").lower()
            if unit in ("inches", "inch"):
                return round(float(v) * 2.54, 2)
            if unit in ("feet", "foot"):
                return round(float(v) * 30.48, 2)
            return round(float(v), 2)  # assume cm

        for entry in item.get("dimensions", []):
            dim = entry.get("item", {})
            return {
                "weight_kg": _to_kg(dim.get("weight")),
                "length_cm": _to_cm(dim.get("length")),
                "width_cm":  _to_cm(dim.get("width")),
                "height_cm": _to_cm(dim.get("height")),
            }
        return {"weight_kg": None, "length_cm": None, "width_cm": None, "height_cm": None}

    @staticmethod
    def _extract_bsr(item: dict) -> tuple[Optional[int], Optional[str]]:
        for entry in item.get("salesRanks", []):
            ranks = entry.get("displayGroupRanks", [])
            if ranks:
                top = min(ranks, key=lambda r: r.get("rank", 999_999))
                return top.get("rank"), top.get("title")
        return None, None

    @staticmethod
    def _extract_pricing(
        pricing: dict,
    ) -> tuple[Optional[float], Optional[float], Optional[int], Optional[str]]:
        """
        Parses SP-API CompetitivePricing Product dict.
        Returns (buy_box_price, lowest_new_price, sellers_count, buy_box_seller).
        """
        if not pricing:
            return None, None, None, None

        comp = pricing.get("CompetitivePricing", {})
        competitive_prices = comp.get("CompetitivePrices", [])

        buy_box_price: Optional[float] = None
        buy_box_seller: Optional[str] = None
        lowest_new: Optional[float] = None

        for cp in competitive_prices:
            price_id = cp.get("competitivePriceId")  # "1" = Buy Box (new)
            condition = cp.get("condition", "").lower()
            amount = cp.get("Price", {}).get("LandedPrice", {}).get("Amount")
            if amount is None:
                continue
            price_val = float(amount)

            if price_id == "1" and condition in ("new", ""):
                buy_box_price = price_val
                if cp.get("belongsToRequester"):
                    buy_box_seller = "self"

            if condition == "new":
                if lowest_new is None or price_val < lowest_new:
                    lowest_new = price_val

        sellers_count: Optional[int] = None
        for offer in comp.get("NumberOfOfferListings", []):
            if offer.get("condition", "").lower() == "new":
                sellers_count = offer.get("Count")
                break

        return buy_box_price, lowest_new, sellers_count, buy_box_seller

    # ── Public API ─────────────────────────────────────────────────────────────

    def fetch_listings(
        self,
        keywords: str,
        max_results: int = 20,
    ) -> list[AmazonProductListing]:
        """
        Search Amazon for `keywords` and return enriched product listings.
        Makes 2 API round-trips: catalog search + batch competitive pricing.
        """
        logger.info("Amazon search: %r  max=%d", keywords, max_results)

        items = self._search_catalog(keywords, page_size=min(max_results, 20))
        if not items:
            logger.warning("No items returned for: %r", keywords)
            return []

        time.sleep(_CATALOG_SEARCH_DELAY)

        asins = [it["asin"] for it in items if "asin" in it]
        pricing_map = self._get_competitive_pricing(asins)

        listings: list[AmazonProductListing] = []
        for item in items:
            asin = item.get("asin", "")
            summary = self._extract_summary(item)
            dims = self._extract_dimensions(item)
            bsr_rank, bsr_cat = self._extract_bsr(item)
            buy_box, lowest_new, sellers_cnt, bb_seller = self._extract_pricing(
                pricing_map.get(asin, {})
            )
            listings.append(
                AmazonProductListing(
                    asin=asin,
                    title=summary.get("itemName", ""),
                    brand=summary.get("brandName"),
                    category=summary.get("websiteDisplayGroup"),
                    image_url=self._extract_image(item),
                    buy_box_price=buy_box,
                    lowest_new_price=lowest_new,
                    sellers_count=sellers_cnt,
                    buy_box_seller=bb_seller,
                    bsr_rank=bsr_rank,
                    bsr_category=bsr_cat,
                    **dims,
                )
            )

        logger.info("Amazon search complete: %d listings", len(listings))
        return listings

    def close(self) -> None:
        self._client.close()
        if self._owns_auth:
            self._auth.close()

    def __enter__(self) -> "AmazonCatalogCrawler":
        return self

    def __exit__(self, *_) -> None:
        self.close()
