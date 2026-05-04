"""
Arbitrage-X — Web Dashboard (Streamlit)

실행:
  streamlit run dashboard.py
  python cli.py run-dashboard

탭 구성:
  1. Overview        — 파이프라인 요약 지표 카드
  2. 황금 박스 추천   — BoxRecommendation DB 테이블 (ROI 내림차순)
  3. 주간 변수 설정   — WeeklyState 폼 UI
  4. 이슈 & 인보이스  — 물류 이슈 + PDF 인보이스 다운로드
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

# 프로젝트 루트를 sys.path에 추가 (streamlit run 실행 위치에 무관하게 동작)
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ══════════════════════════════════════════════════════════════════════════════
# 페이지 설정 (반드시 다른 st 호출 전에 위치)
# ══════════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="Arbitrage-X Dashboard",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ══════════════════════════════════════════════════════════════════════════════
# 공통 헬퍼
# ══════════════════════════════════════════════════════════════════════════════

def _db_ok() -> bool:
    """DB 연결 및 테이블 존재 여부 확인."""
    try:
        from arbitrage_x.db.database import engine
        from arbitrage_x.db.models import Base
        with engine.connect() as conn:
            from sqlalchemy import text
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _get_week_key() -> str:
    from arbitrage_x.utils.week_utils import get_current_week_key
    return get_current_week_key()


def _fmt_roi(v) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.1f}%"


def _fmt_usd(v) -> str:
    if v is None:
        return "—"
    return f"${v:,.2f}"


def _no_data(msg: str = "데이터가 없습니다.") -> None:
    st.info(f"ℹ️ {msg}", icon="📭")


# ══════════════════════════════════════════════════════════════════════════════
# DB 쿼리 함수 (캐시 TTL 30s — 자동 갱신)
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=30, show_spinner=False)
def _fetch_overview(week_key: str) -> dict:
    """Overview 탭용 집계 데이터를 plain dict로 반환한다."""
    try:
        from arbitrage_x.db.database import SessionLocal
        from arbitrage_x.db.models import BoxRecommendation, MarginRecord, Product, Shipment
        db = SessionLocal()
        try:
            total_products = db.query(Product).count()
            margin_records = db.query(MarginRecord).filter_by(week_key=week_key).all()
            eligible = [r for r in margin_records if r.roi >= 0.30]
            box_recs = db.query(BoxRecommendation).filter_by(week_key=week_key).count()
            active_shipments = db.query(Shipment).filter(
                Shipment.status.notin_(["FC_RECEIVED", "DELIVERED"])
            ).count()
            best_roi = None
            if margin_records:
                best_roi = max(r.roi for r in margin_records)
            return {
                "total_products": total_products,
                "margin_records": len(margin_records),
                "eligible": len(eligible),
                "box_recommendations": box_recs,
                "active_shipments": active_shipments,
                "best_roi": best_roi,
            }
        finally:
            db.close()
    except Exception as e:
        return {"error": str(e)}


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_box_recommendations(week_key: str | None, limit: int) -> list[dict]:
    try:
        from arbitrage_x.db.database import SessionLocal
        from arbitrage_x.db.models import BoxRecommendation
        db = SessionLocal()
        try:
            q = db.query(BoxRecommendation)
            if week_key:
                q = q.filter_by(week_key=week_key)
            recs = q.order_by(BoxRecommendation.roi.desc().nullslast()).limit(limit).all()
            return [
                {
                    "id": r.id,
                    "주차": r.week_key,
                    "박스 ID": r.box_size_id,
                    "박스명": r.box_name or "—",
                    "레이블": r.label or "—",
                    "ROI": _fmt_roi(r.roi),
                    "순이익 (USD)": _fmt_usd(r.net_margin_usd),
                    "총 마진 (USD)": _fmt_usd(r.total_margin_usd),
                    "배송비 (USD)": _fmt_usd(r.estimated_shipping_cost),
                    "충전중량 (kg)": f"{r.chargeable_weight_kg:.2f}" if r.chargeable_weight_kg else "—",
                    "효율": f"{r.packing_efficiency * 100:.0f}%" if r.packing_efficiency else "—",
                    "승인": "✅" if r.is_approved else "—",
                    "생성일": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "—",
                    "_roi_raw": r.roi or 0,
                }
                for r in recs
            ]
        finally:
            db.close()
    except Exception as e:
        return [{"error": str(e)}]


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_weekly_state(week_key: str) -> dict | None:
    try:
        from arbitrage_x.db.database import SessionLocal
        from arbitrage_x.db.models import WeeklyState
        db = SessionLocal()
        try:
            state = db.query(WeeklyState).filter_by(week_key=week_key).first()
            if not state:
                return None
            return {
                "id": state.id,
                "week_key": state.week_key,
                "is_locked": state.is_locked,
                "exchange_rate_usd_krw": state.exchange_rate_usd_krw,
                "domestic_shipping_cost": state.domestic_shipping_cost,
                "international_shipping_cost": state.international_shipping_cost,
                "fba_fee_override": state.fba_fee_override or 0.0,
                "amazon_referral_fee_rate": state.amazon_referral_fee_rate or 0.15,
                "prep_service_fee": state.prep_service_fee,
                "customs_duty_rate": state.customs_duty_rate,
                "misc_cost_per_unit": state.misc_cost_per_unit,
                "notes": state.notes or "",
                "created_at": state.created_at.isoformat() if state.created_at else "",
            }
        finally:
            db.close()
    except Exception as e:
        return {"error": str(e)}


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_shipments() -> list[dict]:
    try:
        from arbitrage_x.db.database import SessionLocal
        from arbitrage_x.db.models import Shipment
        db = SessionLocal()
        try:
            shipments = (
                db.query(Shipment)
                .order_by(Shipment.updated_at.desc())
                .limit(50)
                .all()
            )
            return [
                {
                    "트래킹 번호": s.tracking_number,
                    "상태": s.status,
                    "캐리어": s.carrier,
                    "Amazon 운송장 ID": s.amazon_shipment_id or "—",
                    "마지막 이벤트": (s.last_event or "—")[:60],
                    "알림 발송": "✅" if s.alert_sent else "—",
                    "업데이트": s.updated_at.strftime("%m-%d %H:%M") if s.updated_at else "—",
                    "_alert": s.alert_sent,
                    "_status": s.status,
                }
                for s in shipments
            ]
        finally:
            db.close()
    except Exception as e:
        return [{"error": str(e)}]


def _list_invoices() -> list[Path]:
    try:
        from config.settings import INVOICES_DIR
        if not INVOICES_DIR.exists():
            return []
        return sorted(INVOICES_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════════
# 사이드바
# ══════════════════════════════════════════════════════════════════════════════

def _render_sidebar(week_key: str) -> None:
    with st.sidebar:
        st.image("https://img.icons8.com/emoji/96/package-emoji.png", width=64)
        st.title("Arbitrage-X")
        st.caption(f"현재 주차: **{week_key}**")
        st.divider()

        st.subheader("빠른 실행")
        if st.button("🔄 파이프라인 실행", use_container_width=True, key="sb_run"):
            _run_pipeline_now(week_key)

        if st.button("📄 테스트 인보이스 생성", use_container_width=True, key="sb_inv"):
            _gen_test_invoice()

        if st.button("📣 주간 알림 전송", use_container_width=True, key="sb_remind"):
            _send_weekly_reminder(week_key)

        st.divider()
        st.caption("© 2026 Arbitrage-X")


def _run_pipeline_now(week_key: str) -> None:
    with st.spinner("파이프라인 실행 중..."):
        try:
            from arbitrage_x.core.orchestrator import ArbitrageOrchestrator, PipelineConfig
            config = PipelineConfig(week_key=week_key, keywords=["electronics"], dry_run=False)
            result = ArbitrageOrchestrator().run(config)
            st.sidebar.success(
                f"완료! 적격 {result.margin_eligible}개 / "
                f"황금박스 {'있음' if result.golden_box else '없음'}"
            )
            st.cache_data.clear()
        except Exception as e:
            st.sidebar.error(f"실행 실패: {e}")


def _gen_test_invoice() -> None:
    with st.spinner("인보이스 생성 중..."):
        try:
            from arbitrage_x.modules.invoice_generator import InvoiceData, InvoiceGenerator, LineItem
            gen = InvoiceGenerator()
            data = InvoiceData(
                invoice_number=InvoiceGenerator.build_invoice_number(
                    datetime.now().strftime("%Y-W%V"), 9999
                ),
                issued_date=datetime.now(),
                purpose="Dashboard Test Invoice",
                buyer_name="Amazon.com Services LLC",
                buyer_address="410 Terry Ave N\nSeattle, WA 98109",
                line_items=[
                    LineItem("Test Product", qty=5, unit_price=19.99, asin="B09TEST0001")
                ],
            )
            path = gen.generate(data)
            st.sidebar.success(f"생성 완료: {path.name}")
            st.cache_data.clear()
        except Exception as e:
            st.sidebar.error(f"생성 실패: {e}")


def _send_weekly_reminder(week_key: str) -> None:
    with st.spinner("알림 전송 중..."):
        try:
            from arbitrage_x.utils.notifier import TelegramNotifier
            with TelegramNotifier() as n:
                sent = n.send_weekly_reminder(week_key)
            if sent:
                st.sidebar.success("텔레그램 알림 전송 완료!")
            else:
                st.sidebar.warning("Telegram 미설정 (TELEGRAM_BOT_TOKEN 확인)")
        except Exception as e:
            st.sidebar.error(f"전송 실패: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 탭 1: Overview
# ══════════════════════════════════════════════════════════════════════════════

def _tab_overview(week_key: str) -> None:
    st.header("📊 이번 주 파이프라인 요약")
    st.caption(f"주차: {week_key} · 자동 갱신 주기: 30초")

    data = _fetch_overview(week_key)
    if "error" in data:
        st.error(f"DB 조회 오류: {data['error']}")
        st.info("먼저 사이드바에서 **파이프라인 실행** 또는 `python cli.py init-db`를 실행해 주세요.")
        return

    # 지표 카드
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("📦 등록 상품", f"{data['total_products']:,}개")
    col2.metric("📋 마진 계산", f"{data['margin_records']}개", help="이번 주 마진 레코드 수")
    col3.metric(
        "✅ 적격 상품",
        f"{data['eligible']}개",
        delta=f"ROI ≥ 30%" if data['eligible'] > 0 else None,
    )
    col4.metric("📦 황금 박스 구성", f"{data['box_recommendations']}개")
    col5.metric("🚚 활성 배송", f"{data['active_shipments']}건")

    st.divider()

    # 최고 ROI 표시
    if data["best_roi"] is not None:
        st.success(
            f"🏆 이번 주 최고 ROI: **{data['best_roi'] * 100:.1f}%**",
        )

    # 데이터 없을 때 가이드
    if data["total_products"] == 0 and data["margin_records"] == 0:
        st.info(
            "📭 아직 파이프라인이 실행된 적 없습니다.\n\n"
            "- 사이드바 **파이프라인 실행** 버튼을 클릭하거나\n"
            "- `python cli.py run-pipeline` 명령을 실행하세요.",
            icon="🚀",
        )

    # 최근 마진 레코드 미리보기
    st.subheader("최근 마진 레코드")
    _show_recent_margin_records(week_key)


def _show_recent_margin_records(week_key: str) -> None:
    try:
        from arbitrage_x.db.database import SessionLocal
        from arbitrage_x.db.models import MarginRecord, Product
        db = SessionLocal()
        try:
            rows = (
                db.query(MarginRecord, Product)
                .join(Product, MarginRecord.product_id == Product.id)
                .filter(MarginRecord.week_key == week_key)
                .order_by(MarginRecord.roi.desc())
                .limit(10)
                .all()
            )
            if not rows:
                _no_data("이번 주 마진 레코드가 없습니다.")
                return
            df = pd.DataFrame([
                {
                    "ASIN": p.asin,
                    "상품명": (p.title or "")[:40],
                    "바이박스 ($)": f"${r.amazon_price:.2f}",
                    "총 비용 ($)": f"${r.total_cost:.2f}",
                    "이익 ($)": f"${r.gross_profit:.2f}",
                    "마진율": f"{r.margin_rate * 100:.1f}%",
                    "ROI": f"{r.roi * 100:.1f}%",
                }
                for r, p in rows
            ])
            st.dataframe(df, use_container_width=True, hide_index=True)
        finally:
            db.close()
    except Exception as e:
        st.warning(f"마진 레코드 조회 실패: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 탭 2: 황금 박스 추천
# ══════════════════════════════════════════════════════════════════════════════

def _tab_box_recommendations(week_key: str) -> None:
    st.header("📦 황금 박스 추천 (Opti-Packer)")

    col_filter, col_limit = st.columns([2, 1])
    with col_filter:
        filter_week = st.selectbox(
            "주차 필터",
            options=["전체 보기", week_key],
            index=1,
        )
    with col_limit:
        limit = st.number_input("최대 표시 수", min_value=5, max_value=100, value=20, step=5)

    selected_week = None if filter_week == "전체 보기" else filter_week
    recs = _fetch_box_recommendations(selected_week, limit)

    if not recs:
        _no_data("박스 추천 데이터가 없습니다. 파이프라인을 실행해 주세요.")
        return

    if recs and "error" in recs[0]:
        st.error(f"DB 오류: {recs[0]['error']}")
        return

    # ROI 기준 색상 강조
    df_raw = pd.DataFrame(recs)
    display_cols = [c for c in df_raw.columns if not c.startswith("_")]
    df_display = df_raw[display_cols].copy()

    st.dataframe(
        df_display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "ROI": st.column_config.TextColumn("ROI", help="순이익 / 배송비"),
            "순이익 (USD)": st.column_config.TextColumn("순이익 (USD)"),
            "효율": st.column_config.TextColumn("부피 활용률"),
            "승인": st.column_config.TextColumn("승인 여부"),
        },
    )

    # 상위 ROI 분포 차트
    valid = [r for r in recs if isinstance(r.get("_roi_raw"), (int, float)) and r["_roi_raw"] > 0]
    if len(valid) >= 2:
        st.subheader("ROI 분포")
        chart_df = pd.DataFrame({
            "박스": [r["박스명"] + " " + r["주차"] for r in valid],
            "ROI (%)": [r["_roi_raw"] * 100 for r in valid],
        }).sort_values("ROI (%)", ascending=False).head(10)
        st.bar_chart(chart_df.set_index("박스")["ROI (%)"])

    st.caption(f"총 {len(recs)}건 조회됨")


# ══════════════════════════════════════════════════════════════════════════════
# 탭 3: 주간 변수 설정
# ══════════════════════════════════════════════════════════════════════════════

def _tab_weekly_state(week_key: str) -> None:
    st.header("⚙️ 주간 변수 설정")
    st.caption(f"주차: **{week_key}** · 한 번 생성 후 다음 주가 시작되면 자동 잠금")

    current = _fetch_weekly_state(week_key)

    if current and "error" in current:
        st.error(f"DB 조회 오류: {current['error']}")
        return

    # 잠금 상태 배지
    if current and current.get("is_locked"):
        st.warning("🔒 이번 주 데이터는 잠금 상태입니다. 수정할 수 없습니다.", icon="🔒")
        _show_locked_state(current)
        return

    is_update = current is not None
    action_label = "✏️ 업데이트" if is_update else "✅ 생성"

    if is_update:
        st.info(f"이미 이번 주 WeeklyState가 존재합니다 (생성: {current.get('created_at', '')[:10]})", icon="ℹ️")

    with st.form("weekly_state_form"):
        st.subheader("📌 배송 & 물류 비용 (USD)")
        c1, c2 = st.columns(2)
        with c1:
            domestic = st.number_input(
                "국내 배송비 (박스당)", min_value=0.0, step=0.5, format="%.2f",
                value=float(current["domestic_shipping_cost"]) if current else 2.0,
                help="한국 내 발송 기본 운임",
            )
            international = st.number_input(
                "국제 배송비 (박스당)", min_value=0.0, step=0.5, format="%.2f",
                value=float(current["international_shipping_cost"]) if current else 5.0,
                help="미국행 국제 물류비",
            )
            prep_fee = st.number_input(
                "FBA Prep 수수료 (단위당)", min_value=0.0, step=0.1, format="%.2f",
                value=float(current["prep_service_fee"]) if current else 0.5,
            )
        with c2:
            fba_fee = st.number_input(
                "FBA 수수료 오버라이드 (단위당)", min_value=0.0, step=0.5, format="%.2f",
                value=float(current["fba_fee_override"]) if current else 3.5,
                help="None이면 SP-API 조회값 사용",
            )
            misc = st.number_input(
                "기타 잡비 (단위당)", min_value=0.0, step=0.1, format="%.2f",
                value=float(current["misc_cost_per_unit"]) if current else 0.5,
            )
            customs = st.number_input(
                "관세율 (%)", min_value=0.0, max_value=100.0, step=0.5, format="%.1f",
                value=float((current["customs_duty_rate"] or 0) * 100) if current else 0.0,
                help="0 ~ 100 범위",
            )

        st.subheader("💱 환율 & 수수료")
        c3, c4 = st.columns(2)
        with c3:
            exchange_rate = st.number_input(
                "환율 (KRW/USD)", min_value=500.0, max_value=3000.0, step=10.0, format="%.0f",
                value=float(current["exchange_rate_usd_krw"]) if current else 1300.0,
            )
        with c4:
            referral_rate = st.number_input(
                "아마존 레퍼럴 수수료율 (%)", min_value=0.0, max_value=50.0, step=0.5, format="%.1f",
                value=float((current["amazon_referral_fee_rate"] or 0.15) * 100) if current else 15.0,
            )

        notes = st.text_area(
            "메모", value=current.get("notes", "") if current else "",
            placeholder="특이사항을 입력하세요...",
        )

        submitted = st.form_submit_button(action_label, use_container_width=True, type="primary")

    if submitted:
        _save_weekly_state(
            week_key=week_key,
            is_update=is_update,
            domestic=domestic,
            international=international,
            prep_fee=prep_fee,
            fba_fee=fba_fee,
            misc=misc,
            customs_rate=customs / 100.0,
            exchange_rate=exchange_rate,
            referral_rate=referral_rate / 100.0,
            notes=notes,
        )


def _show_locked_state(state: dict) -> None:
    """잠금 상태의 WeeklyState를 읽기 전용으로 표시한다."""
    rows = {
        "환율 (KRW/USD)": f"{state['exchange_rate_usd_krw']:,.0f}",
        "국내 배송비": _fmt_usd(state["domestic_shipping_cost"]),
        "국제 배송비": _fmt_usd(state["international_shipping_cost"]),
        "FBA 수수료": _fmt_usd(state["fba_fee_override"]),
        "레퍼럴율": f"{(state['amazon_referral_fee_rate'] or 0.15) * 100:.1f}%",
        "관세율": f"{(state['customs_duty_rate'] or 0) * 100:.1f}%",
        "Prep 수수료": _fmt_usd(state["prep_service_fee"]),
        "기타 잡비": _fmt_usd(state["misc_cost_per_unit"]),
        "메모": state.get("notes") or "—",
    }
    df = pd.DataFrame(rows.items(), columns=["항목", "값"])
    st.dataframe(df, use_container_width=True, hide_index=True)


def _save_weekly_state(**kwargs) -> None:
    try:
        from arbitrage_x.db.database import SessionLocal
        from arbitrage_x.core.weekly_state_manager import WeeklyStateManager
        db = SessionLocal()
        try:
            wsm = WeeklyStateManager(db)
            week_key = kwargs["week_key"]
            params = {
                "domestic_shipping_cost": kwargs["domestic"],
                "international_shipping_cost": kwargs["international"],
                "prep_service_fee": kwargs["prep_fee"],
                "fba_fee_override": kwargs["fba_fee"],
                "misc_cost_per_unit": kwargs["misc"],
                "customs_duty_rate": kwargs["customs_rate"],
                "exchange_rate_usd_krw": kwargs["exchange_rate"],
                "amazon_referral_fee_rate": kwargs["referral_rate"],
                "notes": kwargs["notes"] or None,
            }
            if kwargs["is_update"]:
                wsm.update_current_week(week_key=week_key, **params)
                st.success("✅ WeeklyState 업데이트 완료!")
            else:
                wsm.get_or_create_current_week(**params, created_by="dashboard")
                st.success("✅ WeeklyState 생성 완료!")
            db.commit()
            st.cache_data.clear()
        finally:
            db.close()
    except PermissionError as e:
        st.error(f"🔒 {e}")
    except Exception as e:
        st.error(f"저장 실패: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 탭 4: 이슈 & 인보이스
# ══════════════════════════════════════════════════════════════════════════════

def _tab_issues_and_invoices() -> None:
    st.header("🚨 이슈 & 인보이스")

    issue_col, inv_col = st.columns([1, 1])

    # ── 물류 이슈 ─────────────────────────────────────────────────────────────
    with issue_col:
        st.subheader("🚚 배송 현황")
        shipments = _fetch_shipments()

        if not shipments:
            _no_data("추적 중인 배송이 없습니다.")
        elif shipments and "error" in shipments[0]:
            st.error(f"DB 오류: {shipments[0]['error']}")
        else:
            display_cols = [c for c in shipments[0].keys() if not c.startswith("_")]
            df = pd.DataFrame(shipments)[display_cols]

            # 이슈 상태 강조
            def _row_style(row):
                if row.get("상태") in ("EXCEPTION", "FC_DELAYED"):
                    return ["background-color: #ffe0e0"] * len(row)
                if row.get("상태") == "FC_RECEIVED":
                    return ["background-color: #e0ffe0"] * len(row)
                return [""] * len(row)

            st.dataframe(df, use_container_width=True, hide_index=True)

            issue_count = sum(
                1 for s in shipments
                if s.get("_status") in ("EXCEPTION", "FC_DELAYED")
            )
            if issue_count:
                st.error(f"⚠️ 이슈 배송 {issue_count}건 감지 — 인보이스 소명이 필요할 수 있습니다.")

        # 수동 이슈 감지 버튼
        if st.button("🔍 배송 이슈 즉시 점검", key="detect_issues"):
            _detect_issues_now()

    # ── 인보이스 PDF 목록 ──────────────────────────────────────────────────────
    with inv_col:
        st.subheader("📄 Commercial Invoice")
        invoices = _list_invoices()

        if not invoices:
            _no_data("생성된 인보이스가 없습니다.")
            st.caption("이슈가 감지되면 인보이스가 자동 생성됩니다.")
        else:
            st.caption(f"총 {len(invoices)}개의 PDF 파일")
            for pdf_path in invoices[:20]:
                col_name, col_btn = st.columns([3, 1])
                with col_name:
                    size_kb = pdf_path.stat().st_size // 1024
                    mtime = datetime.fromtimestamp(pdf_path.stat().st_mtime).strftime("%m-%d %H:%M")
                    st.text(f"📄 {pdf_path.stem}\n   {size_kb} KB · {mtime}")
                with col_btn:
                    try:
                        with open(pdf_path, "rb") as f:
                            st.download_button(
                                label="⬇️",
                                data=f.read(),
                                file_name=pdf_path.name,
                                mime="application/pdf",
                                key=f"dl_{pdf_path.stem}",
                                help=f"다운로드: {pdf_path.name}",
                            )
                    except Exception:
                        st.caption("읽기 오류")

        st.divider()
        if st.button("📄 테스트 인보이스 생성", key="gen_inv_tab4"):
            _gen_test_invoice_inline()


def _detect_issues_now() -> None:
    with st.spinner("배송 이슈 감지 중..."):
        try:
            from arbitrage_x.db.database import SessionLocal
            from arbitrage_x.db.models import Shipment
            from arbitrage_x.modules.logistics_api import LogisticsTracker, MockUPSClient, MockAmazonSPClient
            db = SessionLocal()
            try:
                active = db.query(Shipment).filter(
                    Shipment.status.notin_(["FC_RECEIVED", "DELIVERED"])
                ).all()
                if not active:
                    st.info("현재 추적 중인 활성 배송이 없습니다.")
                    return
                tracker = LogisticsTracker()
                total_issues = 0
                for shipment in active:
                    issues = tracker.detect_issues(
                        shipment.tracking_number,
                        amazon_shipment_id=shipment.amazon_shipment_id,
                    )
                    total_issues += len(issues)
                st.success(f"점검 완료 — {len(active)}건 배송, {total_issues}건 이슈 감지")
                st.cache_data.clear()
            finally:
                db.close()
        except Exception as e:
            st.error(f"점검 실패: {e}")


def _gen_test_invoice_inline() -> None:
    with st.spinner("인보이스 생성 중..."):
        try:
            from arbitrage_x.modules.invoice_generator import InvoiceData, InvoiceGenerator, LineItem
            gen = InvoiceGenerator()
            seq = len(_list_invoices()) + 1
            data = InvoiceData(
                invoice_number=InvoiceGenerator.build_invoice_number(
                    datetime.now().strftime("%Y-W%V"), seq
                ),
                issued_date=datetime.now(),
                purpose="Amazon IP Dispute — Dashboard Test",
                buyer_name="Amazon.com Services LLC",
                buyer_address="410 Terry Ave N\nSeattle, WA 98109",
                ship_to_name="Amazon BFI4",
                ship_to_address="1800 140th Ave E\nSumner, WA 98390",
                line_items=[
                    LineItem("Test Widget", qty=10, unit_price=24.99, asin="B09DASH0001"),
                ],
                notes="대시보드에서 생성된 테스트 인보이스입니다.",
            )
            path = gen.generate(data)
            st.success(f"✅ 생성 완료: `{path.name}`")
            st.cache_data.clear()
        except Exception as e:
            st.error(f"생성 실패: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 메인 진입점
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    week_key = _get_week_key()

    # DB 연결 불가 시 안내
    if not _db_ok():
        st.error("⚠️ DB에 연결할 수 없습니다.")
        st.info(
            "아래 명령으로 DB를 초기화하세요:\n\n"
            "```\npython cli.py init-db\n```",
            icon="🛠️",
        )

    _render_sidebar(week_key)

    st.title("📦 Arbitrage-X Dashboard")
    st.caption(f"주차: **{week_key}** | {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    tab1, tab2, tab3, tab4 = st.tabs([
        "📊 Overview",
        "📦 황금 박스 추천",
        "⚙️ 주간 변수 설정",
        "🚨 이슈 & 인보이스",
    ])

    with tab1:
        _tab_overview(week_key)

    with tab2:
        _tab_box_recommendations(week_key)

    with tab3:
        _tab_weekly_state(week_key)

    with tab4:
        _tab_issues_and_invoices()


if __name__ == "__main__":
    main()
