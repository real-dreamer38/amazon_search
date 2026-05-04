"""
Arbitrage-X — Weekly State Manager
매주 월요일 부대비용을 입력받고, 주차별 스냅샷을 불변 보관한다.

핵심 원칙:
  - 한 주차(week_key)에 WeeklyState는 단 하나만 존재한다.
  - 생성 후 다음 주가 시작되면 is_locked=True로 고정된다.
  - MarginRecord는 항상 해당 week_key의 cost_snapshot을 비정규화하여 저장한다.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from arbitrage_x.db.models import MarginRecord, Product, WeeklyState
from arbitrage_x.utils.week_utils import (
    get_current_week_key,
    get_week_bounds,
    parse_week_key,
)

logger = logging.getLogger(__name__)


class WeeklyStateManager:
    def __init__(self, db: Session):
        self.db = db

    # ──────────────────────────────────────────────────────────────────────────
    # 공개 API
    # ──────────────────────────────────────────────────────────────────────────

    def get_or_create_current_week(
        self,
        *,
        domestic_shipping_cost: float = 0.0,
        international_shipping_cost: float = 0.0,
        prep_service_fee: float = 0.0,
        customs_duty_rate: float = 0.0,
        misc_cost_per_unit: float = 0.0,
        amazon_referral_fee_rate: Optional[float] = None,
        fba_fee_override: Optional[float] = None,
        exchange_rate_usd_krw: float = 1300.0,
        notes: Optional[str] = None,
        created_by: str = "system",
    ) -> WeeklyState:
        """
        현재 주차의 WeeklyState를 반환한다.
        아직 없으면 파라미터로 새로 생성한다.
        이미 존재하면 파라미터를 무시하고 기존 레코드를 반환한다.
        """
        week_key = get_current_week_key()
        existing = self._get_by_week_key(week_key)
        if existing:
            logger.info("WeeklyState already exists for %s", week_key)
            return existing

        # 이전 주차 lock
        self._lock_previous_weeks(week_key)

        week_start, week_end = get_week_bounds(week_key)
        state = WeeklyState(
            week_key=week_key,
            week_start_date=week_start,
            week_end_date=week_end,
            domestic_shipping_cost=domestic_shipping_cost,
            international_shipping_cost=international_shipping_cost,
            prep_service_fee=prep_service_fee,
            customs_duty_rate=customs_duty_rate,
            misc_cost_per_unit=misc_cost_per_unit,
            amazon_referral_fee_rate=amazon_referral_fee_rate,
            fba_fee_override=fba_fee_override,
            exchange_rate_usd_krw=exchange_rate_usd_krw,
            notes=notes,
            created_by=created_by,
            is_locked=False,
        )
        self.db.add(state)
        self.db.flush()
        logger.info("Created WeeklyState for %s", week_key)
        self._export_json_snapshot(state)
        return state

    def update_current_week(
        self,
        week_key: Optional[str] = None,
        **kwargs,
    ) -> WeeklyState:
        """
        현재 주차(또는 지정 week_key)의 WeeklyState를 수정한다.
        locked 상태이면 PermissionError를 발생시킨다.
        """
        wk = week_key or get_current_week_key()
        state = self._get_by_week_key(wk)
        if not state:
            raise ValueError(f"WeeklyState for '{wk}' does not exist.")
        if state.is_locked:
            raise PermissionError(
                f"WeeklyState '{wk}' is locked. Past data cannot be modified."
            )

        allowed_fields = {
            "domestic_shipping_cost", "international_shipping_cost",
            "prep_service_fee", "customs_duty_rate", "misc_cost_per_unit",
            "amazon_referral_fee_rate", "fba_fee_override",
            "exchange_rate_usd_krw", "notes",
        }
        for field, value in kwargs.items():
            if field in allowed_fields:
                setattr(state, field, value)
            else:
                raise ValueError(f"Field '{field}' is not updatable.")

        self.db.flush()
        self._export_json_snapshot(state)
        logger.info("Updated WeeklyState for %s: %s", wk, list(kwargs.keys()))
        return state

    def get_state_for_week(self, week_key: str) -> Optional[WeeklyState]:
        """특정 주차의 WeeklyState 조회."""
        return self._get_by_week_key(week_key)

    def get_all_states(self, limit: int = 52) -> list[WeeklyState]:
        """최근 N주 WeeklyState 목록 (최신순)."""
        return (
            self.db.query(WeeklyState)
            .order_by(WeeklyState.week_key.desc())
            .limit(limit)
            .all()
        )

    def compute_and_record_margin(
        self,
        product: Product,
        source_price: float,
        amazon_price: float,
        fba_fee: Optional[float] = None,
        week_key: Optional[str] = None,
    ) -> MarginRecord:
        """
        주어진 주차의 WeeklyState를 기반으로 마진을 계산하고 MarginRecord를 저장한다.
        이미 같은 week_key + product_id 레코드가 있으면 덮어쓴다 (단, 잠긴 주차 제외).
        """
        wk = week_key or get_current_week_key()
        state = self._get_by_week_key(wk)
        if not state:
            raise ValueError(f"WeeklyState for '{wk}' not found. Create it first.")

        referral_rate = state.amazon_referral_fee_rate or 0.15
        referral_fee = amazon_price * referral_rate
        effective_fba = fba_fee or state.fba_fee_override or 3.50

        total_cost = (
            source_price
            + state.domestic_shipping_cost
            + effective_fba
            + referral_fee
            + state.prep_service_fee
            + (source_price * state.customs_duty_rate)
            + state.misc_cost_per_unit
        )
        gross_profit = amazon_price - total_cost
        margin_rate = gross_profit / amazon_price if amazon_price else 0.0
        roi = gross_profit / total_cost if total_cost else 0.0

        cost_snapshot = {
            "week_key": wk,
            "domestic_shipping_cost": state.domestic_shipping_cost,
            "international_shipping_cost": state.international_shipping_cost,
            "prep_service_fee": state.prep_service_fee,
            "customs_duty_rate": state.customs_duty_rate,
            "misc_cost_per_unit": state.misc_cost_per_unit,
            "referral_fee_rate": referral_rate,
            "fba_fee_used": effective_fba,
            "exchange_rate_usd_krw": state.exchange_rate_usd_krw,
        }

        # Upsert
        existing = (
            self.db.query(MarginRecord)
            .filter_by(week_key=wk, product_id=product.id)
            .first()
        )
        if existing:
            if state.is_locked:
                raise PermissionError(
                    f"Cannot overwrite MarginRecord for locked week '{wk}'."
                )
            existing.source_price = source_price
            existing.amazon_price = amazon_price
            existing.fba_fee = effective_fba
            existing.referral_fee = referral_fee
            existing.total_cost = total_cost
            existing.gross_profit = gross_profit
            existing.margin_rate = margin_rate
            existing.roi = roi
            existing.cost_snapshot = cost_snapshot
            existing.calculated_at = datetime.utcnow()
            record = existing
        else:
            record = MarginRecord(
                week_key=wk,
                product_id=product.id,
                source_price=source_price,
                amazon_price=amazon_price,
                fba_fee=effective_fba,
                referral_fee=referral_fee,
                total_cost=total_cost,
                gross_profit=gross_profit,
                margin_rate=margin_rate,
                roi=roi,
                cost_snapshot=cost_snapshot,
            )
            self.db.add(record)

        self.db.flush()
        return record

    def get_pnl_summary(self, from_week: str, to_week: str) -> list[dict]:
        """
        지정 기간의 주차별 손익 요약.
        각 주차의 마진 레코드를 집계하여 반환한다.
        """
        records = (
            self.db.query(MarginRecord)
            .filter(
                MarginRecord.week_key >= from_week,
                MarginRecord.week_key <= to_week,
            )
            .all()
        )

        summary: dict[str, dict] = {}
        for r in records:
            wk = r.week_key
            if wk not in summary:
                summary[wk] = {
                    "week_key": wk,
                    "total_gross_profit": 0.0,
                    "total_revenue": 0.0,
                    "total_cost": 0.0,
                    "avg_margin_rate": 0.0,
                    "product_count": 0,
                }
            s = summary[wk]
            s["total_gross_profit"] += r.gross_profit
            s["total_revenue"] += r.amazon_price
            s["total_cost"] += r.total_cost
            s["product_count"] += 1

        for s in summary.values():
            s["avg_margin_rate"] = (
                s["total_gross_profit"] / s["total_revenue"]
                if s["total_revenue"] else 0.0
            )

        return sorted(summary.values(), key=lambda x: x["week_key"])

    # ──────────────────────────────────────────────────────────────────────────
    # 내부 헬퍼
    # ──────────────────────────────────────────────────────────────────────────

    def _get_by_week_key(self, week_key: str) -> Optional[WeeklyState]:
        return (
            self.db.query(WeeklyState)
            .filter(WeeklyState.week_key == week_key)
            .first()
        )

    def _lock_previous_weeks(self, current_week_key: str) -> None:
        """현재 주차보다 이전 주차를 모두 lock."""
        unlocked = (
            self.db.query(WeeklyState)
            .filter(
                WeeklyState.week_key < current_week_key,
                WeeklyState.is_locked == False,  # noqa: E712
            )
            .all()
        )
        for state in unlocked:
            # ORM event listener를 우회하지 않기 위해 직접 SQL 업데이트
            self.db.execute(
                WeeklyState.__table__.update()
                .where(WeeklyState.__table__.c.week_key == state.week_key)
                .values(is_locked=True)
            )
            logger.info("Locked WeeklyState for %s", state.week_key)

    def _export_json_snapshot(self, state: WeeklyState) -> None:
        """data/weekly_snapshots/ 에 JSON 파일로도 백업."""
        from config.settings import SNAPSHOTS_DIR

        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "week_key": state.week_key,
            "week_start_date": str(state.week_start_date),
            "week_end_date": str(state.week_end_date),
            "domestic_shipping_cost": state.domestic_shipping_cost,
            "international_shipping_cost": state.international_shipping_cost,
            "prep_service_fee": state.prep_service_fee,
            "customs_duty_rate": state.customs_duty_rate,
            "misc_cost_per_unit": state.misc_cost_per_unit,
            "amazon_referral_fee_rate": state.amazon_referral_fee_rate,
            "fba_fee_override": state.fba_fee_override,
            "exchange_rate_usd_krw": state.exchange_rate_usd_krw,
            "notes": state.notes,
            "created_at": state.created_at.isoformat() if state.created_at else None,
            "is_locked": state.is_locked,
        }
        path = SNAPSHOTS_DIR / f"{state.week_key}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        logger.debug("Snapshot exported to %s", path)
