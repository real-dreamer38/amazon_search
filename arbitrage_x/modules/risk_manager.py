"""
Arbitrage-X — Risk Manager
IP 분쟁 가능성 탐지 + Amazon 직접 판매 체크
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from config.settings import USPTO_TESS_BASE_URL

logger = logging.getLogger(__name__)

AMAZON_SELLER_FLAG = "Amazon.com"


@dataclass
class RiskReport:
    asin: str
    brand: str
    ip_risk_level: str           # "NONE" | "LOW" | "MEDIUM" | "HIGH"
    uspto_registered: bool
    trademark_serial: Optional[str]
    trademark_owner: Optional[str]
    trademark_status: Optional[str]
    sold_by_amazon: bool
    warnings: list[str] = field(default_factory=list)
    raw_uspto: Optional[dict] = None

    @property
    def needs_review(self) -> bool:
        return self.ip_risk_level in ("MEDIUM", "HIGH") or self.sold_by_amazon


class RiskManager:
    """
    1. USPTO TSDR API로 브랜드 상표 등록 여부 확인
    2. Buy Box / 판매자 목록에서 'Amazon.com' 포함 여부 확인
    """

    def __init__(self, http_timeout: int = 10):
        self._client = httpx.Client(timeout=http_timeout)

    def assess(
        self,
        asin: str,
        brand: str,
        sellers: list[str],
    ) -> RiskReport:
        """전체 리스크 평가를 수행하고 RiskReport를 반환한다."""
        tm_result = self._check_uspto(brand)
        sold_by_amazon = self._check_amazon_seller(sellers)

        ip_risk_level = self._determine_ip_risk(tm_result, sold_by_amazon)

        warnings: list[str] = []
        if tm_result.get("registered"):
            warnings.append(
                f"[IP 경고] 브랜드 '{brand}'는 USPTO에 등록된 상표입니다 "
                f"(Serial: {tm_result.get('serial_number')}, "
                f"소유자: {tm_result.get('owner')}). "
                "판매 전 IP 분쟁 가능성을 검토하십시오."
            )
        if sold_by_amazon:
            warnings.append(
                f"[체크 요망] 이 상품의 판매자 중 '{AMAZON_SELLER_FLAG}'이 포함되어 있습니다. "
                "아마존이 직접 판매하는 상품은 Buy Box 경쟁 및 계정 리스크가 높습니다."
            )

        report = RiskReport(
            asin=asin,
            brand=brand,
            ip_risk_level=ip_risk_level,
            uspto_registered=tm_result.get("registered", False),
            trademark_serial=tm_result.get("serial_number"),
            trademark_owner=tm_result.get("owner"),
            trademark_status=tm_result.get("status"),
            sold_by_amazon=sold_by_amazon,
            warnings=warnings,
            raw_uspto=tm_result.get("raw"),
        )

        for w in warnings:
            logger.warning("RISK [%s]: %s", asin, w)

        return report

    # ──────────────────────────────────────────────────────────────────────────
    # USPTO 조회
    # ──────────────────────────────────────────────────────────────────────────

    def _check_uspto(self, brand: str) -> dict:
        """
        USPTO TSDR API로 상표 등록 여부를 조회한다.
        API 실패 시 빈 결과를 반환하여 전체 흐름을 중단하지 않는다.
        """
        if not brand or len(brand) < 2:
            return {"registered": False}

        try:
            # TSDR Status API: GET /ts/cd/casestatus/{searchTerm}/sn
            url = f"{USPTO_TESS_BASE_URL}/casestatus/{httpx.URL(brand).path}/sn"
            # 공식 USPTO Trademark Search (Open Data)
            # 실제 운영 시 TSDR API 키 및 엔드포인트 확인 필요
            search_url = (
                "https://developer.uspto.gov/ipmarketplace/search/trademarks"
                f"?query={brand}&start=0&rows=5&type=us_trademark"
            )
            resp = self._client.get(search_url, headers={"Accept": "application/json"})

            if resp.status_code != 200:
                logger.debug("USPTO API returned %s for brand '%s'", resp.status_code, brand)
                return {"registered": False}

            data = resp.json()
            results = data.get("response", {}).get("docs", [])
            if not results:
                return {"registered": False}

            # 가장 일치하는 첫 번째 결과 분석
            first = results[0]
            mark_words = (first.get("markVerbalElements") or "").lower()
            registered = brand.lower() in mark_words and first.get("registrationNumber")

            return {
                "registered": bool(registered),
                "serial_number": first.get("serialNumber"),
                "registration_number": first.get("registrationNumber"),
                "owner": first.get("applicantNames", [None])[0],
                "status": first.get("markCurrentStatusExternalDescEng"),
                "raw": first,
            }

        except httpx.RequestError as e:
            logger.warning("USPTO API request failed for brand '%s': %s", brand, e)
            return {"registered": False, "error": str(e)}
        except Exception as e:
            logger.error("Unexpected error in USPTO check for '%s': %s", brand, e)
            return {"registered": False, "error": str(e)}

    # ──────────────────────────────────────────────────────────────────────────
    # Amazon 직접 판매 체크
    # ──────────────────────────────────────────────────────────────────────────

    def _check_amazon_seller(self, sellers: list[str]) -> bool:
        """판매자 목록에 'Amazon.com'이 포함되어 있으면 True."""
        return any(AMAZON_SELLER_FLAG.lower() in s.lower() for s in sellers)

    # ──────────────────────────────────────────────────────────────────────────
    # 리스크 레벨 산정
    # ──────────────────────────────────────────────────────────────────────────

    def _determine_ip_risk(self, tm_result: dict, sold_by_amazon: bool) -> str:
        if tm_result.get("registered"):
            status = (tm_result.get("status") or "").upper()
            if "LIVE" in status or "REGISTERED" in status:
                return "HIGH"
            return "MEDIUM"
        if sold_by_amazon:
            return "LOW"
        return "NONE"

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
