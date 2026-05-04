"""
Arbitrage-X — CLI 제어 인터페이스

Termux 터미널 환경에서 시스템을 수동으로 조작한다.

사용법:
  python cli.py run-pipeline               # 전체 파이프라인 즉시 실행
  python cli.py run-pipeline --dry-run     # DB 저장 없이 드라이런
  python cli.py run-pipeline --keywords "electronics" "beauty"
  python cli.py test-invoice               # 인보이스 테스트 PDF 생성
  python cli.py init-db                    # DB 테이블 초기화
  python cli.py weekly-status              # 이번 주 상태 조회
  python cli.py list-recommendations       # 박스 추천 목록 조회
  python cli.py send-reminder              # 주간 비용 입력 알림 즉시 발송
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.WARNING,   # CLI는 WARNING 이상만 기본 출력
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)


# ══════════════════════════════════════════════════════════════════════════════
# 커맨드 핸들러
# ══════════════════════════════════════════════════════════════════════════════


def cmd_init_db(args: argparse.Namespace) -> int:
    from arbitrage_x.db.database import init_db
    init_db()
    print("✓ DB 초기화 완료")
    return 0


def cmd_run_pipeline(args: argparse.Namespace) -> int:
    from arbitrage_x.core.orchestrator import ArbitrageOrchestrator, PipelineConfig
    from arbitrage_x.utils.week_utils import get_current_week_key

    week_key = args.week_key or get_current_week_key()
    keywords = args.keywords or ["electronics", "beauty"]

    config = PipelineConfig(
        week_key=week_key,
        keywords=keywords,
        max_products_per_keyword=args.max_products,
        min_roi=args.min_roi / 100.0,
        dry_run=args.dry_run,
    )

    print(f"▶ 파이프라인 시작 [{week_key}] keywords={keywords} dry_run={args.dry_run}")
    orchestrator = ArbitrageOrchestrator()
    result = orchestrator.run(config)

    print(result.summary())
    if result.errors:
        print(f"⚠  오류: {result.errors}")
        return 1
    return 0


def cmd_test_invoice(args: argparse.Namespace) -> int:
    from arbitrage_x.modules.invoice_generator import InvoiceData, InvoiceGenerator, LineItem

    output_dir = Path(args.output_dir) if args.output_dir else Path("data/invoices")
    gen = InvoiceGenerator(output_dir=output_dir)

    inv_num = InvoiceGenerator.build_invoice_number(
        datetime.now().strftime("%Y-W%V"),
        sequence=9999,
    )
    data = InvoiceData(
        invoice_number=inv_num,
        issued_date=datetime.now(),
        purpose="Amazon IP Dispute — CLI Test",
        buyer_name="Amazon.com Services LLC",
        buyer_address="410 Terry Ave N\nSeattle, WA 98109",
        ship_to_name="Amazon BFI4",
        ship_to_address="1800 140th Ave E\nSumner, WA 98390",
        line_items=[
            LineItem(description="Test Product Alpha", qty=10, unit_price=19.99, asin="B09TEST0001"),
            LineItem(description="Test Product Beta",  qty=5,  unit_price=39.99, asin="B09TEST0002"),
        ],
        tax_rate=0.0,
        notes="테스트 인보이스 — CLI test-invoice 명령으로 생성됨.",
    )

    path = gen.generate(data)
    print(f"✓ 인보이스 생성 완료: {path}")
    return 0


def cmd_weekly_status(args: argparse.Namespace) -> int:
    from arbitrage_x.db.database import get_db
    from arbitrage_x.core.weekly_state_manager import WeeklyStateManager
    from arbitrage_x.utils.week_utils import get_current_week_key

    week_key = args.week_key or get_current_week_key()

    with get_db() as db:
        wsm = WeeklyStateManager(db)
        state = wsm.get_state_for_week(week_key)
        if not state:
            print(f"⚠  {week_key} WeeklyState 없음. 먼저 run-pipeline 또는 init-db를 실행하세요.")
            return 1

        print(f"\n━━ WeeklyState [{week_key}] ━━")
        print(f"  환율       : {state.exchange_rate_usd_krw:,.0f} KRW/USD")
        print(f"  국내 배송  : ${state.domestic_shipping_cost:.2f}")
        print(f"  국제 배송  : ${state.international_shipping_cost:.2f}")
        print(f"  FBA 수수료 : ${state.fba_fee_override or '(SP-API 기본값)'}")
        print(f"  레퍼럴율   : {(state.amazon_referral_fee_rate or 0.15) * 100:.0f}%")
        print(f"  관세율     : {state.customs_duty_rate * 100:.1f}%")
        print(f"  잠금 여부  : {'🔒 잠김' if state.is_locked else '🔓 편집 가능'}")
        print(f"  메모       : {state.notes or '(없음)'}")

        # MarginRecord 집계
        from arbitrage_x.db.models import MarginRecord
        records = db.query(MarginRecord).filter_by(week_key=week_key).all()
        if records:
            total_profit = sum(r.gross_profit for r in records)
            avg_roi = sum(r.roi for r in records) / len(records)
            print(f"\n━━ 이번 주 마진 요약 ━━")
            print(f"  분석 상품  : {len(records)}개")
            print(f"  총 예상 이익: ${total_profit:,.2f}")
            print(f"  평균 ROI   : {avg_roi * 100:.1f}%")
    return 0


def cmd_list_recommendations(args: argparse.Namespace) -> int:
    from arbitrage_x.db.database import get_db
    from arbitrage_x.modules.box_optimizer import BoxArchiver
    from arbitrage_x.utils.week_utils import get_current_week_key

    week_key = args.week_key or get_current_week_key()

    with get_db() as db:
        archiver = BoxArchiver()
        recs = archiver.get_recommendations(db, week_key=week_key, limit=args.top)
        if not recs:
            print(f"⚠  {week_key} 추천 박스 없음.")
            return 0

        print(f"\n━━ 박스 추천 TOP {args.top} [{week_key}] ━━")
        for i, rec in enumerate(recs, 1):
            roi_str = f"{rec.roi * 100:.1f}%" if rec.roi else "N/A"
            net_str = f"${rec.net_margin_usd:.2f}" if rec.net_margin_usd else "N/A"
            print(
                f"  {i:2}. [{rec.box_size_id}] {rec.box_name or '':<15} "
                f"ROI={roi_str:<8} 순익={net_str:<10} "
                f"{'✓ 승인됨' if rec.is_approved else ''}"
            )
    return 0


def cmd_send_reminder(args: argparse.Namespace) -> int:
    from arbitrage_x.utils.notifier import TelegramNotifier
    from arbitrage_x.utils.week_utils import get_current_week_key

    week_key = args.week_key or get_current_week_key()
    with TelegramNotifier() as notifier:
        sent = notifier.send_weekly_reminder(week_key)
    if sent:
        print(f"✓ 주간 알림 전송 완료 [{week_key}]")
        return 0
    else:
        print("⚠  Telegram 미설정 또는 전송 실패 (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 확인)")
        return 1


# ══════════════════════════════════════════════════════════════════════════════
# 파서 구성
# ══════════════════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Arbitrage-X CLI — 아마존 아비트리지 자동화 시스템",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="상세 로그 출력")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # ── init-db ───────────────────────────────────────────────────────────────
    sub.add_parser("init-db", help="DB 테이블 초기화")

    # ── run-pipeline ─────────────────────────────────────────────────────────
    p_run = sub.add_parser("run-pipeline", help="전체 파이프라인 즉시 실행")
    p_run.add_argument("--week-key", help="대상 주차 (기본: 이번 주, 예: 2026-W19)")
    p_run.add_argument("--keywords", nargs="+", help="검색 키워드 목록")
    p_run.add_argument("--max-products", type=int, default=20, metavar="N", help="키워드당 최대 상품 수")
    p_run.add_argument("--min-roi", type=float, default=30.0, metavar="PCT", help="최소 ROI (%%), 기본 30")
    p_run.add_argument("--dry-run", action="store_true", help="DB 저장 없이 테스트 실행")

    # ── test-invoice ──────────────────────────────────────────────────────────
    p_inv = sub.add_parser("test-invoice", help="테스트 인보이스 PDF 생성")
    p_inv.add_argument("--output-dir", help="저장 디렉토리 (기본: data/invoices)")

    # ── weekly-status ─────────────────────────────────────────────────────────
    p_ws = sub.add_parser("weekly-status", help="이번 주 WeeklyState + 마진 요약 조회")
    p_ws.add_argument("--week-key", help="조회할 주차 (기본: 이번 주)")

    # ── list-recommendations ──────────────────────────────────────────────────
    p_lr = sub.add_parser("list-recommendations", help="박스 추천 목록 조회")
    p_lr.add_argument("--week-key", help="조회할 주차")
    p_lr.add_argument("--top", type=int, default=5, help="표시 개수")

    # ── send-reminder ─────────────────────────────────────────────────────────
    p_sr = sub.add_parser("send-reminder", help="주간 비용 입력 텔레그램 알림 즉시 발송")
    p_sr.add_argument("--week-key", help="대상 주차")

    return parser


# ══════════════════════════════════════════════════════════════════════════════
# 진입점
# ══════════════════════════════════════════════════════════════════════════════


_HANDLERS = {
    "init-db": cmd_init_db,
    "run-pipeline": cmd_run_pipeline,
    "test-invoice": cmd_test_invoice,
    "weekly-status": cmd_weekly_status,
    "list-recommendations": cmd_list_recommendations,
    "send-reminder": cmd_send_reminder,
}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    handler = _HANDLERS.get(args.command)
    if not handler:
        parser.error(f"Unknown command: {args.command}")

    sys.exit(handler(args))


if __name__ == "__main__":
    main()
