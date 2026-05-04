"""
Arbitrage-X — Notifier
Telegram Bot을 통한 알림 발송.
환경 변수 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 필요.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

import httpx

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class AlertLevel(str, Enum):
    INFO = "ℹ️"
    WARNING = "⚠️"
    CRITICAL = "🚨"


class TelegramNotifier:
    """
    사용 예:
        notifier = TelegramNotifier()
        notifier.send("주간 비용 입력이 필요합니다.", level=AlertLevel.WARNING)
    """

    def __init__(
        self,
        token: Optional[str] = None,
        chat_id: Optional[str] = None,
    ):
        self.token = token or TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or TELEGRAM_CHAT_ID
        self._http = httpx.Client(timeout=10)

    def send(self, message: str, level: AlertLevel = AlertLevel.INFO) -> bool:
        """메시지를 Telegram으로 발송한다. 성공 시 True."""
        if not self.token or not self.chat_id:
            logger.warning(
                "Telegram not configured (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing). "
                "Message: %s", message
            )
            return False

        text = f"{level.value} *[Arbitrage-X]*\n{message}"
        url = TELEGRAM_API.format(token=self.token)
        try:
            resp = self._http.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                },
            )
            resp.raise_for_status()
            logger.info("Telegram notification sent: %s", message[:60])
            return True
        except httpx.HTTPStatusError as e:
            logger.error("Telegram send failed: %s", e)
            return False
        except Exception as e:
            logger.error("Telegram unexpected error: %s", e)
            return False

    def send_weekly_reminder(self, week_key: str) -> bool:
        return self.send(
            f"📅 *주간 비용 입력 요청*\n\n"
            f"새 주차 *{week_key}* 가 시작되었습니다.\n"
            f"배송비, 관세율 등 부대비용을 입력해 주세요.\n\n"
            f"👉 `/api/v1/weekly-state/create` 엔드포인트 또는 대시보드에서 입력 가능합니다.",
            level=AlertLevel.WARNING,
        )

    def send_shipment_alert(self, tracking_number: str, message: str) -> bool:
        return self.send(
            f"*배송 이슈 발생*\n\n"
            f"트래킹 번호: `{tracking_number}`\n"
            f"내용: {message}",
            level=AlertLevel.CRITICAL,
        )

    def send_ip_risk_alert(self, asin: str, brand: str, message: str) -> bool:
        return self.send(
            f"*IP 리스크 감지*\n\n"
            f"ASIN: `{asin}`\n"
            f"브랜드: `{brand}`\n"
            f"내용: {message}",
            level=AlertLevel.WARNING,
        )

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
