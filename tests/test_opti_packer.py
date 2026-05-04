"""
Arbitrage-X — Opti-Packer 단위 테스트

가상 상품 4종:
  P1 이어폰 케이스  : 8×5×3 cm, 0.10 kg, 마진 $4.50  → 마진 밀도 최상
  P2 스마트폰 케이스: 16×8×1.5 cm, 0.08 kg, 마진 $6.00 → 얇고 가벼움
  P3 무선 충전 패드 : 12×12×1 cm, 0.25 kg, 마진 $7.00  → 납작하지만 무거움
  P4 블루투스 스피커: 20×15×10 cm, 0.80 kg, 마진 $12.00 → 크고 무거움

시나리오:
  A. 단일 이어폰 케이스 → 우체국 5호에 최대 수량 패킹 (치수 & 무게 제약 확인)
  B. 4종 혼합 → KR_POST_BOXES 전체 팩 & 황금 박스 선택 (ROI 기준)
  C. 너무 큰 상품 → 박스에 맞지 않으면 해당 사이즈 결과 없음
  D. 무게 초과 → 무게 제약 내에서만 적재
  E. 마진 밀도 정렬 → 고밀도 상품이 먼저 채워지는지 확인
  F. 수익성 없는 구성 → find_golden_box가 None 반환
  G. DB 아카이빙 → archive() 후 get_recommendations() / get_top_by_roi() 검증
  H. 기존 BoxOptimizer API → 기존 테스트 모두 통과 (회귀 방지)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arbitrage_x.db.models import Base, BoxRecommendation
from arbitrage_x.modules.box_optimizer import (
    BoxArchiver,
    BoxOptimizer,
    BoxSpec,
    ItemSpec,
    KR_POST_BOXES,
    MultiPackingResult,
    OptiPacker,
    ProductSpec,
)


# ──────────────────────────────────────────────────────────────────────────────
# 픽스처
# ──────────────────────────────────────────────────────────────────────────────

# 표준 우체국 5호 박스 (테스트 기준 박스)
BOX_5 = BoxSpec(id="KR-5호", name="우체국 5호", length=45, width=35, height=25, max_weight_kg=10.0)
BOX_4 = BoxSpec(id="KR-4호", name="우체국 4호", length=35, width=25, height=20, max_weight_kg=5.0)


def _item(
    asin: str,
    title: str,
    l: float, w: float, h: float,
    weight: float,
    margin: float,
    qty: int = 50,
) -> ItemSpec:
    return ItemSpec(
        asin=asin, title=title,
        length_cm=l, width_cm=w, height_cm=h,
        weight_kg=weight, unit_margin_usd=margin, quantity=qty,
    )


P1 = _item("B001", "이어폰 케이스",   8,  5,  3,  0.10,  4.50, qty=50)
P2 = _item("B002", "스마트폰 케이스", 16,  8,  1.5, 0.08, 6.00, qty=30)
P3 = _item("B003", "무선 충전 패드",  12, 12,  1,  0.25,  7.00, qty=20)
P4 = _item("B004", "블루투스 스피커", 20, 15, 10,  0.80, 12.00, qty=10)


@pytest.fixture
def packer_5():
    """우체국 5호 박스만 사용하는 OptiPacker."""
    return OptiPacker(boxes=[BOX_5], shipping_rate_usd_per_kg=8.0)


@pytest.fixture
def packer_kr():
    """우체국 전 사이즈 박스를 사용하는 OptiPacker."""
    return OptiPacker(boxes=KR_POST_BOXES, shipping_rate_usd_per_kg=8.0)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


# ──────────────────────────────────────────────────────────────────────────────
# A. 단일 상품 패킹 — 치수·무게 제약 동시 검증
# ──────────────────────────────────────────────────────────────────────────────

def test_single_item_packs_within_weight_limit(packer_5):
    """이어폰 케이스를 5호 박스에 담을 때 총 무게가 max_weight_kg 이하여야 한다."""
    results = packer_5.pack([P1])
    assert len(results) == 1
    r = results[0]
    assert r.actual_weight_kg <= BOX_5.max_weight_kg


def test_single_item_packs_within_volume_limit(packer_5):
    """이어폰 케이스 적재 부피 합이 박스 부피 이하여야 한다."""
    results = packer_5.pack([P1])
    r = results[0]
    total_vol = sum(pi.total_volume_cm3 for pi in r.packed_items)
    assert total_vol <= BOX_5.volume_cm3


def test_single_item_quantity_not_exceed_available(packer_5):
    """적재 수량이 ItemSpec.quantity를 초과해선 안 된다."""
    item = _item("B001", "이어폰 케이스", 8, 5, 3, 0.10, 4.50, qty=5)
    results = packer_5.pack([item])
    r = results[0]
    packed_qty = sum(pi.qty for pi in r.packed_items if pi.item.asin == "B001")
    assert packed_qty <= 5


# ──────────────────────────────────────────────────────────────────────────────
# B. 4종 혼합 패킹 & 황금 박스 선택
# ──────────────────────────────────────────────────────────────────────────────

def test_mixed_items_pack_returns_results(packer_kr):
    """4종 혼합 상품을 전체 박스 사이즈에 패킹하면 하나 이상 유효한 결과가 나와야 한다."""
    results = packer_kr.pack([P1, P2, P3, P4])
    assert len(results) > 0


def test_golden_box_has_highest_roi(packer_kr):
    """find_golden_box가 반환하는 구성이 모든 후보 중 ROI가 가장 높아야 한다."""
    all_results = [r for r in packer_kr.pack([P1, P2, P3, P4]) if r.is_profitable]
    golden = packer_kr.find_golden_box([P1, P2, P3, P4])

    assert golden is not None
    assert golden.is_profitable
    assert all(golden.roi >= r.roi for r in all_results)


def test_golden_box_is_profitable(packer_kr):
    """황금 박스 구성의 net_margin_usd가 양수여야 한다."""
    golden = packer_kr.find_golden_box([P1, P2, P3, P4])
    assert golden is not None
    assert golden.net_margin_usd > 0


def test_mixed_packing_all_items_fit_in_box(packer_kr):
    """패킹된 모든 상품이 해당 박스에 치수상 들어가야 한다."""
    results = packer_kr.pack([P1, P2, P3, P4])
    for result in results:
        box = result.box
        for pi in result.packed_items:
            item = pi.item
            dims = sorted([item.length_cm, item.width_cm, item.height_cm])
            box_dims = sorted([box.length_cm, box.width_cm, box.height_cm])
            # 가장 작은 회전이 박스에 들어가는지
            assert all(d <= b for d, b in zip(dims, box_dims)), (
                f"{item.asin} ({dims}) does not fit in box {box.id} ({box_dims})"
            )


# ──────────────────────────────────────────────────────────────────────────────
# C. 너무 큰 상품 제외
# ──────────────────────────────────────────────────────────────────────────────

def test_oversized_item_excluded_from_small_box():
    """박스보다 큰 상품은 해당 박스 결과에 포함되지 않는다."""
    small_box = BoxSpec(id="TINY", name="작은 박스", length=10, width=10, height=10, max_weight_kg=2.0)
    packer = OptiPacker(boxes=[small_box], shipping_rate_usd_per_kg=8.0)
    giant = _item("B999", "거대 상품", 50, 50, 50, 5.0, 20.0, qty=1)

    results = packer.pack([giant])
    assert len(results) == 0


def test_oversized_item_mixed_with_small_items():
    """박스보다 큰 상품과 작은 상품 혼합 시, 큰 상품만 제외되고 작은 상품은 패킹된다."""
    small_box = BoxSpec(id="TINY", name="작은 박스", length=10, width=10, height=10, max_weight_kg=5.0)
    packer = OptiPacker(boxes=[small_box], shipping_rate_usd_per_kg=8.0)
    giant = _item("B999", "거대 상품", 50, 50, 50, 5.0, 20.0, qty=1)
    tiny = _item("B001", "작은 상품", 5, 5, 5, 0.1, 3.0, qty=10)

    results = packer.pack([giant, tiny])
    assert len(results) == 1
    packed_asins = {pi.item.asin for pi in results[0].packed_items}
    assert "B001" in packed_asins
    assert "B999" not in packed_asins


# ──────────────────────────────────────────────────────────────────────────────
# D. 무게 제약 검증
# ──────────────────────────────────────────────────────────────────────────────

def test_weight_constraint_respected(packer_5):
    """무거운 상품 패킹 후 총 무게가 박스 최대 무게 이하여야 한다."""
    heavy = _item("B010", "무거운 상품", 10, 10, 10, 2.5, 15.0, qty=20)
    results = packer_5.pack([heavy])
    assert len(results) == 1
    r = results[0]
    assert r.actual_weight_kg <= BOX_5.max_weight_kg + 1e-6


def test_single_item_heavier_than_box_excluded():
    """단일 아이템이 박스 최대 무게 초과 → 결과 없음."""
    tiny_box = BoxSpec(id="LIGHT", name="가벼운 박스", length=50, width=50, height=50, max_weight_kg=1.0)
    packer = OptiPacker(boxes=[tiny_box])
    heavy = _item("B010", "무거운 상품", 10, 10, 10, 2.0, 15.0, qty=5)
    results = packer.pack([heavy])
    assert len(results) == 0


# ──────────────────────────────────────────────────────────────────────────────
# E. 마진 밀도 정렬 — 고밀도 상품 우선 적재
# ──────────────────────────────────────────────────────────────────────────────

def test_high_margin_density_item_packed_first(packer_5):
    """마진 밀도가 높은 상품이 공간 경쟁에서 우선 적재된다."""
    high_density = _item("HIGH", "고밀도 상품", 5, 5, 5, 0.05, 10.0, qty=3)   # density=10/(5*5*5)=0.08
    low_density  = _item("LOW",  "저밀도 상품", 5, 5, 5, 0.05,  1.0, qty=200)  # density=1/(5*5*5)=0.008

    results = packer_5.pack([high_density, low_density])
    assert len(results) == 1
    packed = {pi.item.asin: pi.qty for pi in results[0].packed_items}
    # 고밀도 상품은 available_quantity=3 모두 채워져야 함
    assert packed.get("HIGH") == 3


# ──────────────────────────────────────────────────────────────────────────────
# F. 수익성 없는 구성 → find_golden_box None 반환
# ──────────────────────────────────────────────────────────────────────────────

def test_find_golden_box_returns_none_when_no_profit():
    """배송비가 마진보다 높은 경우 황금 박스 없음."""
    # 배송비 = chargeable_kg × 500 USD/kg → 마진 $1 × 수량 몇 개로는 절대 흑자 불가
    packer = OptiPacker(boxes=[BOX_5], shipping_rate_usd_per_kg=500.0)
    tiny_margin = _item("B001", "마진 없음", 5, 5, 5, 0.1, 1.0, qty=1)
    golden = packer.find_golden_box([tiny_margin])
    assert golden is None


# ──────────────────────────────────────────────────────────────────────────────
# G. DB 아카이빙
# ──────────────────────────────────────────────────────────────────────────────

def test_archive_saves_recommendation(db_session, packer_kr):
    """archive()가 BoxRecommendation 레코드를 올바르게 저장한다."""
    golden = packer_kr.find_golden_box([P1, P2, P3])
    assert golden is not None

    archiver = BoxArchiver()
    rec = archiver.archive(db_session, golden, week_key="2026-W19", label="황금 박스 #1")
    db_session.commit()

    assert rec.id is not None
    assert rec.week_key == "2026-W19"
    assert rec.label == "황금 박스 #1"
    assert rec.roi == pytest.approx(golden.roi, abs=1e-6)
    assert rec.net_margin_usd == pytest.approx(golden.net_margin_usd, abs=1e-6)
    assert rec.chargeable_weight_kg == pytest.approx(golden.chargeable_weight_kg, abs=1e-4)
    assert isinstance(rec.packing_detail, list)
    assert len(rec.packing_detail) > 0


def test_archive_stores_item_breakdown(db_session, packer_5):
    """packing_detail JSON에 적재 상품별 ASIN, 수량, 마진 정보가 담겨야 한다."""
    result = packer_5.pack([P1])[0]
    archiver = BoxArchiver()
    rec = archiver.archive(db_session, result, week_key="2026-W19")
    db_session.commit()

    detail = rec.packing_detail
    assert len(detail) >= 1
    first_entry = detail[0]
    assert "asin" in first_entry
    assert "qty" in first_entry
    assert "total_margin_usd" in first_entry


def test_get_recommendations_ordered_by_roi(db_session, packer_kr):
    """get_recommendations()가 ROI 내림차순으로 반환한다."""
    archiver = BoxArchiver()
    for item_set in [[P1], [P2], [P3, P4]]:
        results = packer_kr.pack(item_set)
        if results:
            archiver.archive(db_session, results[0], week_key="2026-W19")
    db_session.commit()

    recs = archiver.get_recommendations(db_session, week_key="2026-W19")
    assert len(recs) > 0
    rois = [r.roi for r in recs if r.roi is not None]
    assert rois == sorted(rois, reverse=True)


def test_get_top_by_roi_limits_results(db_session, packer_kr):
    """get_top_by_roi(top_n=2)는 최대 2개만 반환한다."""
    archiver = BoxArchiver()
    for item_set in [[P1], [P2], [P3], [P4]]:
        results = packer_kr.pack(item_set)
        if results:
            archiver.archive(db_session, results[0], week_key="2026-W19")
    db_session.commit()

    top2 = archiver.get_top_by_roi(db_session, week_key="2026-W19", top_n=2)
    assert len(top2) <= 2


def test_get_recommendations_filters_by_week(db_session, packer_5):
    """week_key 필터가 올바르게 동작한다."""
    archiver = BoxArchiver()
    result = packer_5.pack([P1])[0]
    archiver.archive(db_session, result, week_key="2026-W18")
    archiver.archive(db_session, result, week_key="2026-W19")
    db_session.commit()

    recs_w18 = archiver.get_recommendations(db_session, week_key="2026-W18")
    recs_w19 = archiver.get_recommendations(db_session, week_key="2026-W19")
    assert all(r.week_key == "2026-W18" for r in recs_w18)
    assert all(r.week_key == "2026-W19" for r in recs_w19)


# ──────────────────────────────────────────────────────────────────────────────
# H. DIM 무게 vs 실 무게 청구 기준
# ──────────────────────────────────────────────────────────────────────────────

def test_chargeable_weight_is_max_of_actual_and_dim(packer_5):
    """청구 무게 = max(실 무게, DIM 무게)."""
    result = packer_5.pack([P1])[0]
    expected_chargeable = max(result.actual_weight_kg, result.dimensional_weight_kg)
    assert result.chargeable_weight_kg == pytest.approx(expected_chargeable, abs=1e-4)


def test_dim_weight_formula(packer_5):
    """DIM 무게 = 박스 부피(cm³) / 5000."""
    result = packer_5.pack([P1])[0]
    expected_dim = BOX_5.volume_cm3 / 5000
    assert result.dimensional_weight_kg == pytest.approx(expected_dim, abs=1e-4)


def test_shipping_cost_uses_chargeable_weight(packer_5):
    """배송비 = 청구 무게 × 배송 단가."""
    result = packer_5.pack([P1])[0]
    expected_cost = result.chargeable_weight_kg * 8.0
    assert result.shipping_cost_usd == pytest.approx(expected_cost, abs=1e-4)


# ──────────────────────────────────────────────────────────────────────────────
# I. 마진 산술 검증
# ──────────────────────────────────────────────────────────────────────────────

def test_net_margin_equals_total_margin_minus_shipping(packer_5):
    """net_margin = total_margin - shipping_cost."""
    result = packer_5.pack([P1])[0]
    assert result.net_margin_usd == pytest.approx(
        result.total_margin_usd - result.shipping_cost_usd, abs=1e-4
    )


def test_roi_formula(packer_5):
    """ROI = net_margin / shipping_cost."""
    result = packer_5.pack([P1])[0]
    expected_roi = result.net_margin_usd / result.shipping_cost_usd
    assert result.roi == pytest.approx(expected_roi, rel=1e-5)


def test_total_items_count(packer_5):
    """total_items = packed_items 수량의 합."""
    result = packer_5.pack([P1, P2])[0]
    expected = sum(pi.qty for pi in result.packed_items)
    assert result.total_items == expected


# ──────────────────────────────────────────────────────────────────────────────
# J. BoxSpec 속성 호환성
# ──────────────────────────────────────────────────────────────────────────────

def test_box_spec_cm_aliases():
    """BoxSpec.length_cm 등 별칭이 length와 동일한 값이어야 한다."""
    box = BoxSpec(id="TEST", length=45, width=35, height=25, max_weight_kg=10.0)
    assert box.length_cm == box.length
    assert box.width_cm == box.width
    assert box.height_cm == box.height


def test_item_spec_margin_density():
    """margin_density = unit_margin / volume."""
    item = ItemSpec(asin="X", title="x", length_cm=10, width_cm=5, height_cm=2,
                    weight_kg=0.1, unit_margin_usd=10.0)
    assert item.margin_density == pytest.approx(10.0 / (10 * 5 * 2), rel=1e-6)


# ──────────────────────────────────────────────────────────────────────────────
# K. 기존 BoxOptimizer 회귀 테스트 (API 호환 보장)
# ──────────────────────────────────────────────────────────────────────────────

def _make_product(**kwargs):
    defaults = dict(asin="B0TEST", weight_kg=0.5, length_cm=10, width_cm=8, height_cm=5)
    defaults.update(kwargs)
    return ProductSpec(**defaults)


def test_legacy_basic_optimization():
    optimizer = BoxOptimizer()
    results = optimizer.optimize(_make_product(), total_units=50)
    assert len(results) > 0
    costs = [r.cost_per_unit for r in results]
    assert costs == sorted(costs)


def test_legacy_product_too_large():
    optimizer = BoxOptimizer()
    results = optimizer.optimize(
        _make_product(length_cm=200, width_cm=200, height_cm=200), total_units=10
    )
    assert len(results) == 0


def test_legacy_weight_limit():
    import math
    optimizer = BoxOptimizer()
    product = _make_product(weight_kg=7.0, length_cm=10, width_cm=10, height_cm=10)
    results = optimizer.optimize(product, total_units=10)
    for r in results:
        assert r.units_per_box * product.weight_kg <= r.box.max_weight_kg + 0.01


def test_legacy_best_returns_cheapest():
    optimizer = BoxOptimizer()
    results = optimizer.optimize(_make_product(), total_units=100)
    best = optimizer.best(results)
    assert best is not None
    assert best.cost_per_unit == min(r.cost_per_unit for r in results)
