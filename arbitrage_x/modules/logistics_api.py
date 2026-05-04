"""
Arbitrage-X — Logistics Tracker (물류 추적 모듈)

UPS Tracking API + Amazon SP-API FBA Inbound 상태 통합 조회.
이슈 감지:
  - INBOUND_DELAY  : 마지막 UPS 이벤트 후 7일 이상 이동 없음
  - ACTION_REQUIRED: 아마존 FC에서 인보이스 제출 요구
  - UPS_EXCEPTION  : UPS 배송 이상 (분실, 세관 묶임 등)
  - FC_DELAYED     : FC 입고 처리 지연

Mock 클래스(MockUPSClient, MockAmazonSPClient)는 실제 API 자격증명 없이
테스트와 개발 환경에서 임의의 시나리오를 재현한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Protocol, runtime_checkable

import httpx

from config.settings import (
    SP_API_LWA_APP_ID,
    SP_API_LWA_CLIENT_SECRET,
    SP_API_MARKETPLACE_ID,
    SP_API_REFRESH_TOKEN,
    UPS_BASE_URL,
    UPS_CLIENT_ID,
    UPS_CLIENT_SECRET,
)

logger = logging.getLogger(__name__)

# 입고 지연 판단 기준일
DELAY_THRESHOLD_DAYS: int = 7

# 아마존 FC에서 인보이스 제출을 요구하는 상태 코드
_ACTION_REQUIRED_FC_STATUSES: frozenset[str] = frozenset({
    "ACTION_REQUIRED",
    "SUSPENDED",
    "CLOSED_WITH_ISSUES",
    "INVOICE_REQUIRED",
})


# ══════════════════════════════════════════════════════════════════════════════
# 이슈 도메인 타입
# ══════════════════════════════════════════════════════════════════════════════


class IssueType(str, Enum):
    INBOUND_DELAY = "INBOUND_DELAY"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    UPS_EXCEPTION = "UPS_EXCEPTION"
    FC_DELAYED = "FC_DELAYED"


@dataclass
class ShipmentIssue:
    tracking_number: str
    issue_type: IssueType
    message: str
    amazon_shipment_id: Optional[str] = None
    detected_at: datetime = field(default_factory=datetime.utcnow)
    requires_invoice: bool = False

    @property
    def urgency(self) -> str:
        """CRITICAL → 즉시 인보이스 소명 필요. WARNING → 모니터링."""
        if self.requires_invoice or self.issue_type == IssueType.ACTION_REQUIRED:
            return "CRITICAL"
        return "WARNING"


# ══════════════════════════════════════════════════════════════════════════════
# 클라이언트 인터페이스
# ══════════════════════════════════════════════════════════════════════════════


@runtime_checkable
class UPSClientProtocol(Protocol):
    def track(self, tracking_number: str) -> dict: ...


@runtime_checkable
class SPClientProtocol(Protocol):
    def get_inbound_shipment(self, shipment_id: str) -> dict: ...


# ══════════════════════════════════════════════════════════════════════════════
# Mock 클라이언트 (테스트 전용)
# ══════════════════════════════════════════════════════════════════════════════


class MockUPSClient:
    """
    결정론적 Mock UPS 클라이언트.

    stalled=True  → event_time을 8일 전으로 설정하여 지연 감지 트리거.
    status="EXCEPTION" → UPS_EXCEPTION 이슈 트리거.
    """

    def __init__(
        self,
        *,
        status: str = "IN_TRANSIT",
        last_event: str = "Package in transit",
        location: str = "Louisville, KY",
        event_time: Optional[datetime] = None,
        estimated_delivery: Optional[str] = None,
        stalled: bool = False,
    ):
        self._status = status
        self._last_event = last_event
        self._location = location
        self._event_time = event_time or (
            datetime.utcnow() - timedelta(days=8) if stalled
            else datetime.utcnow() - timedelta(hours=6)
        )
        self._estimated_delivery = estimated_delivery

    def track(self, tracking_number: str) -> dict:
        logger.debug("MockUPSClient.track(%s) → %s", tracking_number, self._status)
        return {
            "tracking_number": tracking_number,
            "status": self._status,
            "last_event": self._last_event,
            "location": self._location,
            "event_time": self._event_time.isoformat(),
            "estimated_delivery": self._estimated_delivery,
        }


class MockAmazonSPClient:
    """
    결정론적 Mock Amazon SP-API 클라이언트.

    action_required=True → "ACTION_REQUIRED" 상태를 반환하여 인보이스 요구 시나리오 재현.
    fc_delayed=True      → "FC_DELAYED" 상태.
    """

    def __init__(
        self,
        *,
        amazon_status: str = "WORKING",
        action_required: bool = False,
        fc_delayed: bool = False,
    ):
        if action_required:
            self._status = "ACTION_REQUIRED"
        elif fc_delayed:
            self._status = "FC_DELAYED"
        else:
            self._status = amazon_status

    def get_inbound_shipment(self, shipment_id: str) -> dict:
        logger.debug("MockAmazonSPClient.get_inbound_shipment(%s) → %s", shipment_id, self._status)
        fc_status_map = {
            "WORKING": "FC_RECEIVING",
            "SHIPPED": "IN_TRANSIT",
            "IN_TRANSIT": "IN_TRANSIT",
            "RECEIVING": "FC_RECEIVING",
            "CLOSED": "FC_RECEIVED",
            "DELETED": "EXCEPTION",
            "CANCELLED": "EXCEPTION",
            "ACTION_REQUIRED": "ACTION_REQUIRED",
            "FC_DELAYED": "FC_DELAYED",
            "INVOICE_REQUIRED": "ACTION_REQUIRED",
        }
        return {
            "amazon_status": self._status,
            "mapped_status": fc_status_map.get(self._status, "UNKNOWN"),
            "shipment_id": shipment_id,
            "destination_fc": "BFI4",
        }


# ══════════════════════════════════════════════════════════════════════════════
# 실제 클라이언트 (운영 환경)
# ══════════════════════════════════════════════════════════════════════════════


class UPSClient:
    """UPS OAuth2 + Track API v2."""

    TOKEN_URL = "https://onlinetools.ups.com/security/v1/oauth/token"
    TRACK_URL = f"{UPS_BASE_URL}/api/track/v1/details"

    def __init__(self):
        self._token: Optional[str] = None
        self._token_expires: Optional[datetime] = None
        self._http = httpx.Client(timeout=15)

    def _ensure_token(self) -> str:
        now = datetime.utcnow()
        if self._token and self._token_expires and now < self._token_expires:
            return self._token
        resp = self._http.post(
            self.TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(UPS_CLIENT_ID, UPS_CLIENT_SECRET),
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires = now + timedelta(seconds=int(payload.get("expires_in", 3600)) - 60)
        return self._token

    def track(self, tracking_number: str) -> dict:
        try:
            token = self._ensure_token()
            resp = self._http.get(
                f"{self.TRACK_URL}/{tracking_number}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "transId": f"arbitrage-x-{tracking_number}",
                    "transactionSrc": "ArbitrageX",
                },
            )
            resp.raise_for_status()
            return self._parse_tracking(resp.json(), tracking_number)
        except httpx.HTTPStatusError as e:
            logger.error("UPS tracking failed [%s]: %s", tracking_number, e)
            return {"status": "ERROR", "error": str(e)}
        except Exception as e:
            logger.error("UPS unexpected error [%s]: %s", tracking_number, e)
            return {"status": "ERROR", "error": str(e)}

    def _parse_tracking(self, data: dict, tracking_number: str) -> dict:
        try:
            shipment = data["trackResponse"]["shipment"][0]
            package = shipment.get("package", [{}])[0]
            activity = package.get("activity", [{}])[0]
            status_map = {
                "I": "IN_TRANSIT", "O": "OUT_FOR_DELIVERY", "D": "DELIVERED",
                "P": "PICKED_UP", "X": "EXCEPTION", "M": "PENDING",
            }
            status = status_map.get(
                activity.get("status", {}).get("statusCode", ""), "UNKNOWN"
            )
            location_data = activity.get("location", {}).get("address", {})
            location = ", ".join(filter(None, [
                location_data.get("city"),
                location_data.get("stateProvince"),
                location_data.get("countryCode"),
            ]))
            event_date = activity.get("date", "")
            event_time_str = activity.get("time", "")
            event_time = None
            if event_date and event_time_str:
                try:
                    event_time = datetime.strptime(
                        f"{event_date} {event_time_str}", "%Y%m%d %H%M%S"
                    ).isoformat()
                except ValueError:
                    pass
            return {
                "tracking_number": tracking_number,
                "status": status,
                "last_event": activity.get("status", {}).get("description", ""),
                "location": location,
                "event_time": event_time,
                "estimated_delivery": (
                    package.get("deliveryDate", [{}])[0].get("date")
                    if package.get("deliveryDate") else None
                ),
                "raw": data,
            }
        except (KeyError, IndexError) as e:
            logger.warning("Failed to parse UPS response: %s", e)
            return {"tracking_number": tracking_number, "status": "PARSE_ERROR", "raw": data}

    def close(self):
        self._http.close()


class AmazonSPClient:
    """Amazon SP-API LWA 인증 + FBA Inbound Shipment 조회."""

    LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
    SP_API_BASE = "https://sellingpartnerapi-na.amazon.com"

    def __init__(self):
        self._access_token: Optional[str] = None
        self._token_expires: Optional[datetime] = None
        self._http = httpx.Client(timeout=20)

    def _get_access_token(self) -> str:
        now = datetime.utcnow()
        if self._access_token and self._token_expires and now < self._token_expires:
            return self._access_token
        resp = self._http.post(
            self.LWA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": SP_API_REFRESH_TOKEN,
                "client_id": SP_API_LWA_APP_ID,
                "client_secret": SP_API_LWA_CLIENT_SECRET,
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        self._access_token = payload["access_token"]
        self._token_expires = datetime.utcnow() + timedelta(seconds=3500)
        return self._access_token

    def get_inbound_shipment(self, shipment_id: str) -> dict:
        try:
            token = self._get_access_token()
            resp = self._http.get(
                f"{self.SP_API_BASE}/inbound/fba/2024-03-20/inboundPlans/{shipment_id}",
                headers={
                    "x-amz-access-token": token,
                    "x-amz-marketplace-id": SP_API_MARKETPLACE_ID,
                },
            )
            resp.raise_for_status()
            return self._parse_inbound(resp.json())
        except httpx.HTTPStatusError as e:
            logger.error("SP-API inbound error [%s]: %s", shipment_id, e)
            return {"status": "ERROR", "error": str(e)}
        except Exception as e:
            logger.error("SP-API unexpected error: %s", e)
            return {"status": "ERROR", "error": str(e)}

    def _parse_inbound(self, data: dict) -> dict:
        status = data.get("status", "UNKNOWN")
        fc_status_map = {
            "WORKING": "FC_RECEIVING", "SHIPPED": "IN_TRANSIT",
            "IN_TRANSIT": "IN_TRANSIT", "RECEIVING": "FC_RECEIVING",
            "CLOSED": "FC_RECEIVED", "DELETED": "EXCEPTION",
            "CANCELLED": "EXCEPTION", "ERROR": "EXCEPTION",
        }
        return {
            "amazon_status": status,
            "mapped_status": fc_status_map.get(status.upper(), "UNKNOWN"),
            "shipment_id": data.get("inboundPlanId"),
            "destination_fc": data.get("destinationMarketplaces", [None])[0],
            "items": data.get("items", []),
            "raw": data,
        }

    def close(self):
        self._http.close()


# ══════════════════════════════════════════════════════════════════════════════
# Logistics Tracker — 이슈 감지 엔진
# ══════════════════════════════════════════════════════════════════════════════


class LogisticsTracker:
    """
    UPS + SP-API 조회를 통합하여 배송 이슈를 탐지한다.

    의존성 주입(DI) 방식으로 UPS/SP 클라이언트를 받으므로
    Mock을 주입하여 실제 API 없이 모든 이슈 시나리오를 재현할 수 있다.
    """

    def __init__(
        self,
        ups_client=None,
        sp_client=None,
    ):
        self.ups = ups_client or UPSClient()
        self.sp = sp_client or AmazonSPClient()

    # ──────────────────────────────────────────────────────────────────────────
    # 공개 API
    # ──────────────────────────────────────────────────────────────────────────

    def detect_issues(
        self,
        tracking_number: str,
        amazon_shipment_id: Optional[str] = None,
        last_event_time: Optional[datetime] = None,
    ) -> list[ShipmentIssue]:
        """
        UPS 조회 + SP-API 조회를 수행하고 감지된 이슈 목록을 반환한다.

        last_event_time: DB에 저장된 마지막 이벤트 시각
                        (None이면 UPS 응답의 event_time으로 대체)
        """
        issues: list[ShipmentIssue] = []

        # ── UPS 추적 ─────────────────────────────────────────────────────────
        ups_info = self.ups.track(tracking_number)
        if ups_info.get("status") not in ("ERROR", "PARSE_ERROR"):
            # UPS Exception
            exc = self._check_ups_exception(ups_info, tracking_number)
            if exc:
                issues.append(exc)

            # 입고 지연: UPS 이벤트 후 7일 이상 정체
            effective_event_time = last_event_time or self._parse_event_time(
                ups_info.get("event_time")
            )
            delay = self._check_inbound_delay(ups_info, tracking_number, effective_event_time)
            if delay:
                issues.append(delay)

        # ── SP-API 조회 ───────────────────────────────────────────────────────
        if amazon_shipment_id:
            sp_info = self.sp.get_inbound_shipment(amazon_shipment_id)
            if sp_info.get("mapped_status") != "ERROR":
                ar = self._check_action_required(sp_info, tracking_number, amazon_shipment_id)
                if ar:
                    issues.append(ar)
                fc = self._check_fc_delayed(sp_info, tracking_number, amazon_shipment_id)
                if fc:
                    issues.append(fc)

        if issues:
            logger.warning(
                "[TRACKER] %d issue(s) detected for tracking=%s: %s",
                len(issues),
                tracking_number,
                [i.issue_type.value for i in issues],
            )
        return issues

    # ──────────────────────────────────────────────────────────────────────────
    # 이슈 감지 헬퍼
    # ──────────────────────────────────────────────────────────────────────────

    def _check_ups_exception(
        self, ups_info: dict, tracking_number: str
    ) -> Optional[ShipmentIssue]:
        if ups_info.get("status") == "EXCEPTION":
            return ShipmentIssue(
                tracking_number=tracking_number,
                issue_type=IssueType.UPS_EXCEPTION,
                message=(
                    f"UPS 배송 이상 감지: {ups_info.get('last_event', 'Unknown exception')} "
                    f"({ups_info.get('location', '')})"
                ),
                requires_invoice=False,
            )
        return None

    def _check_inbound_delay(
        self,
        ups_info: dict,
        tracking_number: str,
        event_time: Optional[datetime],
    ) -> Optional[ShipmentIssue]:
        """마지막 이벤트 후 DELAY_THRESHOLD_DAYS일 이상 경과 + 미배송 → INBOUND_DELAY."""
        if ups_info.get("status") in ("DELIVERED", "FC_RECEIVED"):
            return None
        if event_time is None:
            return None
        elapsed = datetime.utcnow() - event_time
        if elapsed.days >= DELAY_THRESHOLD_DAYS:
            return ShipmentIssue(
                tracking_number=tracking_number,
                issue_type=IssueType.INBOUND_DELAY,
                message=(
                    f"배송 정체 {elapsed.days}일 경과 — "
                    f"마지막 이벤트: {event_time.strftime('%Y-%m-%d')} "
                    f"({ups_info.get('location', '위치 미상')})"
                ),
                requires_invoice=True,
            )
        return None

    def _check_action_required(
        self,
        sp_info: dict,
        tracking_number: str,
        amazon_shipment_id: str,
    ) -> Optional[ShipmentIssue]:
        """아마존 FC에서 ACTION_REQUIRED / 인보이스 제출 요구 감지."""
        status = (sp_info.get("amazon_status") or "").upper()
        mapped = (sp_info.get("mapped_status") or "").upper()
        if status in _ACTION_REQUIRED_FC_STATUSES or mapped == "ACTION_REQUIRED":
            return ShipmentIssue(
                tracking_number=tracking_number,
                amazon_shipment_id=amazon_shipment_id,
                issue_type=IssueType.ACTION_REQUIRED,
                message=(
                    f"아마존 FC 인보이스 제출 요구 — "
                    f"Shipment ID: {amazon_shipment_id}, Status: {status}"
                ),
                requires_invoice=True,
            )
        return None

    def _check_fc_delayed(
        self,
        sp_info: dict,
        tracking_number: str,
        amazon_shipment_id: str,
    ) -> Optional[ShipmentIssue]:
        """FC 입고 처리 지연 감지."""
        if sp_info.get("mapped_status") == "FC_DELAYED":
            return ShipmentIssue(
                tracking_number=tracking_number,
                amazon_shipment_id=amazon_shipment_id,
                issue_type=IssueType.FC_DELAYED,
                message=f"FC 입고 처리 지연 — Shipment ID: {amazon_shipment_id}",
                requires_invoice=False,
            )
        return None

    @staticmethod
    def _parse_event_time(event_time_str: Optional[str]) -> Optional[datetime]:
        if not event_time_str:
            return None
        try:
            return datetime.fromisoformat(event_time_str)
        except ValueError:
            return None

    def close(self):
        if hasattr(self.ups, "close"):
            self.ups.close()
        if hasattr(self.sp, "close"):
            self.sp.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
