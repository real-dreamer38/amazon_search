"""
Arbitrage-X — Box Optimizer 단위 테스트
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arbitrage_x.modules.box_optimizer import BoxOptimizer, ProductSpec


def make_product(**kwargs):
    defaults = dict(
        asin="B0TEST0001",
        weight_kg=0.5,
        length_cm=10,
        width_cm=8,
        height_cm=5,
    )
    defaults.update(kwargs)
    return ProductSpec(**defaults)


def test_basic_optimization():
    optimizer = BoxOptimizer()
    product = make_product(weight_kg=0.5, length_cm=10, width_cm=8, height_cm=5)
    results = optimizer.optimize(product, total_units=50)

    assert len(results) > 0
    # 단위당 비용 오름차순 정렬 확인
    costs = [r.cost_per_unit for r in results]
    assert costs == sorted(costs)


def test_product_too_large():
    """박스보다 큰 상품은 결과가 없어야 한다."""
    optimizer = BoxOptimizer()
    product = make_product(
        weight_kg=1.0,
        length_cm=200,  # 모든 박스보다 큼
        width_cm=200,
        height_cm=200,
    )
    results = optimizer.optimize(product, total_units=10)
    assert len(results) == 0


def test_weight_limit():
    """무거운 상품은 박스당 수량이 제한되어야 한다."""
    optimizer = BoxOptimizer()
    # M 박스 max_weight=15kg, 단품 7kg → 최대 2개/박스
    product = make_product(
        weight_kg=7.0,
        length_cm=10,
        width_cm=10,
        height_cm=10,
    )
    results = optimizer.optimize(product, total_units=10)
    for r in results:
        assert r.units_per_box * product.weight_kg <= r.box.max_weight_kg + 0.01


def test_best_returns_cheapest():
    optimizer = BoxOptimizer()
    product = make_product()
    results = optimizer.optimize(product, total_units=100)
    best = optimizer.best(results)
    assert best is not None
    assert best.cost_per_unit == min(r.cost_per_unit for r in results)


def test_total_boxes_calculation():
    optimizer = BoxOptimizer()
    product = make_product(weight_kg=0.1, length_cm=5, width_cm=5, height_cm=5)
    results = optimizer.optimize(product, total_units=100)
    for r in results:
        import math
        assert r.total_boxes == math.ceil(100 / r.units_per_box)
