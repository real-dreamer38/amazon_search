"""
Arbitrage-X — Logistics API
UPS Tracking + Amazon SP-API FBA Inbound 연동
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

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


# ══════════════════════════════════════════════════════════════════════════════
# UPS Tracking
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
        expires_in = int(payload.get("expires_in", 3600))
        from datetime import timedelta
        self._token_expires = now + timedelta(seconds=expires_in - 60)
        return self._token

    def track(self, tracking_number: str) -> dict:
        """
        단일 트래킹 번호의 현재 상태를 조회한다.
        반환값:
            {
                "status": "IN_TRANSIT",
                "last_event": "Departed facility",
                "location": "Louisville, KY",
                "event_time": "2026-05-03T10:30:00",
                "estimated_delivery": "2026-05-05",
                "raw": {...},
            }
        """
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
            data = resp.json()
            return self._parse_tracking(data, tracking_number)

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

            status_code = (
                activity.get("status", {}).get("statusCode", "")
            )
            status_map = {
                "I": "IN_TRANSIT",
                "O": "OUT_FOR_DELIVERY",
                "D": "DELIVERED",
                "P": "PICKED_UP",
                "X": "EXCEPTION",
                "M": "PENDING",
            }
            status = status_map.get(status_code, "UNKNOWN")

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

            delivery_date = (
                package.get("deliveryDate", [{}])[0].get("date")
                if package.get("deliveryDate") else None
            )

            return {
                "tracking_number": tracking_number,
                "status": status,
                "last_event": activity.get("status", {}).get("description", ""),
                "location": location,
                "event_time": event_time,
                "estimated_delivery": delivery_date,
                "raw": data,
            }
        except (KeyError, IndexError) as e:
            logger.warning("Failed to parse UPS response: %s", e)
            return {"tracking_number": tracking_number, "status": "PARSE_ERROR", "raw": data}

    def close(self):
        self._http.close()


# ══════════════════════════════════════════════════════════════════════════════
# Amazon SP-API — FBA Inbound
# ══════════════════════════════════════════════════════════════════════════════

class AmazonSPClient:
    """
    Amazon SP-API LWA(Login with Amazon) 인증 + FBA Inbound Shipment 조회.
    실운영 시 sp-api-python 라이브러리 사용 권장.
    """

    LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
    SP_API_BASE = "https://sellingpartnerapi-na.amazon.com"

    def __init__(self):
        self._access_token: Optional[str] = None
        self._token_expires: Optional[datetime] = None
        self._http = httpx.Client(timeout=20)

    def _get_access_token(self) -> str:
        from datetime import timedelta
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
        self._token_expires = now + timedelta(seconds=3500)
        return self._access_token

    def get_inbound_shipment(self, shipment_id: str) -> dict:
        """
        FBA 입고 배송 현황 조회.
        https://developer-docs.amazon.com/sp-api/docs/fulfillment-inbound-api-v2024-03-20
        """
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
            "WORKING": "FC_RECEIVING",
            "SHIPPED": "IN_TRANSIT",
            "IN_TRANSIT": "IN_TRANSIT",
            "RECEIVING": "FC_RECEIVING",
            "CLOSED": "FC_RECEIVED",
            "DELETED": "EXCEPTION",
            "CANCELLED": "EXCEPTION",
            "ERROR": "EXCEPTION",
        }
        return {
            "amazon_status": status,
            "mapped_status": fc_status_map.get(status.upper(), "UNKNOWN"),
            "shipment_id": data.get("inboundPlanId"),
            "destination_fc": data.get("destinationMarketplaces", [None])[0],
            "items": data.get("items", []),
            "raw": data,
        }

    def list_inbound_shipments(self, status_filter: str = "WORKING") -> list[dict]:
        """활성 입고 배송 목록 조회."""
        try:
            token = self._get_access_token()
            resp = self._http.get(
                f"{self.SP_API_BASE}/inbound/fba/2024-03-20/inboundPlans",
                params={"status": status_filter},
                headers={"x-amz-access-token": token},
            )
            resp.raise_for_status()
            plans = resp.json().get("inboundPlans", [])
            return [self._parse_inbound(p) for p in plans]
        except Exception as e:
            logger.error("SP-API list_inbound_shipments error: %s", e)
            return []

    def close(self):
        self._http.close()


# ══════════════════════════════════════════════════════════════════════════════
# Logistics Tracker — UPS + SP-API 통합 조율
# ══════════════════════════════════════════════════════════════════════════════

class LogisticsTracker:
    """
    DB의 Shipment 레코드를 순회하며 UPS + SP-API로 상태를 갱신하고
    이슈 발생 시 알림 트리거를 반환한다.
    """

    ALERT_STATUSES = {"EXCEPTION", "FC_DELAYED"}

    def __init__(self):
        self.ups = UPSClient()
        self.sp = AmazonSPClient()

    def refresh_shipment(self, shipment) -> tuple[dict, bool]:
        """
        단일 Shipment ORM 객체를 갱신한다.
        반환: (updated_fields_dict, should_alert)
        """
        updates: dict = {"last_checked_at": datetime.utcnow()}
        should_alert = False

        # UPS 추적
        if shipment.tracking_number and shipment.carrier == "UPS":
            result = self.ups.track(shipment.tracking_number)
            if result.get("status") not in ("ERROR", "PARSE_ERROR"):
                ups_status = result["status"]
                updates["last_event"] = result.get("last_event")

                if ups_status == "DELIVERED" and not shipment.actual_delivery:
                    updates["actual_delivery"] = datetime.utcnow()

                # SP-API로 FC 입고 상태 확인
                if ups_status == "DELIVERED" and shipment.amazon_shipment_id:
                    sp_result = self.sp.get_inbound_shipment(shipment.amazon_shipment_id)
                    mapped = sp_result.get("mapped_status", "UNKNOWN")
                    updates["status"] = mapped
                    if mapped == "FC_DELAYED":
                        should_alert = True
                        updates["alert_message"] = (
                            f"FC 입고 지연: {shipment.amazon_shipment_id}"
                        )
                else:
                    updates["status"] = ups_status

                if ups_status == "EXCEPTION":
                    should_alert = True
                    updates["alert_message"] = (
                        f"UPS 배송 이슈: {shipment.tracking_number} — "
                        f"{result.get('last_event', 'Unknown exception')}"
                    )

        return updates, should_alert

    def close(self):
        self.ups.close()
        self.sp.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
