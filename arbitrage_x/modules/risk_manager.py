"""
Arbitrage-X — Risk Shield (리스크 관리 모듈)

아마존 아비트리지 실행 전 두 가지 필수 안전 체크를 수행한다.

  1차 (IP 리스크): USPTO Open Data API로 브랜드 상표 등록 여부 조회
                  → 등록 상태(LIVE/REGISTERED)이면 IP_RISK_HIGH
  2차 (경쟁 리스크): 바이박스 셀러에 'Amazon' 또는 'Amazon.com' 포함 여부
                   → AMAZON_SELLING 플래그

종합 상태(RiskStatus):
  SAFE           — 모든 체크 통과 → 마진 계산기·추천 리스트로 이관 가능
  IP_RISK_HIGH   — USPTO 등록 상표 (LIVE / REGISTERED) → 판매 중단 권고
  IP_RISK_MEDIUM — USPTO 등록 상표 (그 외 상태)       → 법률 검토 후 결정
  AMAZON_SELLING — 아마존 직접 판매 중                → 바이박스 경쟁 불가 권고
  BLOCKED        — IP_RISK_HIGH + AMAZON_SELLING 동시 해당

fail-open 정책: USPTO API가 실패(타임아웃·비정상 응답)하면 경고 로그만 남기고
IP 체크는 SAFE로 처리한다. 운영 가용성을 IP 정확도보다 우선시한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol, runtime_checkable

from arbitrage_x.ingestion.base import RetryClient

logger = logging.getLogger(__name__)

# Amazon 직접 판매를 판별하는 키워드 집합 (소문자 비교)
_AMAZON_KEYWORDS: frozenset[str] = frozenset({"amazon.com", "amazon"})

# USPTO 상태값 중 '현행 등록' 으로 간주하는 키워드 (소문자 부분 일치)
_LIVE_STATUS_KEYWORDS: frozenset[str] = frozenset({
    "live",
    "registered",
    "published for opposition",
    "notice of allowance",
})


# ══════════════════════════════════════════════════════════════════════════════
# 도메인 타입
# ══════════════════════════════════════════════════════════════════════════════


class RiskStatus(str, Enum):
    SAFE = "SAFE"
    IP_RISK_HIGH = "IP_RISK_HIGH"
    IP_RISK_MEDIUM = "IP_RISK_MEDIUM"
    AMAZON_SELLING = "AMAZON_SELLING"
    BLOCKED = "BLOCKED"

    @property
    def blocks_pipeline(self) -> bool:
        """마진 계산기·추천 리스트 진입을 차단하는 상태면 True."""
        return self != RiskStatus.SAFE


@dataclass
class TrademarkResult:
    """USPTO 조회 결과의 정규화된 표현."""
    registered: bool
    live: bool = False
    serial_number: Optional[str] = None
    owner: Optional[str] = None
    status_description: Optional[str] = None
    raw: Optional[dict] = None


@dataclass
class RiskInput:
    """RiskShield.assess()에 전달하는 상품 정보 단위."""
    asin: str
    brand: Optional[str]
    buy_box_seller: Optional[str] = None
    all_sellers: Optional[list[str]] = None


@dataclass
class RiskAssessment:
    """리스크 평가 결과."""
    asin: str
    brand: Optional[str]
    status: RiskStatus
    uspto_registered: bool
    trademark_serial: Optional[str]
    trademark_owner: Optional[str]
    trademark_status: Optional[str]
    sold_by_amazon: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def is_safe(self) -> bool:
        return self.status == RiskStatus.SAFE

    def can_proceed_to_margin(self) -> bool:
        """안전 판정 상품만 True — 마진 계산기 진입 게이트."""
        return self.is_safe


# ══════════════════════════════════════════════════════════════════════════════
# USPTO 클라이언트 인터페이스
# ══════════════════════════════════════════════════════════════════════════════


@runtime_checkable
class USPTOClientProtocol(Protocol):
    def search_trademark(self, brand: str) -> TrademarkResult:
        """브랜드명을 USPTO DB에서 조회하고 결과를 반환한다."""
        ...


# ══════════════════════════════════════════════════════════════════════════════
# Mock 구현 (테스트 전용)
# ══════════════════════════════════════════════════════════════════════════════


class MockUSPTOClient:
    """
    결정론적 Mock — 실제 HTTP 호출 없이 설정한 결과를 반환한다.
    테스트와 개발 환경 전용.
    """

    def __init__(
        self,
        *,
        registered: bool = False,
        live: bool = True,
        serial_number: Optional[str] = "78123456",
        owner: Optional[str] = "Test Corporation",
        status_description: Optional[str] = None,
    ):
        self._result = TrademarkResult(
            registered=registered,
            live=live if registered else False,
            serial_number=serial_number if registered else None,
            owner=owner if registered else None,
            status_description=(
                status_description or ("LIVE" if live else "PENDING CANCELLATION")
            ) if registered else None,
        )

    def search_trademark(self, brand: str) -> TrademarkResult:
        logger.debug("MockUSPTOClient: returning registered=%s for '%s'", self._result.registered, brand)
        return self._result


# ══════════════════════════════════════════════════════════════════════════════
# 실제 USPTO Open Data 클라이언트
# ══════════════════════════════════════════════════════════════════════════════


class USPTOOpenDataClient:
    """
    USPTO IP Marketplace Open Data API (공개, 인증 불필요).

    엔드포인트: https://developer.uspto.gov/ipmarketplace/search/trademarks
    rate-limit(429) 및 5xx는 RetryClient 지수 백오프로 처리.
    API 장애 시 TrademarkResult(registered=False) 반환 (fail-open).
    """

    _SEARCH_URL = "https://developer.uspto.gov/ipmarketplace/search/trademarks"

    def __init__(self, *, max_retries: int = 3, base_delay: float = 1.0):
        self._client = RetryClient(
            max_retries=max_retries,
            base_delay=base_delay,
            headers={"Accept": "application/json"},
        )

    def search_trademark(self, brand: str) -> TrademarkResult:
        if not brand or len(brand.strip()) < 2:
            return TrademarkResult(registered=False)

        try:
            resp = self._client.get(
                self._SEARCH_URL,
                params={"query": brand, "start": 0, "rows": 5, "type": "us_trademark"},
            )
            data = resp.json()
            docs = data.get("response", {}).get("docs", [])
            if not docs:
                return TrademarkResult(registered=False)

            # 첫 번째 결과에서 브랜드명 포함 여부 확인
            first = docs[0]
            mark_words = (first.get("markVerbalElements") or "").lower()
            if brand.lower() not in mark_words:
                return TrademarkResult(registered=False)

            status_desc: str = first.get("markCurrentStatusExternalDescEng", "")
            is_live = any(kw in status_desc.lower() for kw in _LIVE_STATUS_KEYWORDS)

            return TrademarkResult(
                registered=True,
                live=is_live,
                serial_number=first.get("serialNumber"),
                owner=(first.get("applicantNames") or [None])[0],
                status_description=status_desc,
                raw=first,
            )

        except Exception as exc:
            logger.warning(
                "USPTO API failed for brand '%s' — fail-open (registered=False). %s: %s",
                brand,
                type(exc).__name__,
                exc,
            )
            return TrademarkResult(registered=False)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "USPTOOpenDataClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()


# ══════════════════════════════════════════════════════════════════════════════
# Risk Shield — 메인 평가 엔진
# ══════════════════════════════════════════════════════════════════════════════


class RiskShield:
    """
    IP 리스크 + 아마존 경쟁 리스크를 종합 평가한다.

    RiskStatus.SAFE 판정을 받은 상품만 마진 계산기로 이관할 것.
    (RiskAssessment.can_proceed_to_margin() 참조)
    """

    def __init__(self, uspto_client: USPTOClientProtocol):
        if not isinstance(uspto_client, USPTOClientProtocol):
            raise TypeError(
                f"uspto_client must implement USPTOClientProtocol, got {type(uspto_client)}"
            )
        self._uspto = uspto_client

    # ──────────────────────────────────────────────────────────────────────────
    # 공개 API
    # ──────────────────────────────────────────────────────────────────────────

    def assess(self, item: RiskInput) -> RiskAssessment:
        """단일 상품 리스크 평가."""
        tm = self._check_trademark(item.brand)
        sold_by_amazon = self._check_amazon_seller(item.buy_box_seller, item.all_sellers)
        status = self._determine_status(tm, sold_by_amazon)
        warnings = self._build_warnings(item, tm, sold_by_amazon)

        for w in warnings:
            logger.warning("[RISK][%s] %s", item.asin, w)

        logger.info(
            "[RISK] asin=%s brand=%s status=%s sold_by_amazon=%s uspto_registered=%s",
            item.asin, item.brand, status.value, sold_by_amazon, tm.registered,
        )

        return RiskAssessment(
            asin=item.asin,
            brand=item.brand,
            status=status,
            uspto_registered=tm.registered,
            trademark_serial=tm.serial_number,
            trademark_owner=tm.owner,
            trademark_status=tm.status_description,
            sold_by_amazon=sold_by_amazon,
            warnings=warnings,
        )

    def batch_assess(self, items: list[RiskInput]) -> list[RiskAssessment]:
        """여러 상품을 일괄 평가한다."""
        return [self.assess(item) for item in items]

    def filter_safe(self, items: list[RiskInput]) -> list[RiskAssessment]:
        """SAFE 판정 상품만 반환한다."""
        results = self.batch_assess(items)
        safe = [r for r in results if r.is_safe]
        logger.info(
            "[RISK] filter_safe: %d/%d passed", len(safe), len(results)
        )
        return safe

    # ──────────────────────────────────────────────────────────────────────────
    # 내부 로직
    # ──────────────────────────────────────────────────────────────────────────

    def _check_trademark(self, brand: Optional[str]) -> TrademarkResult:
        if not brand or len(brand.strip()) < 2:
            return TrademarkResult(registered=False)
        try:
            return self._uspto.search_trademark(brand.strip())
        except Exception as exc:
            logger.warning(
                "USPTO client raised exception for brand '%s' — fail-open. %s: %s",
                brand, type(exc).__name__, exc,
            )
            return TrademarkResult(registered=False)

    def _check_amazon_seller(
        self,
        buy_box_seller: Optional[str],
        all_sellers: Optional[list[str]],
    ) -> bool:
        sellers: list[str] = []
        if buy_box_seller:
            sellers.append(buy_box_seller)
        if all_sellers:
            sellers.extend(all_sellers)
        return any(
            any(kw in s.lower() for kw in _AMAZON_KEYWORDS)
            for s in sellers
        )

    def _determine_status(self, tm: TrademarkResult, sold_by_amazon: bool) -> RiskStatus:
        if tm.registered:
            ip_status = RiskStatus.IP_RISK_HIGH if tm.live else RiskStatus.IP_RISK_MEDIUM
            if sold_by_amazon:
                return RiskStatus.BLOCKED
            return ip_status
        if sold_by_amazon:
            return RiskStatus.AMAZON_SELLING
        return RiskStatus.SAFE

    def _build_warnings(
        self, item: RiskInput, tm: TrademarkResult, sold_by_amazon: bool
    ) -> list[str]:
        warnings: list[str] = []
        if tm.registered:
            warnings.append(
                f"브랜드 '{item.brand}'은(는) USPTO 등록 상표입니다 "
                f"(Serial: {tm.serial_number}, 소유자: {tm.owner}, 상태: {tm.status_description}). "
                "판매 전 IP 분쟁 가능성을 반드시 검토하십시오."
            )
        if sold_by_amazon:
            warnings.append(
                "아마존이 이 상품의 바이박스를 직접 보유하고 있습니다. "
                "바이박스 경쟁에서 이기기 어렵고 계정 정책 위반 리스크가 있습니다."
            )
        return warnings
