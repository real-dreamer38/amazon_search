"""
Arbitrage-X — Ingestion API Router

POST /ingestion/search    — 키워드 검색 + DB 저장
GET  /ingestion/products  — 수집된 상품 목록 조회
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from arbitrage_x.db.database import get_db_session
from arbitrage_x.ingestion.ingestion_service import IngestionService
from arbitrage_x.ingestion.schemas import IngestionResult

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ingestion", tags=["ingestion"])


class SearchRequest(BaseModel):
    keyword_en: str = Field(..., min_length=1, max_length=200, examples=["laptop stand"])
    keyword_ko: Optional[str] = Field(None, max_length=200, examples=["노트북 거치대"])
    max_amazon: int = Field(20, ge=1, le=20)
    max_naver: int = Field(20, ge=1, le=100)


@router.post("/search", response_model=IngestionResult, summary="아마존+네이버 상품 수집")
def search_and_ingest(
    req: SearchRequest,
    db: Session = Depends(get_db_session),
) -> IngestionResult:
    """
    Amazon US와 Naver Shopping을 동시에 검색하고
    Amazon 결과를 DB에 저장한 뒤 두 마켓 결과를 반환한다.
    """
    with IngestionService() as svc:
        return svc.run(
            keyword_en=req.keyword_en,
            keyword_ko=req.keyword_ko,
            max_amazon=req.max_amazon,
            max_naver=req.max_naver,
            session=db,
        )


@router.get("/products", summary="수집된 상품 목록")
def list_products(
    db: Session = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    from sqlalchemy import desc
    from arbitrage_x.db.models import PriceSnapshot, Product

    products = (
        db.query(Product)
        .order_by(desc(Product.updated_at))
        .offset(offset)
        .limit(limit)
        .all()
    )

    items = []
    for p in products:
        latest: Optional[PriceSnapshot] = (
            db.query(PriceSnapshot)
            .filter_by(product_id=p.id)
            .order_by(desc(PriceSnapshot.recorded_at))
            .first()
        )
        items.append(
            {
                "asin": p.asin,
                "title": p.title,
                "brand": p.brand,
                "category": p.category,
                "image_url": p.image_url,
                "buy_box_price": latest.buy_box_price if latest else None,
                "lowest_new_price": latest.lowest_new_price if latest else None,
                "sellers_count": latest.sellers_count if latest else None,
                "bsr_rank": None,
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            }
        )

    return {"total": len(items), "offset": offset, "limit": limit, "items": items}
