"""
Arbitrage-X — Risk Shield 단위 테스트

시나리오:
  A. SAFE                  — 상표 미등록 + 아마존 미판매
  B. IP_RISK_HIGH          — USPTO LIVE 상표 등록 + 아마존 미판매
  C. IP_RISK_MEDIUM        — USPTO 비-LIVE 상표 등록 + 아마존 미판매
  D. AMAZON_SELLING        — 상표 미등록 + 바이박스 아마존 직접 판매
  E. BLOCKED               — LIVE 상표 등록 + 아마존 직접 판매 동시 해당
  F. 빈 브랜드             — brand=None/""  → SAFE (USPTO 조회 생략)
  G. 단일 문자 브랜드      — brand="A"     → SAFE (조회 생략)
  H. Amazon 셀러 대소문자  — "AMAZON.COM", "amazon" 모두 감지
  I. all_sellers 목록 검사 — buy_box_seller=None이어도 all_sellers에 Amazon 있으면 감지
  J. USPTO API 실패        — fail-open → SAFE (예외 미전파)
  K. USPTO 재시도(429)     — RetryClient 지수 백오프 검증
  L. 재시도 소진           — HTTPStatusError → fail-open으로 처리 (SAFE)
  M. can_proceed_to_margin — SAFE만 True, 나머지 모두 False
  N. batch_assess          — 여러 상품 일괄 평가
  O. filter_safe           — SAFE 상품만 필터링
  P. RiskStatus.blocks_pipeline — SAFE 이외 전부 True
  Q. USPTOClientProtocol   — MockUSPTOClient가 Protocol을 충족
  R. USPTOOpenDataClient   — RetryClient 설정 검증
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arbitrage_x.ingestion.base import RetryClient
from arbitrage_x.modules.risk_manager import (
    MockUSPTOClient,
    RiskAssessment,
    RiskInput,
    RiskShield,
    RiskStatus,
    TrademarkResult,
    USPTOClientProtocol,
    USPTOOpenDataClient,
)


# ──────────────────────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────────────────────


def _make_input(
    asin: str = "B0TEST001",
    brand: Optional[str] = "TestBrand",
    buy_box_seller: Optional[str] = None,
    all_sellers: Optional[list[str]] = None,
) -> RiskInput:
    return RiskInput(
        asin=asin,
        brand=brand,
        buy_box_seller=buy_box_seller,
        all_sellers=all_sellers,
    )


class _FailingUSPTOClient:
    """매 호출마다 예외를 던지는 Mock."""
    def search_trademark(self, brand: str) -> TrademarkResult:
        raise httpx.ConnectTimeout("USPTO API simulated timeout")


# ──────────────────────────────────────────────────────────────────────────────
# A. SAFE
# ──────────────────────────────────────────────────────────────────────────────


def test_safe_no_ip_risk_no_amazon():
    """상표 미등록 + 아마존 미판매 → SAFE."""
    shield = RiskShield(MockUSPTOClient(registered=False))
    result = shield.assess(_make_input())

    assert result.status == RiskStatus.SAFE
    assert result.is_safe is True
    assert result.can_proceed_to_margin() is True
    assert result.uspto_registered is False
    assert result.sold_by_amazon is False
    assert result.warnings == []


# ──────────────────────────────────────────────────────────────────────────────
# B. IP_RISK_HIGH
# ──────────────────────────────────────────────────────────────────────────────


def test_ip_risk_high_live_trademark():
    """USPTO LIVE 상표 등록 → IP_RISK_HIGH."""
    shield = RiskShield(MockUSPTOClient(registered=True, live=True))
    result = shield.assess(_make_input(brand="Nike"))

    assert result.status == RiskStatus.IP_RISK_HIGH
    assert result.is_safe is False
    assert result.can_proceed_to_margin() is False
    assert result.uspto_registered is True
    assert len(result.warnings) == 1
    assert "USPTO 등록 상표" in result.warnings[0]


def test_ip_risk_high_contains_serial_and_owner():
    """IP_RISK_HIGH 결과에 serial_number와 owner가 채워져 있어야 한다."""
    shield = RiskShield(MockUSPTOClient(
        registered=True, live=True,
        serial_number="88888888", owner="Nike Inc.",
    ))
    result = shield.assess(_make_input(brand="Nike"))

    assert result.trademark_serial == "88888888"
    assert result.trademark_owner == "Nike Inc."


# ──────────────────────────────────────────────────────────────────────────────
# C. IP_RISK_MEDIUM
# ──────────────────────────────────────────────────────────────────────────────


def test_ip_risk_medium_non_live_trademark():
    """USPTO 등록 상태이나 LIVE 아닌 상태 → IP_RISK_MEDIUM."""
    shield = RiskShield(MockUSPTOClient(
        registered=True, live=False, status_description="PENDING CANCELLATION"
    ))
    result = shield.assess(_make_input(brand="Acme"))

    assert result.status == RiskStatus.IP_RISK_MEDIUM
    assert result.is_safe is False
    assert result.uspto_registered is True


# ──────────────────────────────────────────────────────────────────────────────
# D. AMAZON_SELLING
# ──────────────────────────────────────────────────────────────────────────────


def test_amazon_selling_buybox():
    """바이박스 셀러가 'Amazon.com' → AMAZON_SELLING."""
    shield = RiskShield(MockUSPTOClient(registered=False))
    result = shield.assess(_make_input(buy_box_seller="Amazon.com"))

    assert result.status == RiskStatus.AMAZON_SELLING
    assert result.is_safe is False
    assert result.sold_by_amazon is True
    assert "바이박스" in result.warnings[0]


# ──────────────────────────────────────────────────────────────────────────────
# E. BLOCKED
# ──────────────────────────────────────────────────────────────────────────────


def test_blocked_ip_and_amazon_selling():
    """LIVE 상표 + 아마존 직접 판매 → BLOCKED."""
    shield = RiskShield(MockUSPTOClient(registered=True, live=True))
    result = shield.assess(_make_input(buy_box_seller="Amazon.com"))

    assert result.status == RiskStatus.BLOCKED
    assert result.is_safe is False
    assert result.uspto_registered is True
    assert result.sold_by_amazon is True
    assert len(result.warnings) == 2


# ──────────────────────────────────────────────────────────────────────────────
# F. 빈 브랜드 → USPTO 조회 생략
# ──────────────────────────────────────────────────────────────────────────────


def test_none_brand_skips_uspto():
    """brand=None이면 USPTO 조회를 생략하고 SAFE를 반환한다."""
    shield = RiskShield(MockUSPTOClient(registered=True, live=True))  # 조회 시 HIGH 반환할 설정
    result = shield.assess(_make_input(brand=None))

    assert result.status == RiskStatus.SAFE
    assert result.uspto_registered is False


def test_empty_string_brand_skips_uspto():
    """brand=""이면 USPTO 조회를 생략한다."""
    shield = RiskShield(MockUSPTOClient(registered=True, live=True))
    result = shield.assess(_make_input(brand=""))

    assert result.status == RiskStatus.SAFE


# ──────────────────────────────────────────────────────────────────────────────
# G. 단일 문자 브랜드 → 조회 생략
# ──────────────────────────────────────────────────────────────────────────────


def test_single_char_brand_skips_uspto():
    """brand가 1글자이면 USPTO 조회를 생략한다."""
    shield = RiskShield(MockUSPTOClient(registered=True, live=True))
    result = shield.assess(_make_input(brand="A"))

    assert result.status == RiskStatus.SAFE


# ──────────────────────────────────────────────────────────────────────────────
# H. Amazon 셀러 대소문자 무관 감지
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("seller", [
    "Amazon.com",
    "amazon.com",
    "AMAZON.COM",
    "Amazon",
    "AMAZON",
    "Sold by Amazon.com",
    "Ships from Amazon",
])
def test_amazon_seller_case_insensitive(seller):
    """Amazon 셀러 감지는 대소문자를 가리지 않아야 한다."""
    shield = RiskShield(MockUSPTOClient(registered=False))
    result = shield.assess(_make_input(buy_box_seller=seller))

    assert result.sold_by_amazon is True
    assert result.status == RiskStatus.AMAZON_SELLING


def test_non_amazon_seller_is_safe():
    """아마존이 아닌 셀러 → AMAZON_SELLING 아님."""
    shield = RiskShield(MockUSPTOClient(registered=False))
    result = shield.assess(_make_input(buy_box_seller="Third Party Seller LLC"))

    assert result.sold_by_amazon is False
    assert result.status == RiskStatus.SAFE


# ──────────────────────────────────────────────────────────────────────────────
# I. all_sellers 목록 검사
# ──────────────────────────────────────────────────────────────────────────────


def test_amazon_detected_in_all_sellers_list():
    """buy_box_seller가 없어도 all_sellers에 Amazon이 있으면 감지한다."""
    shield = RiskShield(MockUSPTOClient(registered=False))
    result = shield.assess(_make_input(
        buy_box_seller=None,
        all_sellers=["Third Party A", "Amazon.com", "Third Party B"],
    ))

    assert result.sold_by_amazon is True
    assert result.status == RiskStatus.AMAZON_SELLING


def test_no_sellers_provided_is_safe():
    """buy_box_seller와 all_sellers 모두 없으면 AMAZON_SELLING 아님."""
    shield = RiskShield(MockUSPTOClient(registered=False))
    result = shield.assess(_make_input(buy_box_seller=None, all_sellers=None))

    assert result.sold_by_amazon is False


# ──────────────────────────────────────────────────────────────────────────────
# J. USPTO API 실패 → fail-open (SAFE 반환, 예외 미전파)
# ──────────────────────────────────────────────────────────────────────────────


def test_uspto_api_failure_fail_open():
    """USPTO API가 실패해도 예외가 전파되지 않고 IP 체크는 SAFE로 처리된다."""
    shield = RiskShield(_FailingUSPTOClient())
    result = shield.assess(_make_input(brand="SomeBrand"))

    assert result.status == RiskStatus.SAFE
    assert result.uspto_registered is False


def test_uspto_api_failure_with_amazon_seller():
    """USPTO 실패 + 아마존 판매 → IP는 SAFE, 전체는 AMAZON_SELLING."""
    shield = RiskShield(_FailingUSPTOClient())
    result = shield.assess(_make_input(brand="SomeBrand", buy_box_seller="Amazon.com"))

    assert result.status == RiskStatus.AMAZON_SELLING
    assert result.uspto_registered is False
    assert result.sold_by_amazon is True


# ──────────────────────────────────────────────────────────────────────────────
# K. USPTO 재시도(429) — RetryClient 지수 백오프 검증
# ──────────────────────────────────────────────────────────────────────────────


@patch("time.sleep")
def test_uspto_client_retries_on_429(mock_sleep):
    """429 응답 2회 후 정상 응답 → RetryClient 지수 백오프 2회 수행 후 성공."""
    client = USPTOOpenDataClient(max_retries=2)
    _req = httpx.Request("GET", USPTOOpenDataClient._SEARCH_URL)

    call_count = 0

    def fake_request(method, url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            resp = httpx.Response(429, headers={"Retry-After": "0.01"})
            resp.request = _req
            return resp
        resp = httpx.Response(200, json={"response": {"docs": []}})
        resp.request = _req
        return resp

    with patch.object(client._client._http, "request", fake_request):
        result = client.search_trademark("TestBrand")

    assert call_count == 3  # 초기 1회 + 재시도 2회
    assert mock_sleep.call_count == 2
    assert result.registered is False  # docs 빈 배열 → 미등록


# ──────────────────────────────────────────────────────────────────────────────
# L. 재시도 소진 → fail-open으로 처리 (SAFE)
# ──────────────────────────────────────────────────────────────────────────────


@patch("time.sleep")
def test_uspto_retries_exhausted_fail_open(mock_sleep):
    """재시도가 모두 소진되면 예외를 삼키고 registered=False를 반환한다 (fail-open)."""
    client = USPTOOpenDataClient(max_retries=1)
    _req = httpx.Request("GET", USPTOOpenDataClient._SEARCH_URL)

    def always_429(method, url, **kwargs):
        resp = httpx.Response(429)
        resp.request = _req
        return resp

    with patch.object(client._client._http, "request", always_429):
        result = client.search_trademark("AnyBrand")

    assert result.registered is False
    assert mock_sleep.call_count == 1


@patch("time.sleep")
def test_risk_shield_with_exhausted_uspto_still_safe(mock_sleep):
    """RiskShield가 USPTOOpenDataClient를 사용하고 재시도 소진 시에도 SAFE를 반환한다."""
    client = USPTOOpenDataClient(max_retries=1)
    shield = RiskShield(client)
    _req = httpx.Request("GET", USPTOOpenDataClient._SEARCH_URL)

    def always_429(method, url, **kwargs):
        resp = httpx.Response(429)
        resp.request = _req
        return resp

    with patch.object(client._client._http, "request", always_429):
        result = shield.assess(_make_input(brand="SomeBrand"))

    assert result.status == RiskStatus.SAFE


# ──────────────────────────────────────────────────────────────────────────────
# M. can_proceed_to_margin 파이프라인 게이트
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status,expected", [
    (RiskStatus.SAFE, True),
    (RiskStatus.IP_RISK_HIGH, False),
    (RiskStatus.IP_RISK_MEDIUM, False),
    (RiskStatus.AMAZON_SELLING, False),
    (RiskStatus.BLOCKED, False),
])
def test_can_proceed_to_margin(status, expected):
    """SAFE만 마진 계산기로 이관 가능해야 한다."""
    assessment = RiskAssessment(
        asin="B0TEST",
        brand="Test",
        status=status,
        uspto_registered=False,
        trademark_serial=None,
        trademark_owner=None,
        trademark_status=None,
        sold_by_amazon=False,
    )
    assert assessment.can_proceed_to_margin() is expected


# ──────────────────────────────────────────────────────────────────────────────
# N. batch_assess
# ──────────────────────────────────────────────────────────────────────────────


def test_batch_assess_returns_all_results():
    """batch_assess는 모든 상품 결과를 반환한다 (차단 상품 포함)."""
    shield = RiskShield(MockUSPTOClient(registered=False))
    items = [
        _make_input(asin="B001", buy_box_seller=None),          # SAFE
        _make_input(asin="B002", buy_box_seller="Amazon.com"),  # AMAZON_SELLING
        _make_input(asin="B003", buy_box_seller=None),          # SAFE
    ]
    results = shield.batch_assess(items)

    assert len(results) == 3
    statuses = {r.asin: r.status for r in results}
    assert statuses["B001"] == RiskStatus.SAFE
    assert statuses["B002"] == RiskStatus.AMAZON_SELLING
    assert statuses["B003"] == RiskStatus.SAFE


def test_batch_assess_mixed_ip_and_amazon():
    """배치에서 IP 리스크, Amazon 판매, SAFE 상품이 혼합된 경우."""
    shield_registered = RiskShield(MockUSPTOClient(registered=True, live=True))

    items = [
        RiskInput(asin="B001", brand="Nike", buy_box_seller=None),        # IP_RISK_HIGH
        RiskInput(asin="B002", brand="Nike", buy_box_seller="Amazon.com"), # BLOCKED
        RiskInput(asin="B003", brand=None,   buy_box_seller="Amazon.com"), # AMAZON_SELLING
    ]
    results = shield_registered.batch_assess(items)

    statuses = {r.asin: r.status for r in results}
    assert statuses["B001"] == RiskStatus.IP_RISK_HIGH
    assert statuses["B002"] == RiskStatus.BLOCKED
    assert statuses["B003"] == RiskStatus.AMAZON_SELLING


# ──────────────────────────────────────────────────────────────────────────────
# O. filter_safe
# ──────────────────────────────────────────────────────────────────────────────


def test_filter_safe_returns_only_safe():
    """filter_safe는 SAFE 상품만 반환한다."""
    shield = RiskShield(MockUSPTOClient(registered=False))
    items = [
        _make_input(asin="B001", buy_box_seller=None),          # SAFE
        _make_input(asin="B002", buy_box_seller="Amazon.com"),  # AMAZON_SELLING
        _make_input(asin="B003", buy_box_seller=None),          # SAFE
    ]
    safe_results = shield.filter_safe(items)

    assert len(safe_results) == 2
    assert all(r.is_safe for r in safe_results)
    asins = [r.asin for r in safe_results]
    assert "B001" in asins
    assert "B003" in asins
    assert "B002" not in asins


def test_filter_safe_empty_when_all_blocked():
    """모든 상품이 차단 상태이면 빈 리스트를 반환한다."""
    shield = RiskShield(MockUSPTOClient(registered=True, live=True))
    items = [
        _make_input(asin="B001", buy_box_seller="Amazon.com"),
        _make_input(asin="B002", buy_box_seller=None),
    ]
    safe_results = shield.filter_safe(items)

    assert safe_results == []


# ──────────────────────────────────────────────────────────────────────────────
# P. RiskStatus.blocks_pipeline
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status,blocks", [
    (RiskStatus.SAFE, False),
    (RiskStatus.IP_RISK_HIGH, True),
    (RiskStatus.IP_RISK_MEDIUM, True),
    (RiskStatus.AMAZON_SELLING, True),
    (RiskStatus.BLOCKED, True),
])
def test_risk_status_blocks_pipeline(status, blocks):
    assert status.blocks_pipeline is blocks


# ──────────────────────────────────────────────────────────────────────────────
# Q. MockUSPTOClient가 USPTOClientProtocol을 충족
# ──────────────────────────────────────────────────────────────────────────────


def test_mock_client_implements_protocol():
    assert isinstance(MockUSPTOClient(), USPTOClientProtocol)


def test_failing_client_implements_protocol():
    assert isinstance(_FailingUSPTOClient(), USPTOClientProtocol)


# ──────────────────────────────────────────────────────────────────────────────
# R. USPTOOpenDataClient — RetryClient 설정 검증
# ──────────────────────────────────────────────────────────────────────────────


def test_uspto_open_data_client_uses_retry_client():
    """USPTOOpenDataClient가 RetryClient를 올바른 파라미터로 초기화한다."""
    client = USPTOOpenDataClient(max_retries=5, base_delay=2.0)
    assert isinstance(client._client, RetryClient)
    assert client._client.max_retries == 5
    assert client._client.base_delay == 2.0


# ──────────────────────────────────────────────────────────────────────────────
# 추가: RiskShield 생성자 타입 검증
# ──────────────────────────────────────────────────────────────────────────────


def test_risk_shield_rejects_non_protocol_client():
    """Protocol을 구현하지 않은 객체를 넘기면 TypeError."""
    class NotAClient:
        pass

    with pytest.raises(TypeError):
        RiskShield(NotAClient())
