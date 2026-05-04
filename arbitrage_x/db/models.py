"""
Arbitrage-X — SQLAlchemy ORM Models
모든 테이블 정의. 주간 스냅샷 불변성(append-only) 보장 설계.
"""
from __future__ import annotations

import enum
from datetime import datetime, date

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Enum, Float, ForeignKey,
    Integer, JSON, String, Text, UniqueConstraint, event,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


# ══════════════════════════════════════════════════════════════════════════════
# 1. WEEKLY STATE — 주간 변수 (불변 스냅샷)
# ══════════════════════════════════════════════════════════════════════════════

class WeeklyState(Base):
    """
    매주 월요일 사용자가 입력하는 부대비용 스냅샷.
    한 번 생성된 레코드는 수정/삭제 불가 (append-only).
    과거 주차의 마진 계산은 항상 해당 주차의 snapshot을 참조한다.
    """
    __tablename__ = "weekly_states"

    id = Column(Integer, primary_key=True, autoincrement=True)
    week_key = Column(String(10), unique=True, nullable=False, index=True)
    # week_key 형식: "2026-W18"  (ISO 주차)
    week_start_date = Column(Date, nullable=False)   # 해당 주 월요일
    week_end_date = Column(Date, nullable=False)     # 해당 주 일요일

    # ── 배송 비용 (USD) ──────────────────────────────────────────────────────
    domestic_shipping_cost = Column(Float, nullable=False, default=0.0)
    # 미국 내 발송 기본 운임 (박스당)
    international_shipping_cost = Column(Float, nullable=False, default=0.0)
    # 국제 배송 운임 (박스당)
    prep_service_fee = Column(Float, nullable=False, default=0.0)
    # FBA Prep 서비스 수수료 (단위당)
    customs_duty_rate = Column(Float, nullable=False, default=0.0)
    # 관세율 (0.0 ~ 1.0)
    misc_cost_per_unit = Column(Float, nullable=False, default=0.0)
    # 기타 잡비 (단위당)

    # ── 아마존 수수료 오버라이드 ────────────────────────────────────────────
    amazon_referral_fee_rate = Column(Float, nullable=True)
    # None이면 카테고리 기본값 사용
    fba_fee_override = Column(Float, nullable=True)
    # None이면 SP-API 조회값 사용

    # ── 환율 ────────────────────────────────────────────────────────────────
    exchange_rate_usd_krw = Column(Float, nullable=False, default=1300.0)

    # ── 메모 ─────────────────────────────────────────────────────────────────
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(String(100), default="system")
    is_locked = Column(Boolean, default=False, nullable=False)
    # 다음 주 생성 시 이전 주는 자동 lock

    # Relations
    margin_records = relationship("MarginRecord", back_populates="weekly_state")

    def __repr__(self) -> str:
        return f"<WeeklyState {self.week_key} locked={self.is_locked}>"


# ── 불변성 보장: UPDATE / DELETE 시도 차단 ────────────────────────────────────
@event.listens_for(WeeklyState, "before_update")
def _prevent_locked_update(mapper, connection, target: WeeklyState):
    if target.is_locked:
        raise PermissionError(
            f"WeeklyState '{target.week_key}' is locked and cannot be modified."
        )


# ══════════════════════════════════════════════════════════════════════════════
# 2. PRODUCTS — 크롤링된 상품 정보
# ══════════════════════════════════════════════════════════════════════════════

class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    asin = Column(String(20), unique=True, nullable=False, index=True)
    title = Column(Text, nullable=False)
    brand = Column(String(200), nullable=True)
    category = Column(String(200), nullable=True)
    image_url = Column(Text, nullable=True)

    # ── 규격 ─────────────────────────────────────────────────────────────────
    weight_kg = Column(Float, nullable=True)
    length_cm = Column(Float, nullable=True)
    width_cm = Column(Float, nullable=True)
    height_cm = Column(Float, nullable=True)

    # ── 리스크 플래그 ──────────────────────────────────────────────────────
    ip_risk_level = Column(
        Enum("NONE", "LOW", "MEDIUM", "HIGH", name="ip_risk_enum"),
        default="NONE",
    )
    uspto_registered = Column(Boolean, default=False)
    uspto_trademark_data = Column(JSON, nullable=True)
    amazon_sold_by_amazon = Column(Boolean, default=False)
    # Amazon.com이 직접 판매자인 경우

    first_seen_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relations
    margin_records = relationship("MarginRecord", back_populates="product")
    price_history = relationship("PriceSnapshot", back_populates="product")
    box_recommendations = relationship("BoxRecommendation", back_populates="product")


# ══════════════════════════════════════════════════════════════════════════════
# 3. PRICE SNAPSHOT — 가격 이력 (시계열)
# ══════════════════════════════════════════════════════════════════════════════

class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    recorded_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    buy_box_price = Column(Float, nullable=True)
    lowest_new_price = Column(Float, nullable=True)
    source_price = Column(Float, nullable=True)   # 소싱처 가격
    fba_fee = Column(Float, nullable=True)
    referral_fee = Column(Float, nullable=True)

    sellers_count = Column(Integer, nullable=True)
    buy_box_seller = Column(String(200), nullable=True)

    product = relationship("Product", back_populates="price_history", foreign_keys=[product_id])


# ══════════════════════════════════════════════════════════════════════════════
# 4. MARGIN RECORD — 주간 마진 계산 결과 (불변)
# ══════════════════════════════════════════════════════════════════════════════

class MarginRecord(Base):
    """
    특정 주차 + 특정 상품의 마진 계산 결과.
    WeeklyState.is_locked 후에는 수정 불가.
    """
    __tablename__ = "margin_records"
    __table_args__ = (
        UniqueConstraint("week_key", "product_id", name="uq_week_product"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    week_key = Column(String(10), ForeignKey("weekly_states.week_key"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)

    source_price = Column(Float, nullable=False)
    amazon_price = Column(Float, nullable=False)
    fba_fee = Column(Float, nullable=False)
    referral_fee = Column(Float, nullable=False)
    total_cost = Column(Float, nullable=False)
    gross_profit = Column(Float, nullable=False)
    margin_rate = Column(Float, nullable=False)
    # margin_rate = gross_profit / amazon_price

    roi = Column(Float, nullable=False)
    # roi = gross_profit / total_cost

    # 계산에 사용된 주간 비용 스냅샷 (비정규화 — 불변성 보장용)
    cost_snapshot = Column(JSON, nullable=False)

    calculated_at = Column(DateTime, default=datetime.utcnow)

    weekly_state = relationship("WeeklyState", back_populates="margin_records")
    product = relationship("Product", back_populates="margin_records")


# ══════════════════════════════════════════════════════════════════════════════
# 5. BOX RECOMMENDATION — Opti-Packer 결과
# ══════════════════════════════════════════════════════════════════════════════

class BoxRecommendation(Base):
    __tablename__ = "box_recommendations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # nullable=True: 복수 상품(multi-item) 박스 구성에서는 단일 product_id 없음
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    week_key = Column(String(10), nullable=False, index=True)

    box_size_id = Column(String(20), nullable=False)
    box_name = Column(String(50), nullable=True)          # "우체국 5호" 등 인간 친화 이름
    label = Column(String(100), nullable=True)            # "황금 박스 #1" 등 사용자 레이블

    units_per_box = Column(Integer, nullable=True)        # 단일 상품 모드
    total_units = Column(Integer, nullable=True)
    total_boxes = Column(Integer, nullable=True)

    # ── 중량 정보 ─────────────────────────────────────────────────────────────
    actual_weight_kg = Column(Float, nullable=True)
    dimensional_weight_kg = Column(Float, nullable=True)
    chargeable_weight_kg = Column(Float, nullable=True)   # max(actual, dim)

    # ── 비용·마진 ─────────────────────────────────────────────────────────────
    estimated_shipping_cost = Column(Float, nullable=True)
    cost_per_unit = Column(Float, nullable=True)
    total_margin_usd = Column(Float, nullable=True)       # 포함 상품들의 총 마진
    net_margin_usd = Column(Float, nullable=True)         # total_margin - shipping_cost
    roi = Column(Float, nullable=True)                    # net_margin / shipping_cost
    packing_efficiency = Column(Float, nullable=True)     # 부피 활용률 0.0~1.0

    packing_detail = Column(JSON, nullable=True)
    # multi-item: [{"asin": "...", "title": "...", "qty": 3, "margin_usd": 9.0}, ...]

    is_approved = Column(Boolean, default=False)
    approved_at = Column(DateTime, nullable=True)
    approved_by = Column(String(100), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    product = relationship("Product", back_populates="box_recommendations")
    shipments = relationship("Shipment", back_populates="box_recommendation")


# ══════════════════════════════════════════════════════════════════════════════
# 6. SHIPMENT — 물류 추적
# ══════════════════════════════════════════════════════════════════════════════

class ShipmentStatus(str, enum.Enum):
    PENDING = "PENDING"
    PICKED_UP = "PICKED_UP"
    IN_TRANSIT = "IN_TRANSIT"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    FC_RECEIVING = "FC_RECEIVING"
    FC_RECEIVED = "FC_RECEIVED"
    FC_DELAYED = "FC_DELAYED"
    EXCEPTION = "EXCEPTION"


class Shipment(Base):
    __tablename__ = "shipments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    box_recommendation_id = Column(
        Integer, ForeignKey("box_recommendations.id"), nullable=True
    )
    week_key = Column(String(10), nullable=False, index=True)

    # ── 배송 정보 ──────────────────────────────────────────────────────────
    carrier = Column(String(50), nullable=False, default="UPS")
    tracking_number = Column(String(100), nullable=False, unique=True, index=True)
    amazon_shipment_id = Column(String(100), nullable=True, index=True)
    # SP-API FBA Inbound Shipment ID

    status = Column(
        Enum(*[s.value for s in ShipmentStatus], name="shipment_status_enum"),
        default=ShipmentStatus.PENDING.value,
        nullable=False,
    )
    last_event = Column(Text, nullable=True)
    last_checked_at = Column(DateTime, nullable=True)

    estimated_delivery = Column(DateTime, nullable=True)
    actual_delivery = Column(DateTime, nullable=True)
    fc_received_at = Column(DateTime, nullable=True)

    alert_sent = Column(Boolean, default=False)
    alert_message = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    box_recommendation = relationship("BoxRecommendation", back_populates="shipments")
    tracking_events = relationship("TrackingEvent", back_populates="shipment")


class TrackingEvent(Base):
    __tablename__ = "tracking_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    shipment_id = Column(Integer, ForeignKey("shipments.id"), nullable=False, index=True)
    event_time = Column(DateTime, nullable=False)
    location = Column(String(300), nullable=True)
    description = Column(Text, nullable=False)
    raw_data = Column(JSON, nullable=True)

    shipment = relationship("Shipment", back_populates="tracking_events")


# ══════════════════════════════════════════════════════════════════════════════
# 7. INVOICE — 자동 생성 인보이스
# ══════════════════════════════════════════════════════════════════════════════

class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    invoice_number = Column(String(50), unique=True, nullable=False, index=True)
    # 형식: INV-2026W18-0001

    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    shipment_id = Column(Integer, ForeignKey("shipments.id"), nullable=True)
    week_key = Column(String(10), nullable=False)

    buyer_name = Column(String(200), nullable=True)
    buyer_address = Column(Text, nullable=True)

    line_items = Column(JSON, nullable=False)
    # [{"description": "...", "qty": 10, "unit_price": 9.99, "total": 99.9}]

    subtotal = Column(Float, nullable=False)
    tax_rate = Column(Float, nullable=False, default=0.0)
    tax_amount = Column(Float, nullable=False, default=0.0)
    total_amount = Column(Float, nullable=False)

    pdf_path = Column(Text, nullable=True)
    issued_at = Column(DateTime, default=datetime.utcnow)
    purpose = Column(String(200), nullable=True)
    # "Amazon IP Dispute", "FBA Inbound", etc.
