"""
Arbitrage-X — Dynamic Margin Calculator
한국 소싱 상품의 아마존 FBA 순수익·ROI를 실시간 계산하고 적합 여부를 판별한다.

비용 계산 순서 (모두 USD로 통일):
  1. 한국 소싱가(KRW) ÷ 환율 → USD
  2. 한국 국내 배송비 (WeeklyState.domestic_shipping_cost, USD)
  3. 국제 물류비 (WeeklyState.international_shipping_cost, USD)
  4. FBA 배송 수수료 (item 직접 전달 또는 WeeklyState.fba_fee_override, USD)
  5. 아마존 레퍼럴 수수료 = buybox_price × referral_rate
  6. FBA Prep 서비스 수수료 (WeeklyState.prep_service_fee, USD)
  7. 관세 = source_price_usd × customs_duty_rate
  8. 기타 잡비 (WeeklyState.misc_cost_per_unit, USD)

ROI = 순수익 / 총비용 (기본 타겟: 30%)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from sqlalchemy.orm import Session

from arbitrage_x.core.weekly_state_manager import WeeklyStateManager
from arbitrage_x.db.models import MarginRecord, Product, WeeklyState
from arbitrage_x.utils.week_utils import get_current_week_key

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# 도메인 타입
# ══════════════════════════════════════════════════════════════════════════════

class EligibilityStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"        # ROI >= target_roi → 발주 진행
    INELIGIBLE = "INELIGIBLE"    # ROI < target_roi → 보류
    DATA_MISSING = "DATA_MISSING"  # 필수 데이터 누락 → 보류


@dataclass
class MarginInput:
    """마진 계산 요청 단위."""
    asin: str
    title: str
    buybox_price_usd: float    # 아마존 바이박스 가격 (USD)
    source_price_krw: float    # 한국 소싱가 (KRW)
    fba_fee_usd: Optional[float] = None  # SP-API 조회값; None이면 WeeklyState.fba_fee_override 사용


@dataclass
class CostBreakdown:
    """개별 비용 항목 (USD)."""
    source_price_usd: float
    domestic_shipping_usd: float
    international_shipping_usd: float
    fba_fee_usd: float
    referral_fee_usd: float
    prep_fee_usd: float
    customs_duty_usd: float
    misc_cost_usd: float

    @property
    def total(self) -> float:
        return (
            self.source_price_usd
            + self.domestic_shipping_usd
            + self.international_shipping_usd
            + self.fba_fee_usd
            + self.referral_fee_usd
            + self.prep_fee_usd
            + self.customs_duty_usd
            + self.misc_cost_usd
        )


@dataclass
class MarginResult:
    """마진 계산 결과."""
    asin: str
    week_key: str
    buybox_price_usd: float
    source_price_krw: float
    exchange_rate_used: float
    costs: CostBreakdown
    net_profit_usd: float
    roi: float           # net_profit / total_cost
    margin_rate: float   # net_profit / buybox_price
    target_roi: float
    status: EligibilityStatus
    cost_snapshot: dict  # 불변성 보장용 비정규화 스냅샷
    missing_fields: list[str] = field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        return self.costs.total

    def is_eligible(self) -> bool:
        return self.status == EligibilityStatus.ELIGIBLE


# ══════════════════════════════════════════════════════════════════════════════
# 메인 계산기
# ══════════════════════════════════════════════════════════════════════════════

class DynamicMarginCalculator:
    """
    WeeklyState의 비용 변수를 불러와 상품별 순수익·ROI를 동적으로 계산한다.
    필수 데이터가 하나라도 누락되면 추정하지 않고 DATA_MISSING으로 처리한다.
    """

    DEFAULT_TARGET_ROI: float = 0.30
    AMAZON_REFERRAL_FEE_FALLBACK: float = 0.15  # Amazon 공식 기본값 (카테고리 미지정 시)

    def __init__(self, db: Session, target_roi: float = DEFAULT_TARGET_ROI):
        self.db = db
        self.target_roi = target_roi
        self._wsm = WeeklyStateManager(db)

    # ──────────────────────────────────────────────────────────────────────────
    # 공개 API
    # ──────────────────────────────────────────────────────────────────────────

    def calculate(
        self,
        item: MarginInput,
        week_key: Optional[str] = None,
    ) -> MarginResult:
        """단일 상품 마진 계산. 데이터 누락 시 DATA_MISSING 결과를 반환한다."""
        wk = week_key or get_current_week_key()

        state = self._wsm.get_state_for_week(wk)
        if state is None:
            logger.error(
                "[DATA_MISSING] asin=%s week=%s — WeeklyState 미존재. "
                "먼저 이번 주차 비용 데이터를 등록해 주세요.",
                item.asin, wk,
            )
            return self._missing_result(item, wk, ["WeeklyState not found for week"])

        missing = self._validate_required_fields(state, item)
        if missing:
            logger.error(
                "[DATA_MISSING] asin=%s week=%s — 필수 필드 누락: %s",
                item.asin, wk, missing,
            )
            return self._missing_result(item, wk, missing)

        return self._compute(item, wk, state)

    def batch_calculate(
        self,
        items: list[MarginInput],
        week_key: Optional[str] = None,
    ) -> list[MarginResult]:
        """여러 상품을 일괄 계산한다. DATA_MISSING 상품도 결과 목록에 포함된다."""
        return [self.calculate(item, week_key) for item in items]

    def filter_eligible(
        self,
        items: list[MarginInput],
        week_key: Optional[str] = None,
    ) -> list[MarginResult]:
        """ROI >= target_roi 인 ELIGIBLE 상품만 반환한다."""
        results = self.batch_calculate(items, week_key)
        eligible = [r for r in results if r.is_eligible()]
        logger.info(
            "Eligibility filter: %d/%d passed (target ROI=%.0f%%)",
            len(eligible), len(results), self.target_roi * 100,
        )
        return eligible

    # ──────────────────────────────────────────────────────────────────────────
    # 내부 계산 로직
    # ──────────────────────────────────────────────────────────────────────────

    def _compute(
        self, item: MarginInput, wk: str, state: WeeklyState
    ) -> MarginResult:
        exchange_rate = state.exchange_rate_usd_krw
        source_price_usd = item.source_price_krw / exchange_rate

        fba_fee_usd = item.fba_fee_usd if item.fba_fee_usd is not None else state.fba_fee_override
        referral_rate = state.amazon_referral_fee_rate or self.AMAZON_REFERRAL_FEE_FALLBACK
        referral_fee_usd = item.buybox_price_usd * referral_rate
        customs_duty_usd = source_price_usd * state.customs_duty_rate

        costs = CostBreakdown(
            source_price_usd=round(source_price_usd, 6),
            domestic_shipping_usd=state.domestic_shipping_cost,
            international_shipping_usd=state.international_shipping_cost,
            fba_fee_usd=fba_fee_usd,
            referral_fee_usd=round(referral_fee_usd, 6),
            prep_fee_usd=state.prep_service_fee,
            customs_duty_usd=round(customs_duty_usd, 6),
            misc_cost_usd=state.misc_cost_per_unit,
        )

        net_profit_usd = item.buybox_price_usd - costs.total
        roi = net_profit_usd / costs.total if costs.total else 0.0
        margin_rate = net_profit_usd / item.buybox_price_usd if item.buybox_price_usd else 0.0

        status = (
            EligibilityStatus.ELIGIBLE
            if roi >= self.target_roi
            else EligibilityStatus.INELIGIBLE
        )

        logger.info(
            "[MARGIN] asin=%s buybox=$%.2f cost=$%.2f profit=$%.2f "
            "roi=%.1f%% status=%s",
            item.asin,
            item.buybox_price_usd,
            costs.total,
            net_profit_usd,
            roi * 100,
            status.value,
        )

        return MarginResult(
            asin=item.asin,
            week_key=wk,
            buybox_price_usd=item.buybox_price_usd,
            source_price_krw=item.source_price_krw,
            exchange_rate_used=exchange_rate,
            costs=costs,
            net_profit_usd=round(net_profit_usd, 6),
            roi=round(roi, 6),
            margin_rate=round(margin_rate, 6),
            target_roi=self.target_roi,
            status=status,
            cost_snapshot={
                "week_key": wk,
                "exchange_rate_usd_krw": exchange_rate,
                "domestic_shipping_usd": state.domestic_shipping_cost,
                "international_shipping_usd": state.international_shipping_cost,
                "fba_fee_usd": fba_fee_usd,
                "referral_fee_rate": referral_rate,
                "prep_fee_usd": state.prep_service_fee,
                "customs_duty_rate": state.customs_duty_rate,
                "misc_cost_usd": state.misc_cost_per_unit,
            },
        )

    def _validate_required_fields(
        self, state: WeeklyState, item: MarginInput
    ) -> list[str]:
        """
        반드시 있어야 하는 값들을 검증한다.
        추정 불가 항목만 체크 — referral_rate는 Amazon 공식 기본값이 존재하므로 제외.
        """
        missing: list[str] = []
        if not state.exchange_rate_usd_krw:
            missing.append("exchange_rate_usd_krw")
        if item.fba_fee_usd is None and state.fba_fee_override is None:
            missing.append("fba_fee (item.fba_fee_usd 또는 WeeklyState.fba_fee_override 필요)")
        return missing

    def _missing_result(
        self, item: MarginInput, wk: str, missing_fields: list[str]
    ) -> MarginResult:
        zero_costs = CostBreakdown(
            source_price_usd=0.0,
            domestic_shipping_usd=0.0,
            international_shipping_usd=0.0,
            fba_fee_usd=0.0,
            referral_fee_usd=0.0,
            prep_fee_usd=0.0,
            customs_duty_usd=0.0,
            misc_cost_usd=0.0,
        )
        return MarginResult(
            asin=item.asin,
            week_key=wk,
            buybox_price_usd=item.buybox_price_usd,
            source_price_krw=item.source_price_krw,
            exchange_rate_used=0.0,
            costs=zero_costs,
            net_profit_usd=0.0,
            roi=0.0,
            margin_rate=0.0,
            target_roi=self.target_roi,
            status=EligibilityStatus.DATA_MISSING,
            cost_snapshot={"error": "DATA_MISSING", "missing_fields": missing_fields, "week_key": wk},
            missing_fields=missing_fields,
        )
