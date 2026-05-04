"""
Arbitrage-X — Background Scheduler
APScheduler 기반 주기적 작업:
  - 매주 월요일 09:00 KST: 주간 비용 입력 텔레그램 알림
  - 매 30분: 활성 Shipment 추적 갱신 + 이슈 알림
  - 매 10분: 물류 이슈 탐지
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from arbitrage_x.db.database import get_db
from arbitrage_x.db.models import Shipment, ShipmentStatus
from arbitrage_x.modules.logistics_api import LogisticsTracker
from arbitrage_x.utils.notifier import AlertLevel, TelegramNotifier
from arbitrage_x.utils.week_utils import get_current_week_key

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


def job_weekly_reminder():
    """매주 월요일 09:00 KST — 주간 비용 입력 알림."""
    week_key = get_current_week_key()
    with TelegramNotifier() as notifier:
        sent = notifier.send_weekly_reminder(week_key)
        if sent:
            logger.info("Weekly reminder sent for %s", week_key)


def job_track_shipments():
    """활성 Shipment를 순회하며 추적 상태를 갱신한다."""
    alert_statuses = {ShipmentStatus.EXCEPTION.value, ShipmentStatus.FC_DELAYED.value}

    with get_db() as db:
        active_shipments = (
            db.query(Shipment)
            .filter(
                Shipment.status.notin_([
                    ShipmentStatus.FC_RECEIVED.value,
                    ShipmentStatus.EXCEPTION.value,
                ])
            )
            .all()
        )

        if not active_shipments:
            return

        logger.info("Tracking %d active shipments", len(active_shipments))

        with LogisticsTracker() as tracker:
            with TelegramNotifier() as notifier:
                for shipment in active_shipments:
                    try:
                        updates, should_alert = tracker.refresh_shipment(shipment)

                        for field, value in updates.items():
                            setattr(shipment, field, value)

                        if should_alert and not shipment.alert_sent:
                            notifier.send_shipment_alert(
                                shipment.tracking_number,
                                updates.get("alert_message", "배송 이슈 발생"),
                            )
                            shipment.alert_sent = True

                        logger.debug(
                            "Shipment %s updated: %s",
                            shipment.tracking_number,
                            updates.get("status"),
                        )
                    except Exception as e:
                        logger.error(
                            "Error tracking shipment %s: %s",
                            shipment.tracking_number, e,
                        )


def create_scheduler() -> BackgroundScheduler:
    """스케줄러를 생성하고 작업을 등록한다."""
    scheduler = BackgroundScheduler(timezone=KST)

    # 매주 월요일 09:00 KST
    scheduler.add_job(
        job_weekly_reminder,
        trigger=CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=KST),
        id="weekly_reminder",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # 매 30분 배송 추적
    scheduler.add_job(
        job_track_shipments,
        trigger=IntervalTrigger(minutes=30),
        id="track_shipments",
        replace_existing=True,
        max_instances=1,
    )

    return scheduler
