"""
Arbitrage-X — Opti-Packer (Box Optimizer)
상품 규격 기반 최적 박스 조합을 계산한다.

알고리즘:
  1. 사용 가능한 박스 규격별로 단위당 운임을 계산
  2. 3D Bin Packing 근사 (부피 & 중량 제약)
  3. 단위당 운임이 최저인 박스+수량 조합을 추천
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from config.settings import AVAILABLE_BOX_SIZES


@dataclass
class BoxSpec:
    id: str
    length: float   # cm
    width: float    # cm
    height: float   # cm
    max_weight_kg: float

    @property
    def volume_cm3(self) -> float:
        return self.length * self.width * self.height

    @property
    def dim_weight_kg(self) -> float:
        """DIM 무게 (UPS 기준: 부피/5000)."""
        return self.volume_cm3 / 5000.0


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
    utilization_pct: float       # 부피 활용률
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
    사용 예:
        optimizer = BoxOptimizer()
        results = optimizer.optimize(product_spec, total_units=100, base_rate_per_kg=0.8)
        best = optimizer.best(results)
    """

    def __init__(
        self,
        box_sizes: Optional[list[dict]] = None,
        packing_efficiency: float = 0.80,
    ):
        raw = box_sizes or AVAILABLE_BOX_SIZES
        self.boxes = [BoxSpec(**b) for b in raw]
        self.packing_efficiency = packing_efficiency
        # 실제 패킹 시 박스 부피의 80%만 활용 가능하다고 가정

    def optimize(
        self,
        product: ProductSpec,
        total_units: int,
        base_rate_per_kg: float = 0.80,
        flat_handling_fee: float = 2.50,
    ) -> list[PackingResult]:
        """
        모든 박스 사이즈별 최적 패킹 결과를 반환한다.
        base_rate_per_kg: USD/kg (실제 운임은 UPS API로 교체 가능)
        """
        results = []
        for box in self.boxes:
            result = self._pack(
                product, box, total_units, base_rate_per_kg, flat_handling_fee
            )
            if result:
                results.append(result)

        # 단위당 비용 오름차순 정렬
        results.sort(key=lambda r: r.cost_per_unit)
        return results

    def best(self, results: list[PackingResult]) -> Optional[PackingResult]:
        return results[0] if results else None

    # ──────────────────────────────────────────────────────────────────────────
    # 내부 계산
    # ──────────────────────────────────────────────────────────────────────────

    def _pack(
        self,
        product: ProductSpec,
        box: BoxSpec,
        total_units: int,
        rate: float,
        handling: float,
    ) -> Optional[PackingResult]:
        """단일 박스 사이즈에 대한 패킹 계산."""

        # ── 박스 안에 들어갈 수 있는 최대 단위수 ──────────────────────────
        # 부피 제약
        usable_volume = box.volume_cm3 * self.packing_efficiency
        by_volume = int(usable_volume / product.volume_cm3) if product.volume_cm3 > 0 else 9999

        # 무게 제약
        by_weight = int(box.max_weight_kg / product.weight_kg) if product.weight_kg > 0 else 9999

        units_per_box = min(by_volume, by_weight)
        if units_per_box <= 0:
            return None  # 상품이 박스보다 큼

        # ── 상품이 박스 치수에 맞는지 확인 ────────────────────────────────
        if (product.length_cm > box.length
                or product.width_cm > box.width
                or product.height_cm > box.height):
            return None

        total_boxes = math.ceil(total_units / units_per_box)
        actual_units = units_per_box * total_boxes

        # ── 운임 계산 (DIM weight vs actual weight 중 큰 값) ───────────────
        per_box_actual_weight = units_per_box * product.weight_kg
        per_box_dim_weight = box.dim_weight_kg
        billable_weight = max(per_box_actual_weight, per_box_dim_weight)
        shipping_per_box = billable_weight * rate + handling
        total_shipping = shipping_per_box * total_boxes

        cost_per_unit = total_shipping / total_units

        # ── 공간 활용률 ───────────────────────────────────────────────────
        used_volume = units_per_box * product.volume_cm3
        utilization = (used_volume / box.volume_cm3) * 100
        dead_space = 100 - utilization

        packing_detail = {
            "units_per_box": units_per_box,
            "by_volume_limit": by_volume,
            "by_weight_limit": by_weight,
            "per_box_actual_weight_kg": round(per_box_actual_weight, 3),
            "per_box_dim_weight_kg": round(per_box_dim_weight, 3),
            "billable_weight_kg": round(billable_weight, 3),
            "shipping_per_box_usd": round(shipping_per_box, 2),
        }

        return PackingResult(
            box=box,
            units_per_box=units_per_box,
            total_units=total_units,
            total_boxes=total_boxes,
            estimated_shipping_cost=total_shipping,
            cost_per_unit=cost_per_unit,
            utilization_pct=utilization,
            dead_space_pct=dead_space,
            packing_detail=packing_detail,
        )
