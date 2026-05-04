"""
Arbitrage-X — Box Optimizer API Router
POST /api/v1/box-optimizer/optimize  → 최적 박스 조합 계산
POST /api/v1/box-optimizer/approve   → 추천 조합 승인 → 구매/발송 프로세스 연결
GET  /api/v1/box-optimizer/recommendations → 승인된 추천 목록
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from arbitrage_x.db.database import get_db_session
from arbitrage_x.db.models import BoxRecommendation, Product
from arbitrage_x.modules.box_optimizer import BoxOptimizer, ProductSpec
from arbitrage_x.utils.week_utils import get_current_week_key

router = APIRouter(prefix="/api/v1/box-optimizer", tags=["Box Optimizer"])


class OptimizeRequest(BaseModel):
    asin: str
    weight_kg: float = Field(..., gt=0)
    length_cm: float = Field(..., gt=0)
    width_cm: float = Field(..., gt=0)
    height_cm: float = Field(..., gt=0)
    total_units: int = Field(..., gt=0)
    base_rate_per_kg: float = Field(0.80, gt=0, description="USD/kg")
    flat_handling_fee: float = Field(2.50, ge=0)


@router.post("/optimize")
def optimize_boxes(
    body: OptimizeRequest,
    db: Session = Depends(get_db_session),
):
    """상품 규격 + 수량으로 최적 박스 조합을 계산하고 추천 게시판에 저장한다."""
    product_spec = ProductSpec(
        asin=body.asin,
        weight_kg=body.weight_kg,
        length_cm=body.length_cm,
        width_cm=body.width_cm,
        height_cm=body.height_cm,
    )
    optimizer = BoxOptimizer()
    results = optimizer.optimize(
        product_spec,
        total_units=body.total_units,
        base_rate_per_kg=body.base_rate_per_kg,
        flat_handling_fee=body.flat_handling_fee,
    )

    if not results:
        raise HTTPException(status_code=400, detail="No valid box combination found.")

    best = results[0]
    week_key = get_current_week_key()

    product = db.query(Product).filter_by(asin=body.asin).first()

    saved_ids = []
    for r in results:
        rec = BoxRecommendation(
            product_id=product.id if product else None,
            week_key=week_key,
            box_size_id=r.box.id,
            units_per_box=r.units_per_box,
            total_units=r.total_units,
            total_boxes=r.total_boxes,
            estimated_shipping_cost=r.estimated_shipping_cost,
            cost_per_unit=r.cost_per_unit,
            packing_detail=r.packing_detail,
        )
        db.add(rec)
        db.flush()
        saved_ids.append(rec.id)

    return {
        "best": best.to_dict(),
        "all_options": [r.to_dict() for r in results],
        "saved_recommendation_ids": saved_ids,
    }


@router.post("/approve/{recommendation_id}")
def approve_recommendation(
    recommendation_id: int,
    approved_by: str = "user",
    db: Session = Depends(get_db_session),
):
    """추천 조합을 승인하여 구매/발송 프로세스로 연결한다."""
    rec = db.query(BoxRecommendation).filter_by(id=recommendation_id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found.")

    rec.is_approved = True
    rec.approved_at = datetime.utcnow()
    rec.approved_by = approved_by
    db.flush()

    return {
        "status": "approved",
        "recommendation_id": recommendation_id,
        "next_step": "Create shipment via POST /api/v1/logistics/create-shipment",
        "detail": {
            "box_size": rec.box_size_id,
            "units_per_box": rec.units_per_box,
            "total_boxes": rec.total_boxes,
            "estimated_cost_usd": rec.estimated_shipping_cost,
        },
    }


@router.get("/recommendations")
def list_recommendations(
    week_key: Optional[str] = None,
    approved_only: bool = False,
    db: Session = Depends(get_db_session),
):
    """추천 박스 조합 목록 조회."""
    q = db.query(BoxRecommendation)
    if week_key:
        q = q.filter_by(week_key=week_key)
    if approved_only:
        q = q.filter_by(is_approved=True)
    recs = q.order_by(BoxRecommendation.created_at.desc()).all()

    return {
        "count": len(recs),
        "data": [
            {
                "id": r.id,
                "week_key": r.week_key,
                "box_size_id": r.box_size_id,
                "units_per_box": r.units_per_box,
                "total_boxes": r.total_boxes,
                "cost_per_unit": r.cost_per_unit,
                "is_approved": r.is_approved,
                "approved_at": r.approved_at.isoformat() if r.approved_at else None,
            }
            for r in recs
        ],
    }
