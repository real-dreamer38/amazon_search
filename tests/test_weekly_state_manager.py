"""
Arbitrage-X — Weekly State Manager 단위 테스트
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from arbitrage_x.db.models import Base
from arbitrage_x.core.weekly_state_manager import WeeklyStateManager
from arbitrage_x.utils.week_utils import get_current_week_key, get_week_bounds


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_create_weekly_state(db_session):
    mgr = WeeklyStateManager(db_session)
    state = mgr.get_or_create_current_week(
        domestic_shipping_cost=5.0,
        international_shipping_cost=15.0,
        prep_service_fee=1.5,
        customs_duty_rate=0.05,
        misc_cost_per_unit=0.3,
        exchange_rate_usd_krw=1380.0,
    )
    assert state.week_key == get_current_week_key()
    assert state.domestic_shipping_cost == 5.0
    assert state.is_locked is False


def test_idempotent_creation(db_session):
    """같은 주차를 두 번 생성해도 레코드는 하나여야 한다."""
    mgr = WeeklyStateManager(db_session)
    s1 = mgr.get_or_create_current_week(domestic_shipping_cost=5.0)
    s2 = mgr.get_or_create_current_week(domestic_shipping_cost=99.0)
    assert s1.id == s2.id
    assert s2.domestic_shipping_cost == 5.0  # 기존값 유지


def test_update_unlocked_state(db_session):
    mgr = WeeklyStateManager(db_session)
    mgr.get_or_create_current_week(domestic_shipping_cost=5.0)
    updated = mgr.update_current_week(domestic_shipping_cost=8.0, notes="Updated")
    assert updated.domestic_shipping_cost == 8.0
    assert updated.notes == "Updated"


def test_compute_margin(db_session):
    from arbitrage_x.db.models import Product
    mgr = WeeklyStateManager(db_session)
    mgr.get_or_create_current_week(
        domestic_shipping_cost=3.0,
        prep_service_fee=1.0,
        customs_duty_rate=0.0,
        misc_cost_per_unit=0.5,
    )
    product = Product(asin="B0TEST0001", title="Test Product")
    db_session.add(product)
    db_session.flush()

    record = mgr.compute_and_record_margin(
        product=product,
        source_price=10.0,
        amazon_price=25.0,
        fba_fee=3.50,
    )
    assert record.gross_profit > 0
    assert 0 < record.margin_rate < 1
    assert record.roi > 0
    assert record.cost_snapshot["week_key"] == get_current_week_key()


def test_locked_state_cannot_be_updated(db_session):
    """lock된 WeeklyState는 수정할 수 없어야 한다."""
    from arbitrage_x.db.models import WeeklyState
    mgr = WeeklyStateManager(db_session)
    state = mgr.get_or_create_current_week(domestic_shipping_cost=5.0)

    # 수동으로 lock
    db_session.execute(
        WeeklyState.__table__.update()
        .where(WeeklyState.__table__.c.week_key == state.week_key)
        .values(is_locked=True)
    )
    db_session.expire(state)

    with pytest.raises(PermissionError):
        mgr.update_current_week(week_key=state.week_key, domestic_shipping_cost=99.0)


def test_week_bounds():
    monday, sunday = get_week_bounds("2026-W18")
    assert monday.weekday() == 0  # 월요일
    assert sunday.weekday() == 6  # 일요일
    assert (sunday - monday).days == 6
