"""
Arbitrage-X — Ingestion 모듈 단위 테스트
모든 외부 HTTP 호출은 unittest.mock으로 대체.
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from arbitrage_x.ingestion.amazon_crawler import AmazonCatalogCrawler
from arbitrage_x.ingestion.base import RetryClient
from arbitrage_x.ingestion.naver_crawler import NaverShoppingCrawler
from arbitrage_x.ingestion.ingestion_service import IngestionService
from arbitrage_x.ingestion.schemas import AmazonProductListing, NaverProductListing


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _mock_response(status_code: int, body: dict, headers: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = body
    resp.headers = headers or {}
    resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            f"HTTP {status_code}", request=MagicMock(), response=resp
        )
        if status_code >= 400
        else None
    )
    return resp


# ══════════════════════════════════════════════════════════════════════════════
# RetryClient
# ══════════════════════════════════════════════════════════════════════════════

class TestRetryClient:
    def test_success_on_first_attempt(self):
        client = RetryClient(max_retries=2)
        ok = _mock_response(200, {"ok": True})
        ok.raise_for_status = MagicMock()  # no-op
        with patch.object(client._http, "request", return_value=ok):
            resp = client.get("http://example.com")
        assert resp.status_code == 200

    def test_retries_on_429_then_succeeds(self):
        client = RetryClient(max_retries=2, base_delay=0.01)
        rate_limited = _mock_response(429, {})
        ok = _mock_response(200, {"ok": True})
        ok.raise_for_status = MagicMock()

        call_count = 0
        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return rate_limited if call_count == 1 else ok

        with patch.object(client._http, "request", side_effect=side_effect):
            with patch("time.sleep"):  # skip actual sleep
                resp = client.get("http://example.com")

        assert resp.status_code == 200
        assert call_count == 2

    def test_raises_after_max_retries(self):
        client = RetryClient(max_retries=2, base_delay=0.01)
        server_error = _mock_response(500, {})

        with patch.object(client._http, "request", return_value=server_error):
            with patch("time.sleep"):
                with pytest.raises(httpx.HTTPStatusError):
                    client.get("http://example.com")

    def test_retries_on_connect_timeout(self):
        client = RetryClient(max_retries=2, base_delay=0.01)
        ok = _mock_response(200, {"ok": True})
        ok.raise_for_status = MagicMock()

        call_count = 0
        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectTimeout("timeout", request=MagicMock())
            return ok

        with patch.object(client._http, "request", side_effect=side_effect):
            with patch("time.sleep"):
                resp = client.get("http://example.com")

        assert resp.status_code == 200
        assert call_count == 2

    def test_respects_retry_after_header(self):
        client = RetryClient(max_retries=1, base_delay=1.0)
        rate_limited = _mock_response(429, {}, headers={"Retry-After": "5"})
        ok = _mock_response(200, {})
        ok.raise_for_status = MagicMock()

        calls = []
        def side_effect(*args, **kwargs):
            return rate_limited if not calls else ok

        sleep_calls = []
        with patch.object(client._http, "request", side_effect=side_effect):
            with patch("time.sleep", side_effect=lambda s: (calls.append(1), sleep_calls.append(s))):
                client.get("http://example.com")

        assert sleep_calls[0] == 5.0


# ══════════════════════════════════════════════════════════════════════════════
# AmazonCatalogCrawler — static parsers
# ══════════════════════════════════════════════════════════════════════════════

class TestAmazonCatalogCrawlerParsers:
    def test_extract_image_main_variant(self):
        item = {
            "images": [
                {"images": [
                    {"variant": "PT01", "link": "https://example.com/pt01.jpg"},
                    {"variant": "MAIN", "link": "https://example.com/main.jpg"},
                ]}
            ]
        }
        assert AmazonCatalogCrawler._extract_image(item) == "https://example.com/main.jpg"

    def test_extract_image_missing(self):
        assert AmazonCatalogCrawler._extract_image({}) is None

    def test_extract_dimensions_inches_to_cm(self):
        item = {
            "dimensions": [{
                "item": {
                    "weight": {"value": 2.0, "unit": "pounds"},
                    "length": {"value": 10.0, "unit": "inches"},
                    "width":  {"value": 5.0,  "unit": "inches"},
                    "height": {"value": 3.0,  "unit": "inches"},
                }
            }]
        }
        dims = AmazonCatalogCrawler._extract_dimensions(item)
        assert abs(dims["weight_kg"] - 0.907184) < 0.001
        assert abs(dims["length_cm"] - 25.4) < 0.01
        assert abs(dims["width_cm"] - 12.7) < 0.01
        assert abs(dims["height_cm"] - 7.62) < 0.01

    def test_extract_dimensions_missing(self):
        dims = AmazonCatalogCrawler._extract_dimensions({})
        assert all(v is None for v in dims.values())

    def test_extract_bsr_returns_lowest_rank(self):
        item = {
            "salesRanks": [{
                "displayGroupRanks": [
                    {"rank": 500, "title": "Kitchen"},
                    {"rank": 12,  "title": "Laptop Stands"},
                ]
            }]
        }
        rank, cat = AmazonCatalogCrawler._extract_bsr(item)
        assert rank == 12
        assert cat == "Laptop Stands"

    def test_extract_pricing_buy_box(self):
        pricing = {
            "CompetitivePricing": {
                "CompetitivePrices": [{
                    "competitivePriceId": "1",
                    "condition": "New",
                    "Price": {"LandedPrice": {"Amount": 29.99}},
                    "belongsToRequester": False,
                }],
                "NumberOfOfferListings": [
                    {"condition": "New", "Count": 7}
                ],
            }
        }
        buy_box, lowest, sellers, seller = AmazonCatalogCrawler._extract_pricing(pricing)
        assert buy_box == 29.99
        assert lowest == 29.99
        assert sellers == 7
        assert seller is None

    def test_extract_pricing_empty(self):
        assert AmazonCatalogCrawler._extract_pricing({}) == (None, None, None, None)


# ══════════════════════════════════════════════════════════════════════════════
# NaverShoppingCrawler
# ══════════════════════════════════════════════════════════════════════════════

class TestNaverShoppingCrawler:
    def test_returns_empty_when_unconfigured(self):
        crawler = NaverShoppingCrawler()
        crawler._configured = False
        result = crawler.search("노트북 거치대")
        assert result == []

    def test_strips_html_tags(self):
        crawler = NaverShoppingCrawler()
        crawler._configured = True
        body = {
            "items": [{
                "title": "<b>노트북</b> 거치대",
                "link": "https://example.com",
                "lprice": "15000",
                "hprice": "20000",
                "mallName": "스마트스토어",
                "productId": "12345",
                "category1": "컴퓨터",
                "category2": "주변기기",
                "category3": "",
                "image": "https://example.com/img.jpg",
            }]
        }
        with patch.object(crawler._client, "get", return_value=_mock_response(200, body)):
            results = crawler.search("노트북 거치대")

        assert len(results) == 1
        assert results[0].title == "노트북 거치대"
        assert results[0].low_price == 15000.0
        assert results[0].mall_name == "스마트스토어"

    def test_handles_api_error_gracefully(self):
        crawler = NaverShoppingCrawler()
        crawler._configured = True
        with patch.object(
            crawler._client, "get", side_effect=Exception("connection refused")
        ):
            results = crawler.search("test")
        assert results == []


# ══════════════════════════════════════════════════════════════════════════════
# IngestionService
# ══════════════════════════════════════════════════════════════════════════════

class TestIngestionService:
    def _make_amazon_listing(self, asin: str = "B00TEST001") -> AmazonProductListing:
        return AmazonProductListing(
            asin=asin,
            title="Test Product",
            brand="TestBrand",
            buy_box_price=19.99,
        )

    def _make_naver_listing(self) -> NaverProductListing:
        return NaverProductListing(
            title="테스트 상품",
            link="https://example.com",
            low_price=15000.0,
        )

    def test_returns_result_with_both_crawlers(self):
        amazon_mock = MagicMock()
        amazon_mock.fetch_listings.return_value = [self._make_amazon_listing()]
        naver_mock = MagicMock()
        naver_mock.search.return_value = [self._make_naver_listing()]

        svc = IngestionService(amazon=amazon_mock, naver=naver_mock)
        result = svc.run("laptop stand", keyword_ko="노트북 거치대", session=None)

        assert result.amazon_count == 1
        assert result.naver_count == 1
        assert result.errors == []

    def test_amazon_error_recorded_in_result(self):
        amazon_mock = MagicMock()
        amazon_mock.fetch_listings.side_effect = RuntimeError("SP-API down")
        naver_mock = MagicMock()
        naver_mock.search.return_value = []

        svc = IngestionService(amazon=amazon_mock, naver=naver_mock)
        result = svc.run("test", session=None)

        assert result.amazon_count == 0
        assert any("Amazon crawl failed" in e for e in result.errors)

    def test_uses_keyword_en_for_naver_when_ko_not_provided(self):
        amazon_mock = MagicMock()
        amazon_mock.fetch_listings.return_value = []
        naver_mock = MagicMock()
        naver_mock.search.return_value = []

        svc = IngestionService(amazon=amazon_mock, naver=naver_mock)
        svc.run("laptop stand", keyword_ko=None, session=None)

        naver_mock.search.assert_called_once()
        call_args = naver_mock.search.call_args
        assert call_args[0][0] == "laptop stand"
