"""
Arbitrage-X — Opti-Packer (박스 구성 최적화 & 아카이빙 모듈)

───────────────────────────────────────────────────────────────────────────────
[기존] BoxOptimizer — 단일 상품 타입을 여러 박스 사이즈에 배분 (단가 최소화)
[신규] OptiPacker   — 복수 상품 타입 혼합 패킹 + 마진 기반 황금 박스 선택
[신규] BoxArchiver  — 최적 구성 결과를 BoxRecommendation 테이블에 아카이빙

알고리즘 (OptiPacker):
  1. 6방향 회전 적용 3D 격자 패킹 (각 상품이 박스 치수에 맞는지 확인)
  2. 마진 밀도(USD/cm³) 내림차순 그리디 배분
  3. DIM 무게 = 박스 부피(cm³) ÷ 5000 (IATA 기준)
     실 청구 무게 = max(실 무게, DIM 무게)
  4. ROI = (총 마진 − 배송비) / 배송비 가 가장 높은 조합 → 황금 박스

표준 박스:
  - KR_POST_BOXES: 우체국 국제특급(EMS) 표준 사이즈
  - settings.AVAILABLE_BOX_SIZES: 기존 커스텀 박스 (BoxOptimizer 용)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from arbitrage_x.db.models import BoxRecommendation
from arbitrage_x.utils.week_utils import get_current_week_key
from config.settings import AVAILABLE_BOX_SIZES

import logging

logger = logging.getLogger(__name__)

# DIM factor (IATA 표준, cm 단위): 부피(cm³) / 5000 = DIM 무게(kg)
DIM_FACTOR: int = 5000


# ══════════════════════════════════════════════════════════════════════════════
# 공유 도메인 타입
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class BoxSpec:
    id: str
    length: float       # cm
    width: float        # cm
    height: float       # cm
    max_weight_kg: float
    name: str = ""      # 인간 친화 이름 (e.g. "우체국 5호")

    # ── _cm 별칭 (OptiPacker에서 일관된 네이밍으로 접근) ─────────────────────
    @property
    def length_cm(self) -> float:
        return self.length

    @property
    def width_cm(self) -> float:
        return self.width

    @property
    def height_cm(self) -> float:
        return self.height

    @property
    def volume_cm3(self) -> float:
        return self.length * self.width * self.height

    @property
    def dim_weight_kg(self) -> float:
        """DIM 무게 — 박스 전체 부피 기준 (IATA: cm³ / 5000)."""
        return self.volume_cm3 / DIM_FACTOR


# ── 우체국 국제특급(EMS) 표준 박스 ──────────────────────────────────────────
KR_POST_BOXES: list[BoxSpec] = [
    BoxSpec(id="KR-3호", name="우체국 3호", length=35, width=25, height=10, max_weight_kg=2.0),
    BoxSpec(id="KR-4호", name="우체국 4호", length=35, width=25, height=20, max_weight_kg=5.0),
    BoxSpec(id="KR-5호", name="우체국 5호", length=45, width=35, height=25, max_weight_kg=10.0),
    BoxSpec(id="KR-6호", name="우체국 6호", length=50, width=40, height=30, max_weight_kg=15.0),
    BoxSpec(id="KR-INT-M", name="국제 중형", length=55, width=40, height=40, max_weight_kg=20.0),
]


# ══════════════════════════════════════════════════════════════════════════════
# [기존] BoxOptimizer — 단일 상품, 비용 최소화
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class ProductSpec:
    asin: str
    weight_kg: float
    length_cm: float
    width_cm: float
    height_cm: float

    @property
    def volume_cm3(self) -> float:
        return self.length_cm * self.width_cm * self.height_cm


@dataclass
class PackingResult:
    box: BoxSpec
    units_per_box: int
    total_units: int
    total_boxes: int
    estimated_shipping_cost: float
    cost_per_unit: float
    utilization_pct: float
    dead_space_pct: float
    packing_detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "box_size_id": self.box.id,
            "box_dims": f"{self.box.length}×{self.box.width}×{self.box.height} cm",
            "units_per_box": self.units_per_box,
            "total_units": self.total_units,
            "total_boxes": self.total_boxes,
            "estimated_shipping_cost_usd": round(self.estimated_shipping_cost, 2),
            "cost_per_unit_usd": round(self.cost_per_unit, 4),
            "volume_utilization_pct": round(self.utilization_pct, 1),
            "dead_space_pct": round(self.dead_space_pct, 1),
            "packing_detail": self.packing_detail,
        }


class BoxOptimizer:
    """
    단일 상품 타입을 여러 박스 사이즈에 나눠 담는 비용 최소화 최적화기.
    (기존 API 호환 유지)
    """

    def __init__(
        self,
        box_sizes: Optional[list[dict]] = None,
        packing_efficiency: float = 0.80,
    ):
        raw = box_sizes or AVAILABLE_BOX_SIZES
        self.boxes = [BoxSpec(**b) for b in raw]
        self.packing_efficiency = packing_efficiency

    def optimize(
        self,
        product: ProductSpec,
        total_units: int,
        base_rate_per_kg: float = 0.80,
        flat_handling_fee: float = 2.50,
    ) -> list[PackingResult]:
        results = []
        for box in self.boxes:
            result = self._pack(product, box, total_units, base_rate_per_kg, flat_handling_fee)
            if result:
                results.append(result)
        results.sort(key=lambda r: r.cost_per_unit)
        return results

    def best(self, results: list[PackingResult]) -> Optional[PackingResult]:
        return results[0] if results else None

    def _pack(
        self,
        product: ProductSpec,
        box: BoxSpec,
        total_units: int,
        rate: float,
        handling: float,
    ) -> Optional[PackingResult]:
        usable_volume = box.volume_cm3 * self.packing_efficiency
        by_volume = int(usable_volume / product.volume_cm3) if product.volume_cm3 > 0 else 9999
        by_weight = int(box.max_weight_kg / product.weight_kg) if product.weight_kg > 0 else 9999

        units_per_box = min(by_volume, by_weight)
        if units_per_box <= 0:
            return None

        if (product.length_cm > box.length
                or product.width_cm > box.width
                or product.height_cm > box.height):
            return None

        total_boxes = math.ceil(total_units / units_per_box)
        per_box_actual = units_per_box * product.weight_kg
        per_box_dim = box.dim_weight_kg
        billable = max(per_box_actual, per_box_dim)
        shipping_per_box = billable * rate + handling
        total_shipping = shipping_per_box * total_boxes
        cost_per_unit = total_shipping / total_units

        used_volume = units_per_box * product.volume_cm3
        utilization = (used_volume / box.volume_cm3) * 100

        return PackingResult(
            box=box,
            units_per_box=units_per_box,
            total_units=total_units,
            total_boxes=total_boxes,
            estimated_shipping_cost=total_shipping,
            cost_per_unit=cost_per_unit,
            utilization_pct=utilization,
            dead_space_pct=100 - utilization,
            packing_detail={
                "by_volume_limit": by_volume,
                "by_weight_limit": by_weight,
                "per_box_actual_weight_kg": round(per_box_actual, 3),
                "per_box_dim_weight_kg": round(per_box_dim, 3),
                "billable_weight_kg": round(billable, 3),
                "shipping_per_box_usd": round(shipping_per_box, 2),
            },
        )


# ══════════════════════════════════════════════════════════════════════════════
# [신규] OptiPacker — 복수 상품 혼합 패킹, 마진 극대화
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class ItemSpec:
    """
    OptiPacker에 전달하는 상품 단위.
    ProductSpec 의 확장판 — 마진(unit_margin_usd)과 수량(quantity)을 추가로 포함.
    """
    asin: str
    title: str
    length_cm: float
    width_cm: float
    height_cm: float
    weight_kg: float
    unit_margin_usd: float   # DynamicMarginCalculator 결과값
    quantity: int = 1        # 적재 가능한(또는 발주 예정) 수량

    @property
    def volume_cm3(self) -> float:
        return self.length_cm * self.width_cm * self.height_cm

    @property
    def margin_density(self) -> float:
        """마진 밀도: USD per cm³ — 그리디 정렬 기준."""
        return self.unit_margin_usd / (self.volume_cm3 or 1.0)


@dataclass
class PackedItem:
    """박스에 실제로 적재된 상품 + 수량."""
    item: ItemSpec
    qty: int

    @property
    def total_margin_usd(self) -> float:
        return round(self.item.unit_margin_usd * self.qty, 6)

    @property
    def total_weight_kg(self) -> float:
        return round(self.item.weight_kg * self.qty, 6)

    @property
    def total_volume_cm3(self) -> float:
        return round(self.item.volume_cm3 * self.qty, 4)

    def to_dict(self) -> dict:
        return {
            "asin": self.item.asin,
            "title": self.item.title,
            "qty": self.qty,
            "unit_margin_usd": self.item.unit_margin_usd,
            "total_margin_usd": self.total_margin_usd,
            "weight_kg": self.item.weight_kg,
            "dims_cm": f"{self.item.length_cm}×{self.item.width_cm}×{self.item.height_cm}",
        }


@dataclass
class MultiPackingResult:
    """
    복수 상품 혼합 패킹 결과 — 한 박스에 담긴 상품 조합과 마진 지표.
    is_profitable: net_margin_usd > 0 여부
    """
    box: BoxSpec
    packed_items: list[PackedItem]

    actual_weight_kg: float
    dimensional_weight_kg: float
    chargeable_weight_kg: float      # max(actual, dim)
    packing_efficiency: float        # 부피 활용률 0.0 ~ 1.0

    shipping_cost_usd: float
    total_margin_usd: float
    net_margin_usd: float            # total_margin − shipping_cost
    roi: float                       # net_margin / shipping_cost

    @property
    def is_profitable(self) -> bool:
        return self.net_margin_usd > 0

    @property
    def total_items(self) -> int:
        return sum(pi.qty for pi in self.packed_items)

    def summary(self) -> str:
        items_str = ", ".join(
            f"{pi.item.asin}×{pi.qty}" for pi in self.packed_items
        )
        return (
            f"[{self.box.id}] items=[{items_str}] "
            f"net=${self.net_margin_usd:.2f} roi={self.roi:.1%} "
            f"chargeable={self.chargeable_weight_kg:.2f}kg "
            f"eff={self.packing_efficiency:.1%}"
        )


class OptiPacker:
    """
    복수 상품 혼합 3D 패킹 엔진.

    pack(items) → 각 박스 사이즈별 최적 구성 리스트
    find_golden_box(items) → ROI 최고 구성 단일 반환
    """

    DEFAULT_PACKING_EFFICIENCY: float = 0.72   # 혼합 상품 현실적 부피 활용률

    def __init__(
        self,
        boxes: Optional[list[BoxSpec]] = None,
        shipping_rate_usd_per_kg: float = 8.0,
        packing_efficiency: float = DEFAULT_PACKING_EFFICIENCY,
    ):
        self.boxes = boxes or KR_POST_BOXES
        self.shipping_rate = shipping_rate_usd_per_kg
        self.packing_efficiency = packing_efficiency

    # ──────────────────────────────────────────────────────────────────────────
    # 공개 API
    # ──────────────────────────────────────────────────────────────────────────

    def pack(self, items: list[ItemSpec]) -> list[MultiPackingResult]:
        """모든 박스 사이즈에 대해 혼합 패킹을 시도하고 유효한 결과만 반환한다."""
        results = []
        for box in self.boxes:
            result = self._pack_into_box(box, items)
            if result:
                results.append(result)
                logger.info("[OPTI-PACKER] %s", result.summary())
        return results

    def find_golden_box(self, items: list[ItemSpec]) -> Optional[MultiPackingResult]:
        """
        황금 박스 조합 — ROI(배송비 대비 순마진 비율)이 가장 높은 구성을 반환한다.
        수익성이 없는(net_margin ≤ 0) 구성은 제외된다.
        """
        candidates = [r for r in self.pack(items) if r.is_profitable]
        if not candidates:
            logger.warning("[OPTI-PACKER] No profitable configuration found.")
            return None
        golden = max(candidates, key=lambda r: r.roi)
        logger.info("[OPTI-PACKER] Golden box selected: %s", golden.summary())
        return golden

    # ──────────────────────────────────────────────────────────────────────────
    # 내부 알고리즘
    # ──────────────────────────────────────────────────────────────────────────

    def _pack_into_box(
        self, box: BoxSpec, items: list[ItemSpec]
    ) -> Optional[MultiPackingResult]:
        """
        단일 박스에 혼합 패킹 시도.
        1. 박스 치수에 맞는 상품만 선별
        2. 마진 밀도(USD/cm³) 내림차순 그리디 배분
        3. 부피·무게 제약 내에서 최대 적재
        """
        packable = [item for item in items if self._fits_in_box(box, item)]
        if not packable:
            return None

        packable.sort(key=lambda i: i.margin_density, reverse=True)

        available_vol = box.volume_cm3 * self.packing_efficiency
        available_wt = box.max_weight_kg
        packed: list[PackedItem] = []

        for item in packable:
            max_by_dim = self._max_by_dimension(box, item)
            max_by_vol = int(available_vol / item.volume_cm3) if item.volume_cm3 > 0 else item.quantity
            max_by_wt = int(available_wt / item.weight_kg) if item.weight_kg > 0 else item.quantity

            qty = min(item.quantity, max_by_dim, max_by_vol, max_by_wt)
            if qty <= 0:
                continue

            packed.append(PackedItem(item=item, qty=qty))
            available_vol -= qty * item.volume_cm3
            available_wt -= qty * item.weight_kg

        if not packed:
            return None

        actual_wt = sum(pi.total_weight_kg for pi in packed)
        dim_wt = box.dim_weight_kg
        chargeable_wt = max(actual_wt, dim_wt)
        shipping_cost = round(chargeable_wt * self.shipping_rate, 4)
        total_margin = round(sum(pi.total_margin_usd for pi in packed), 4)
        net_margin = round(total_margin - shipping_cost, 4)
        roi = round(net_margin / shipping_cost, 6) if shipping_cost > 0 else 0.0
        vol_used = sum(pi.total_volume_cm3 for pi in packed)
        efficiency = round(vol_used / box.volume_cm3, 4)

        return MultiPackingResult(
            box=box,
            packed_items=packed,
            actual_weight_kg=round(actual_wt, 4),
            dimensional_weight_kg=round(dim_wt, 4),
            chargeable_weight_kg=round(chargeable_wt, 4),
            packing_efficiency=efficiency,
            shipping_cost_usd=shipping_cost,
            total_margin_usd=total_margin,
            net_margin_usd=net_margin,
            roi=roi,
        )

    def _fits_in_box(self, box: BoxSpec, item: ItemSpec) -> bool:
        """아이템이 박스에 6방향 회전 중 하나로 들어가면 True."""
        return self._max_by_dimension(box, item) > 0

    def _max_by_dimension(self, box: BoxSpec, item: ItemSpec) -> int:
        """
        6방향 회전 중 박스에 들어가는 최대 격자 수량을 반환한다.

        격자 수량 = floor(bL/iL) × floor(bW/iW) × floor(bH/iH)
        단, 아이템 치수 모두 박스 치수 이하인 회전만 유효.
        """
        bL, bW, bH = box.length_cm, box.width_cm, box.height_cm
        iL, iW, iH = item.length_cm, item.width_cm, item.height_cm

        rotations = [
            (iL, iW, iH), (iL, iH, iW),
            (iW, iL, iH), (iW, iH, iL),
            (iH, iL, iW), (iH, iW, iL),
        ]
        best = 0
        for rL, rW, rH in rotations:
            if rL <= bL and rW <= bW and rH <= bH:
                qty = int(bL / rL) * int(bW / rW) * int(bH / rH)
                best = max(best, qty)
        return best


# ══════════════════════════════════════════════════════════════════════════════
# [신규] BoxArchiver — DB 아카이빙
# ══════════════════════════════════════════════════════════════════════════════


class BoxArchiver:
    """
    OptiPacker 최적 구성 결과를 BoxRecommendation 테이블에 저장하고 조회한다.

    '추천 게시판' 용도: 과거 황금 박스 구성을 언제든지 꺼내 재활용할 수 있다.
    """

    def archive(
        self,
        db: Session,
        result: MultiPackingResult,
        *,
        week_key: Optional[str] = None,
        label: Optional[str] = None,
    ) -> BoxRecommendation:
        """MultiPackingResult를 BoxRecommendation 레코드로 저장하고 반환한다."""
        wk = week_key or get_current_week_key()
        rec = BoxRecommendation(
            week_key=wk,
            box_size_id=result.box.id,
            box_name=result.box.name or result.box.id,
            label=label,
            total_units=result.total_items,
            actual_weight_kg=result.actual_weight_kg,
            dimensional_weight_kg=result.dimensional_weight_kg,
            chargeable_weight_kg=result.chargeable_weight_kg,
            packing_efficiency=result.packing_efficiency,
            estimated_shipping_cost=result.shipping_cost_usd,
            total_margin_usd=result.total_margin_usd,
            net_margin_usd=result.net_margin_usd,
            roi=result.roi,
            packing_detail=[pi.to_dict() for pi in result.packed_items],
        )
        db.add(rec)
        db.flush()
        logger.info(
            "[ARCHIVER] Saved BoxRecommendation id=%d week=%s label=%s roi=%.1f%%",
            rec.id, wk, label, result.roi * 100,
        )
        return rec

    def get_recommendations(
        self,
        db: Session,
        *,
        week_key: Optional[str] = None,
        limit: int = 20,
    ) -> list[BoxRecommendation]:
        """주차 필터 + ROI 내림차순으로 추천 박스 조합 목록을 반환한다."""
        q = db.query(BoxRecommendation)
        if week_key:
            q = q.filter(BoxRecommendation.week_key == week_key)
        return (
            q.order_by(BoxRecommendation.roi.desc())
             .limit(limit)
             .all()
        )

    def get_top_by_roi(
        self,
        db: Session,
        *,
        week_key: Optional[str] = None,
        top_n: int = 5,
    ) -> list[BoxRecommendation]:
        """ROI 상위 N개의 추천 박스 구성을 반환한다."""
        return self.get_recommendations(db, week_key=week_key, limit=top_n)
