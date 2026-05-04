"""
Arbitrage-X — Weekly State API Router
POST /api/v1/weekly-state/create   → 주간 비용 생성
PUT  /api/v1/weekly-state/update   → 현재 주차 비용 수정 (lock 전)
GET  /api/v1/weekly-state/current  → 현재 주차 조회
GET  /api/v1/weekly-state/list     → 전체 주차 목록
GET  /api/v1/weekly-state/pnl      → 손익 요약
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from arbitrage_x.core.weekly_state_manager import WeeklyStateManager
from arbitrage_x.db.database import get_db_session
from arbitrage_x.utils.week_utils import get_current_week_key

router = APIRouter(prefix="/api/v1/weekly-state", tags=["Weekly State"])


# ── Pydantic Schemas ──────────────────────────────────────────────────────────

class WeeklyStateCreateRequest(BaseModel):
    domestic_shipping_cost: float = Field(0.0, ge=0, description="국내 배송비 (박스당, USD)")
    international_shipping_cost: float = Field(0.0, ge=0, description="국제 운임 (박스당, USD)")
    prep_service_fee: float = Field(0.0, ge=0, description="FBA Prep 수수료 (단위당, USD)")
    customs_duty_rate: float = Field(0.0, ge=0, le=1, description="관세율 (0.0 ~ 1.0)")
    misc_cost_per_unit: float = Field(0.0, ge=0, description="기타 잡비 (단위당, USD)")
    amazon_referral_fee_rate: Optional[float] = Field(None, ge=0, le=1)
    fba_fee_override: Optional[float] = Field(None, ge=0)
    exchange_rate_usd_krw: float = Field(1300.0, gt=0)
    notes: Optional[str] = None
    created_by: str = "api"


class WeeklyStateUpdateRequest(BaseModel):
    domestic_shipping_cost: Optional[float] = Field(None, ge=0)
    international_shipping_cost: Optional[float] = Field(None, ge=0)
    prep_service_fee: Optional[float] = Field(None, ge=0)
    customs_duty_rate: Optional[float] = Field(None, ge=0, le=1)
    misc_cost_per_unit: Optional[float] = Field(None, ge=0)
    amazon_referral_fee_rate: Optional[float] = Field(None, ge=0, le=1)
    fba_fee_override: Optional[float] = Field(None, ge=0)
    exchange_rate_usd_krw: Optional[float] = Field(None, gt=0)
    notes: Optional[str] = None


class WeeklyStateResponse(BaseModel):
    week_key: str
    week_start_date: str
    week_end_date: str
    domestic_shipping_cost: float
    international_shipping_cost: float
    prep_service_fee: float
    customs_duty_rate: float
    misc_cost_per_unit: float
    amazon_referral_fee_rate: Optional[float]
    fba_fee_override: Optional[float]
    exchange_rate_usd_krw: float
    notes: Optional[str]
    is_locked: bool
    created_at: str
    created_by: str

    class Config:
        from_attributes = True


def _state_to_response(state) -> WeeklyStateResponse:
    return WeeklyStateResponse(
        week_key=state.week_key,
        week_start_date=str(state.week_start_date),
        week_end_date=str(state.week_end_date),
        domestic_shipping_cost=state.domestic_shipping_cost,
        international_shipping_cost=state.international_shipping_cost,
        prep_service_fee=state.prep_service_fee,
        customs_duty_rate=state.customs_duty_rate,
        misc_cost_per_unit=state.misc_cost_per_unit,
        amazon_referral_fee_rate=state.amazon_referral_fee_rate,
        fba_fee_override=state.fba_fee_override,
        exchange_rate_usd_krw=state.exchange_rate_usd_krw,
        notes=state.notes,
        is_locked=state.is_locked,
        created_at=state.created_at.isoformat() if state.created_at else "",
        created_by=state.created_by or "",
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/create", response_model=WeeklyStateResponse)
def create_weekly_state(
    body: WeeklyStateCreateRequest,
    db: Session = Depends(get_db_session),
):
    """현재 주차의 부대비용을 생성한다. 이미 존재하면 기존 값을 반환한다."""
    mgr = WeeklyStateManager(db)
    state = mgr.get_or_create_current_week(**body.model_dump())
    return _state_to_response(state)


@router.put("/update", response_model=WeeklyStateResponse)
def update_weekly_state(
    body: WeeklyStateUpdateRequest,
    week_key: Optional[str] = Query(None),
    db: Session = Depends(get_db_session),
):
    """현재 주차(또는 지정 주차)의 부대비용을 수정한다. lock된 주차는 수정 불가."""
    mgr = WeeklyStateManager(db)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        state = mgr.update_current_week(week_key=week_key, **updates)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _state_to_response(state)


@router.get("/current", response_model=WeeklyStateResponse)
def get_current_state(db: Session = Depends(get_db_session)):
    """현재 주차 WeeklyState 조회."""
    mgr = WeeklyStateManager(db)
    wk = get_current_week_key()
    state = mgr.get_state_for_week(wk)
    if not state:
        raise HTTPException(
            status_code=404,
            detail=f"WeeklyState for '{wk}' not found. Please create it first.",
        )
    return _state_to_response(state)


@router.get("/list", response_model=list[WeeklyStateResponse])
def list_states(
    limit: int = Query(52, ge=1, le=200),
    db: Session = Depends(get_db_session),
):
    """최근 N주의 WeeklyState 목록 (최신순)."""
    mgr = WeeklyStateManager(db)
    states = mgr.get_all_states(limit=limit)
    return [_state_to_response(s) for s in states]


@router.get("/pnl")
def get_pnl_summary(
    from_week: str = Query(..., description="시작 주차 (예: 2026-W01)"),
    to_week: str = Query(..., description="종료 주차 (예: 2026-W20)"),
    db: Session = Depends(get_db_session),
):
    """주차별 손익 요약을 반환한다."""
    mgr = WeeklyStateManager(db)
    try:
        result = mgr.get_pnl_summary(from_week, to_week)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"data": result, "count": len(result)}
