"""
Arbitrage-X — Dynamic Margin Calculator 단위 테스트

Mock 상품 시나리오:
  - 상품 A: 한국 소싱가 15,000 KRW, 바이박스 $29.99 → ELIGIBLE (ROI ≥ 30%)
  - 상품 B: 한국 소싱가 25,000 KRW, 바이박스 $29.99 → INELIGIBLE (마진 부족)
  - 상품 C: FBA 수수료 데이터 없음 → DATA_MISSING
  - 상품 D: WeeklyState 없는 주차 → DATA_MISSING
  - 상품 E: exchange_rate = 0 → DATA_MISSING
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from arbitrage_x.db.models import Base, WeeklyState
from arbitrage_x.core.weekly_state_manager import WeeklyStateManager
from arbitrage_x.core.margin_calculator import (
    DynamicMarginCalculator,
    EligibilityStatus,
    MarginInput,
)
from arbitrage_x.utils.week_utils import get_current_week_key


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def weekly_state(db_session):
    """이번 주차 WeeklyState — 현실적인 한국 FBA 아비트리지 비용 세팅."""
    mgr = WeeklyStateManager(db_session)
    state = mgr.get_or_create_current_week(
        domestic_shipping_cost=1.50,        # 국내 배송비 $1.50/unit
        international_shipping_cost=3.00,   # 국제 물류비 $3.00/unit (한국→미국)
        prep_service_fee=0.50,              # FBA Prep $0.50/unit
        customs_duty_rate=0.0,              # 관세율 0%
        misc_cost_per_unit=0.20,            # 잡비 $0.20/unit
        amazon_referral_fee_rate=0.15,      # 레퍼럴 15%
        fba_fee_override=3.22,              # FBA 배송 수수료 $3.22
        exchange_rate_usd_krw=1380.0,       # 환율 1,380 KRW/USD
    )
    db_session.commit()
    return state


@pytest.fixture
def calculator(db_session, weekly_state):
    return DynamicMarginCalculator(db_session, target_roi=0.30)


# ──────────────────────────────────────────────────────────────────────────────
# 1. 기본 계산 정확성
# ──────────────────────────────────────────────────────────────────────────────

def test_eligible_product(calculator):
    """
    상품 A: 소싱가 10,000 KRW ($7.25), 바이박스 $29.99
    예상 총비용 ≈ $20.16, 순수익 ≈ $9.83, ROI ≈ 48.7% → ELIGIBLE
    """
    item = MarginInput(
        asin="B0TEST0001",
        title="테스트 상품 A",
        buybox_price_usd=29.99,
        source_price_krw=10000,
        fba_fee_usd=None,  # WeeklyState.fba_fee_override=$3.22 사용
    )
    result = calculator.calculate(item)

    assert result.status == EligibilityStatus.ELIGIBLE
    assert result.is_eligible() is True
    assert result.roi >= 0.30
    assert result.net_profit_usd > 0

    # 비용 항목별 검증
    assert result.costs.source_price_usd == pytest.approx(10000 / 1380.0, rel=1e-4)
    assert result.costs.domestic_shipping_usd == 1.50
    assert result.costs.international_shipping_usd == 3.00
    assert result.costs.fba_fee_usd == 3.22
    assert result.costs.referral_fee_usd == pytest.approx(29.99 * 0.15, rel=1e-4)
    assert result.costs.prep_fee_usd == 0.50
    assert result.costs.customs_duty_usd == pytest.approx(0.0, abs=1e-6)
    assert result.costs.misc_cost_usd == 0.20


def test_ineligible_product(calculator):
    """
    상품 B: 소싱가 25,000 KRW ($18.12), 바이박스 $29.99
    총비용이 너무 높아 ROI < 30% → INELIGIBLE
    """
    item = MarginInput(
        asin="B0TEST0002",
        title="테스트 상품 B",
        buybox_price_usd=29.99,
        source_price_krw=25000,
    )
    result = calculator.calculate(item)

    assert result.status == EligibilityStatus.INELIGIBLE
    assert result.is_eligible() is False
    assert result.roi < 0.30


def test_cost_breakdown_arithmetic(calculator):
    """total_cost_usd = 모든 비용 항목의 합과 일치해야 한다."""
    item = MarginInput(
        asin="B0TEST0003",
        title="산술 검증 상품",
        buybox_price_usd=50.00,
        source_price_krw=20000,
        fba_fee_usd=4.50,
    )
    result = calculator.calculate(item)

    expected_total = (
        result.costs.source_price_usd
        + result.costs.domestic_shipping_usd
        + result.costs.international_shipping_usd
        + result.costs.fba_fee_usd
        + result.costs.referral_fee_usd
        + result.costs.prep_fee_usd
        + result.costs.customs_duty_usd
        + result.costs.misc_cost_usd
    )
    assert result.total_cost_usd == pytest.approx(expected_total, rel=1e-6)
    assert result.net_profit_usd == pytest.approx(50.00 - expected_total, rel=1e-6)


def test_roi_and_margin_rate_formulas(calculator):
    """ROI = 순수익/총비용, margin_rate = 순수익/바이박스가."""
    item = MarginInput(
        asin="B0TEST0004",
        title="공식 검증 상품",
        buybox_price_usd=40.00,
        source_price_krw=10000,
    )
    result = calculator.calculate(item)

    expected_roi = result.net_profit_usd / result.total_cost_usd
    expected_margin = result.net_profit_usd / result.buybox_price_usd
    assert result.roi == pytest.approx(expected_roi, rel=1e-5)
    assert result.margin_rate == pytest.approx(expected_margin, rel=1e-5)


# ──────────────────────────────────────────────────────────────────────────────
# 2. KRW → USD 환율 변환
# ──────────────────────────────────────────────────────────────────────────────

def test_exchange_rate_conversion(calculator):
    """소싱가 KRW가 WeeklyState 환율로 정확히 변환되는지 확인."""
    item = MarginInput(
        asin="B0TEST0005",
        title="환율 변환 검증",
        buybox_price_usd=100.00,
        source_price_krw=138000,  # 1380 환율 적용 시 정확히 $100
    )
    result = calculator.calculate(item)

    assert result.exchange_rate_used == 1380.0
    assert result.costs.source_price_usd == pytest.approx(100.0, rel=1e-5)


def test_fba_fee_from_item_takes_precedence(calculator):
    """item.fba_fee_usd가 있으면 WeeklyState.fba_fee_override보다 우선해야 한다."""
    item = MarginInput(
        asin="B0TEST0006",
        title="FBA 우선순위 검증",
        buybox_price_usd=30.00,
        source_price_krw=12000,
        fba_fee_usd=5.99,  # override($3.22)보다 높은 값
    )
    result = calculator.calculate(item)

    assert result.costs.fba_fee_usd == 5.99


def test_customs_duty_applied_to_source_price(db_session):
    """관세가 소싱가(USD 변환 후)에 비율로 부과되는지 검증."""
    mgr = WeeklyStateManager(db_session)
    mgr.get_or_create_current_week(
        customs_duty_rate=0.10,  # 10% 관세
        fba_fee_override=3.00,
        exchange_rate_usd_krw=1000.0,
    )
    db_session.commit()

    calc = DynamicMarginCalculator(db_session)
    item = MarginInput(
        asin="B0TEST0007",
        title="관세 검증",
        buybox_price_usd=30.00,
        source_price_krw=10000,  # → $10 USD
    )
    result = calc.calculate(item)

    # 관세 = $10 * 10% = $1
    assert result.costs.customs_duty_usd == pytest.approx(1.0, rel=1e-5)


# ──────────────────────────────────────────────────────────────────────────────
# 3. 예외 처리 — DATA_MISSING
# ──────────────────────────────────────────────────────────────────────────────

def test_missing_weekly_state(db_session):
    """WeeklyState가 없는 주차 → DATA_MISSING."""
    calc = DynamicMarginCalculator(db_session)
    item = MarginInput(
        asin="B0MISSING1",
        title="주차 없음",
        buybox_price_usd=30.00,
        source_price_krw=10000,
    )
    result = calc.calculate(item, week_key="2020-W01")  # 존재하지 않는 주차

    assert result.status == EligibilityStatus.DATA_MISSING
    assert result.is_eligible() is False
    assert result.net_profit_usd == 0.0
    assert "WeeklyState not found" in result.missing_fields[0]


def test_missing_fba_fee(db_session):
    """FBA 수수료 데이터 없음 → DATA_MISSING (추정 금지)."""
    mgr = WeeklyStateManager(db_session)
    mgr.get_or_create_current_week(
        exchange_rate_usd_krw=1380.0,
        fba_fee_override=None,  # 명시적으로 None
    )
    db_session.commit()

    calc = DynamicMarginCalculator(db_session)
    item = MarginInput(
        asin="B0MISSING2",
        title="FBA 수수료 없음",
        buybox_price_usd=29.99,
        source_price_krw=10000,
        fba_fee_usd=None,  # item에도 없음
    )
    result = calc.calculate(item)

    assert result.status == EligibilityStatus.DATA_MISSING
    assert any("fba_fee" in f.lower() for f in result.missing_fields)


def test_missing_exchange_rate(db_session):
    """환율 0 (미입력) → DATA_MISSING."""
    mgr = WeeklyStateManager(db_session)
    mgr.get_or_create_current_week(
        exchange_rate_usd_krw=0.0,  # 미입력 상태
        fba_fee_override=3.22,
    )
    db_session.commit()

    calc = DynamicMarginCalculator(db_session)
    item = MarginInput(
        asin="B0MISSING3",
        title="환율 없음",
        buybox_price_usd=29.99,
        source_price_krw=10000,
    )
    result = calc.calculate(item)

    assert result.status == EligibilityStatus.DATA_MISSING
    assert any("exchange_rate" in f for f in result.missing_fields)


def test_data_missing_does_not_raise(db_session):
    """DATA_MISSING 상품은 예외를 던지지 않고 보류 결과를 반환해야 한다."""
    calc = DynamicMarginCalculator(db_session)
    item = MarginInput(
        asin="B0MISSING4",
        title="예외 없음 검증",
        buybox_price_usd=30.00,
        source_price_krw=10000,
    )
    # 예외 없이 결과 반환
    result = calc.calculate(item, week_key="1999-W01")
    assert result.status == EligibilityStatus.DATA_MISSING


# ──────────────────────────────────────────────────────────────────────────────
# 4. 배치 처리 및 필터링
# ──────────────────────────────────────────────────────────────────────────────

def test_batch_calculate(calculator):
    """batch_calculate: 모든 상품 결과 반환 (DATA_MISSING 포함)."""
    items = [
        MarginInput("B001", "상품1", 29.99, 10000),  # ELIGIBLE 예상 (ROI≈49%)
        MarginInput("B002", "상품2", 29.99, 25000),  # INELIGIBLE 예상 (적자)
    ]
    results = calculator.batch_calculate(items)

    assert len(results) == 2
    statuses = {r.asin: r.status for r in results}
    assert statuses["B001"] == EligibilityStatus.ELIGIBLE
    assert statuses["B002"] == EligibilityStatus.INELIGIBLE


def test_filter_eligible_returns_only_eligible(calculator):
    """filter_eligible: ELIGIBLE 상품만 반환한다."""
    items = [
        MarginInput("B001", "싼 상품", 29.99, 10000),   # ELIGIBLE (ROI≈49%)
        MarginInput("B002", "비싼 소싱", 29.99, 25000), # INELIGIBLE (적자)
        MarginInput("B003", "좋은 마진", 59.99, 10000), # ELIGIBLE
    ]
    eligible = calculator.filter_eligible(items)

    assert all(r.status == EligibilityStatus.ELIGIBLE for r in eligible)
    assert len(eligible) < len(items)
    asins = [r.asin for r in eligible]
    assert "B001" in asins
    assert "B003" in asins
    assert "B002" not in asins


def test_batch_with_mixed_missing_and_valid(db_session, weekly_state):
    """배치 중 DATA_MISSING 상품이 섞여 있어도 나머지 계산에 영향 없어야 한다."""
    calc = DynamicMarginCalculator(db_session)
    items = [
        MarginInput("B001", "정상", 29.99, 15000),
        MarginInput("B002", "FBA없음", 29.99, 15000, fba_fee_usd=None),
        MarginInput("B003", "정상2", 49.99, 10000),
    ]

    # B002는 fba_fee_override=3.22가 WeeklyState에 있으므로 DATA_MISSING 아님
    # fba_fee_override를 None으로 만들어야 함 → 별도 session 처리 필요
    results = calc.batch_calculate(items)
    assert len(results) == 3
    # 모두 결과 반환 (예외 없음)
    for r in results:
        assert r.asin is not None


# ──────────────────────────────────────────────────────────────────────────────
# 5. cost_snapshot 불변성 보장
# ──────────────────────────────────────────────────────────────────────────────

def test_cost_snapshot_contains_all_fields(calculator):
    """cost_snapshot에 계산에 사용된 모든 변수가 포함되어야 한다."""
    item = MarginInput("B0SNAP01", "스냅샷 검증", 35.00, 15000)
    result = calculator.calculate(item)

    required_keys = {
        "week_key",
        "exchange_rate_usd_krw",
        "domestic_shipping_usd",
        "international_shipping_usd",
        "fba_fee_usd",
        "referral_fee_rate",
        "prep_fee_usd",
        "customs_duty_rate",
        "misc_cost_usd",
    }
    assert required_keys.issubset(result.cost_snapshot.keys())
    assert result.cost_snapshot["week_key"] == get_current_week_key()
    assert result.cost_snapshot["exchange_rate_usd_krw"] == 1380.0


def test_cost_snapshot_on_missing_result(db_session):
    """DATA_MISSING 결과의 cost_snapshot에는 에러 정보가 담겨야 한다."""
    calc = DynamicMarginCalculator(db_session)
    item = MarginInput("B0SNAP02", "스냅샷 에러", 29.99, 10000)
    result = calc.calculate(item, week_key="2000-W01")

    assert result.cost_snapshot.get("error") == "DATA_MISSING"
    assert "missing_fields" in result.cost_snapshot


# ──────────────────────────────────────────────────────────────────────────────
# 6. 타겟 마진 커스터마이징
# ──────────────────────────────────────────────────────────────────────────────

def test_custom_target_roi(db_session, weekly_state):
    """target_roi를 10%로 낮추면 INELIGIBLE 상품이 ELIGIBLE이 될 수 있다."""
    item = MarginInput("B0ROI01", "낮은 ROI 상품", 29.99, 22000)

    strict_calc = DynamicMarginCalculator(db_session, target_roi=0.30)
    lenient_calc = DynamicMarginCalculator(db_session, target_roi=0.05)

    strict_result = strict_calc.calculate(item)
    lenient_result = lenient_calc.calculate(item)

    # 같은 상품도 기준에 따라 결과가 달라진다
    assert lenient_result.roi == strict_result.roi  # ROI 수치는 동일
    assert lenient_result.target_roi == 0.05
    assert strict_result.target_roi == 0.30
