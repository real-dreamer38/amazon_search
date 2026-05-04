"""
Arbitrage-X — Background Daemon

APScheduler 기반 자동 실행 데몬.

스케줄:
  매일 02:00 KST  — 통합 파이프라인 전체 실행
  매주 월 09:00 KST — 주간 비용 입력 텔레그램 알림 (기존 job_weekly_reminder)
  매 30분         — 활성 Shipment 추적 갱신 (기존 job_track_shipments)

실행:
  python run_daemon.py
  python run_daemon.py --run-now   # 시작 즉시 파이프라인 1회 실행 후 대기
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import timezone, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from arbitrage_x.core.orchestrator import ArbitrageOrchestrator, PipelineConfig
from arbitrage_x.core.scheduler import job_track_shipments, job_weekly_reminder
from arbitrage_x.db.database import init_db
from arbitrage_x.utils.week_utils import get_current_week_key

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("daemon")

KST = timezone(timedelta(hours=9))


# ══════════════════════════════════════════════════════════════════════════════
# 스케줄 작업 함수
# ══════════════════════════════════════════════════════════════════════════════


def job_run_pipeline() -> None:
    """매일 02:00 KST — 통합 파이프라인 실행."""
    logger.info("[SCHEDULER] Daily pipeline job started")
    try:
        config = PipelineConfig(
            week_key=get_current_week_key(),
            keywords=["electronics", "beauty", "sports"],
            max_products_per_keyword=20,
            min_roi=0.30,
        )
        orchestrator = ArbitrageOrchestrator()
        result = orchestrator.run(config)
        logger.info("[SCHEDULER] Pipeline done — eligible=%d", result.margin_eligible)
    except Exception as e:
        logger.exception("[SCHEDULER] Pipeline job failed: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# 스케줄러 빌더
# ══════════════════════════════════════════════════════════════════════════════


def build_scheduler() -> BackgroundScheduler:
    """모든 스케줄 작업을 등록한 스케줄러를 반환한다."""
    scheduler = BackgroundScheduler(timezone=KST)

    # ── 매일 02:00 KST — 전체 파이프라인 ───────────────────────────────────
    scheduler.add_job(
        job_run_pipeline,
        trigger=CronTrigger(hour=2, minute=0, timezone=KST),
        id="daily_pipeline",
        replace_existing=True,
        misfire_grace_time=3600,
        max_instances=1,
    )

    # ── 매주 월요일 09:00 KST — 주간 비용 입력 알림 ─────────────────────────
    scheduler.add_job(
        job_weekly_reminder,
        trigger=CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=KST),
        id="weekly_reminder",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # ── 매 30분 — Shipment 추적 갱신 ─────────────────────────────────────────
    scheduler.add_job(
        job_track_shipments,
        trigger=IntervalTrigger(minutes=30),
        id="track_shipments",
        replace_existing=True,
        max_instances=1,
    )

    return scheduler


# ══════════════════════════════════════════════════════════════════════════════
# 진입점
# ══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(description="Arbitrage-X Background Daemon")
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="시작 즉시 파이프라인을 1회 실행한 뒤 스케줄 대기",
    )
    args = parser.parse_args()

    logger.info("=== Arbitrage-X Daemon starting ===")

    # DB 초기화 (테이블 없으면 생성)
    init_db()
    logger.info("DB initialized")

    scheduler = build_scheduler()
    scheduler.start()
    logger.info("Scheduler started — jobs: %s", [j.id for j in scheduler.get_jobs()])

    # Ctrl+C / SIGTERM 처리
    stop_event = {"flag": False}

    def _shutdown(signum, frame):
        logger.info("Shutdown signal received (%s)", signum)
        stop_event["flag"] = True

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if args.run_now:
        logger.info("--run-now: executing pipeline immediately")
        job_run_pipeline()

    logger.info("Daemon running. Press Ctrl+C to stop.")
    while not stop_event["flag"]:
        time.sleep(5)

    scheduler.shutdown(wait=False)
    logger.info("=== Arbitrage-X Daemon stopped ===")


if __name__ == "__main__":
    main()
