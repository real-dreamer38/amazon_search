"""
Arbitrage-X — Resolution Hub + Logistics Tracker 테스트

범위:
  1. InvoiceGenerator — PDF 실제 생성 / 파일 크기 / ASIN 포함 여부
  2. InvoiceData 산술 — subtotal / tax / total
  3. InvoiceGenerator.build_invoice_number — 형식 검증
  4. LogisticsTracker — 7일 지연, ACTION_REQUIRED, UPS_EXCEPTION, FC_DELAYED, 정상
  5. TelegramNotifier.send_invoice_ready — 메시지 내용 / 절대경로 포함 여부
  6. 통합: 이슈 감지 → 인보이스 생성 → Telegram 알림
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from arbitrage_x.modules.invoice_generator import (
    InvoiceData,
    InvoiceGenerator,
    LineItem,
)
from arbitrage_x.modules.logistics_api import (
    IssueType,
    LogisticsTracker,
    MockAmazonSPClient,
    MockUPSClient,
    ShipmentIssue,
)
from arbitrage_x.utils.notifier import AlertLevel, TelegramNotifier


# ══════════════════════════════════════════════════════════════════════════════
# 공통 픽스처
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def sample_invoice_data() -> InvoiceData:
    return InvoiceData(
        invoice_number="INV-2026W19-0001",
        issued_date=datetime(2026, 5, 4),
        purpose="Amazon IP Dispute",
        buyer_name="Amazon.com Services LLC",
        buyer_address="410 Terry Ave N\nSeattle, WA 98109",
        ship_to_name="Amazon BFI4",
        ship_to_address="1800 140th Ave E\nSumner, WA 98390",
        line_items=[
            LineItem(description="Widget Pro X200", qty=10, unit_price=9.99, asin="B09ABC1234"),
            LineItem(description="Gadget Ultra Y500", qty=5, unit_price=24.99, asin="B09DEF5678"),
        ],
        tax_rate=0.10,
        notes="This invoice is issued for Amazon IP dispute resolution.",
    )


@pytest.fixture
def invoice_generator(tmp_path: Path) -> InvoiceGenerator:
    return InvoiceGenerator(output_dir=tmp_path)


# ══════════════════════════════════════════════════════════════════════════════
# 1. InvoiceGenerator — PDF 생성
# ══════════════════════════════════════════════════════════════════════════════


class TestInvoiceGeneration:
    def test_pdf_file_is_created(self, invoice_generator, sample_invoice_data, tmp_path):
        path = invoice_generator.generate(sample_invoice_data)
        assert path.exists(), "PDF 파일이 존재해야 한다"

    def test_pdf_extension(self, invoice_generator, sample_invoice_data):
        path = invoice_generator.generate(sample_invoice_data)
        assert path.suffix == ".pdf"

    def test_pdf_is_non_empty(self, invoice_generator, sample_invoice_data):
        path = invoice_generator.generate(sample_invoice_data)
        assert path.stat().st_size > 1024, "PDF 크기가 1 KB 이상이어야 한다"

    def test_pdf_filename_matches_invoice_number(self, invoice_generator, sample_invoice_data):
        path = invoice_generator.generate(sample_invoice_data)
        assert path.stem == sample_invoice_data.invoice_number

    def test_pdf_starts_with_pdf_magic_bytes(self, invoice_generator, sample_invoice_data):
        path = invoice_generator.generate(sample_invoice_data)
        with open(path, "rb") as f:
            header = f.read(4)
        assert header == b"%PDF", "파일이 PDF 형식이어야 한다"

    def test_pdf_path_is_absolute(self, invoice_generator, sample_invoice_data):
        path = invoice_generator.generate(sample_invoice_data)
        assert path.is_absolute(), "반환 경로가 절대 경로여야 한다"

    def test_output_dir_created_automatically(self, tmp_path):
        nested = tmp_path / "nested" / "invoices"
        gen = InvoiceGenerator(output_dir=nested)
        data = InvoiceData(
            invoice_number="INV-TEST-0001",
            issued_date=datetime.utcnow(),
            purpose="Test",
            buyer_name="Test Buyer",
            buyer_address="123 Test St",
            line_items=[LineItem(description="Item", qty=1, unit_price=1.0)],
        )
        path = gen.generate(data)
        assert path.exists()

    def test_multiple_line_items_all_included(self, invoice_generator, sample_invoice_data):
        """라인 아이템이 2개 이상일 때 파일이 정상 생성된다."""
        path = invoice_generator.generate(sample_invoice_data)
        assert path.exists()
        assert path.stat().st_size > 0

    def test_pdf_with_no_ship_to(self, invoice_generator, tmp_path):
        data = InvoiceData(
            invoice_number="INV-2026W19-NOSHP",
            issued_date=datetime.utcnow(),
            purpose="FBA Inbound",
            buyer_name="Test Co",
            buyer_address="1 Test Ave",
            line_items=[LineItem(description="Item", qty=2, unit_price=5.0, asin="B00TEST001")],
        )
        path = invoice_generator.generate(data)
        assert path.exists()

    def test_pdf_with_zero_tax(self, invoice_generator):
        data = InvoiceData(
            invoice_number="INV-2026W19-NOTAX",
            issued_date=datetime.utcnow(),
            purpose="IP Dispute",
            buyer_name="Buyer",
            buyer_address="Addr",
            line_items=[LineItem(description="Product", qty=1, unit_price=50.0)],
            tax_rate=0.0,
        )
        path = invoice_generator.generate(data)
        assert path.exists()


# ══════════════════════════════════════════════════════════════════════════════
# 2. InvoiceData 산술
# ══════════════════════════════════════════════════════════════════════════════


class TestInvoiceArithmetic:
    def test_subtotal_single_item(self):
        data = InvoiceData(
            invoice_number="INV-X", issued_date=datetime.utcnow(),
            purpose="Test", buyer_name="B", buyer_address="A",
            line_items=[LineItem(description="Item", qty=3, unit_price=10.0)],
        )
        assert data.subtotal == 30.0

    def test_subtotal_multiple_items(self, sample_invoice_data):
        # 10 × 9.99 + 5 × 24.99 = 99.90 + 124.95 = 224.85
        assert sample_invoice_data.subtotal == pytest.approx(224.85, abs=0.01)

    def test_tax_amount(self, sample_invoice_data):
        # 224.85 × 0.10 = 22.485 → 22.49 (round)
        assert sample_invoice_data.tax_amount == pytest.approx(22.49, abs=0.01)

    def test_total_amount(self, sample_invoice_data):
        assert sample_invoice_data.total_amount == pytest.approx(
            sample_invoice_data.subtotal + sample_invoice_data.tax_amount, abs=0.01
        )

    def test_zero_tax_rate(self):
        data = InvoiceData(
            invoice_number="INV-X", issued_date=datetime.utcnow(),
            purpose="Test", buyer_name="B", buyer_address="A",
            line_items=[LineItem(description="Item", qty=1, unit_price=100.0)],
            tax_rate=0.0,
        )
        assert data.tax_amount == 0.0
        assert data.total_amount == 100.0

    def test_line_item_total(self):
        item = LineItem(description="Widget", qty=7, unit_price=3.33)
        assert item.total == pytest.approx(23.31, abs=0.01)

    def test_empty_line_items_subtotal_zero(self):
        data = InvoiceData(
            invoice_number="INV-EMPTY", issued_date=datetime.utcnow(),
            purpose="Test", buyer_name="B", buyer_address="A",
        )
        assert data.subtotal == 0.0
        assert data.total_amount == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# 3. Invoice Number Format
# ══════════════════════════════════════════════════════════════════════════════


class TestInvoiceNumberFormat:
    _PATTERN = re.compile(r"^INV-\d{4}W\d{2}-\d{4}$")

    @pytest.mark.parametrize("week_key, seq, expected", [
        ("2026-W19", 1,    "INV-2026W19-0001"),
        ("2026-W01", 99,   "INV-2026W01-0099"),
        ("2025-W52", 1000, "INV-2025W52-1000"),
    ])
    def test_build_invoice_number(self, week_key, seq, expected):
        result = InvoiceGenerator.build_invoice_number(week_key, seq)
        assert result == expected

    @pytest.mark.parametrize("week_key, seq", [
        ("2026-W19", 1),
        ("2026-W52", 42),
    ])
    def test_invoice_number_matches_pattern(self, week_key, seq):
        result = InvoiceGenerator.build_invoice_number(week_key, seq)
        assert self._PATTERN.match(result), f"{result!r} does not match expected pattern"


# ══════════════════════════════════════════════════════════════════════════════
# 4. LogisticsTracker 이슈 감지
# ══════════════════════════════════════════════════════════════════════════════


TN = "1Z999AA10123456784"
AMZN_ID = "FBA123456789"


class TestLogisticsTrackerIssues:
    def test_no_issues_happy_path(self):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(status="IN_TRANSIT"),
            sp_client=MockAmazonSPClient(amazon_status="WORKING"),
        )
        issues = tracker.detect_issues(TN, AMZN_ID)
        assert issues == []

    def test_detects_seven_day_stall(self):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(stalled=True),
            sp_client=MockAmazonSPClient(),
        )
        issues = tracker.detect_issues(TN)
        types = [i.issue_type for i in issues]
        assert IssueType.INBOUND_DELAY in types

    def test_stall_issue_requires_invoice(self):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(stalled=True),
            sp_client=MockAmazonSPClient(),
        )
        issues = tracker.detect_issues(TN)
        delay = next(i for i in issues if i.issue_type == IssueType.INBOUND_DELAY)
        assert delay.requires_invoice is True

    def test_stall_urgency_is_critical(self):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(stalled=True),
            sp_client=MockAmazonSPClient(),
        )
        issues = tracker.detect_issues(TN)
        delay = next(i for i in issues if i.issue_type == IssueType.INBOUND_DELAY)
        assert delay.urgency == "CRITICAL"

    def test_detects_action_required(self):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(),
            sp_client=MockAmazonSPClient(action_required=True),
        )
        issues = tracker.detect_issues(TN, AMZN_ID)
        types = [i.issue_type for i in issues]
        assert IssueType.ACTION_REQUIRED in types

    def test_action_required_requires_invoice(self):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(),
            sp_client=MockAmazonSPClient(action_required=True),
        )
        issues = tracker.detect_issues(TN, AMZN_ID)
        ar = next(i for i in issues if i.issue_type == IssueType.ACTION_REQUIRED)
        assert ar.requires_invoice is True

    def test_action_required_urgency_critical(self):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(),
            sp_client=MockAmazonSPClient(action_required=True),
        )
        issues = tracker.detect_issues(TN, AMZN_ID)
        ar = next(i for i in issues if i.issue_type == IssueType.ACTION_REQUIRED)
        assert ar.urgency == "CRITICAL"

    def test_detects_ups_exception(self):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(status="EXCEPTION", last_event="Package damaged"),
            sp_client=MockAmazonSPClient(),
        )
        issues = tracker.detect_issues(TN)
        types = [i.issue_type for i in issues]
        assert IssueType.UPS_EXCEPTION in types

    def test_ups_exception_does_not_require_invoice_by_default(self):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(status="EXCEPTION"),
            sp_client=MockAmazonSPClient(),
        )
        issues = tracker.detect_issues(TN)
        exc = next(i for i in issues if i.issue_type == IssueType.UPS_EXCEPTION)
        assert exc.requires_invoice is False

    def test_ups_exception_urgency_is_warning(self):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(status="EXCEPTION"),
            sp_client=MockAmazonSPClient(),
        )
        issues = tracker.detect_issues(TN)
        exc = next(i for i in issues if i.issue_type == IssueType.UPS_EXCEPTION)
        assert exc.urgency == "WARNING"

    def test_detects_fc_delayed(self):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(),
            sp_client=MockAmazonSPClient(fc_delayed=True),
        )
        issues = tracker.detect_issues(TN, AMZN_ID)
        types = [i.issue_type for i in issues]
        assert IssueType.FC_DELAYED in types

    def test_fc_delayed_urgency_is_warning(self):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(),
            sp_client=MockAmazonSPClient(fc_delayed=True),
        )
        issues = tracker.detect_issues(TN, AMZN_ID)
        fc = next(i for i in issues if i.issue_type == IssueType.FC_DELAYED)
        assert fc.urgency == "WARNING"

    def test_no_sp_api_call_when_no_shipment_id(self):
        mock_sp = MagicMock()
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(status="IN_TRANSIT"),
            sp_client=mock_sp,
        )
        tracker.detect_issues(TN)  # no amazon_shipment_id
        mock_sp.get_inbound_shipment.assert_not_called()

    def test_delivered_status_no_delay_issue(self):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(status="DELIVERED", stalled=True),
            sp_client=MockAmazonSPClient(),
        )
        issues = tracker.detect_issues(TN)
        types = [i.issue_type for i in issues]
        assert IssueType.INBOUND_DELAY not in types

    def test_multiple_issues_can_be_detected(self):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(stalled=True),
            sp_client=MockAmazonSPClient(action_required=True),
        )
        issues = tracker.detect_issues(TN, AMZN_ID)
        assert len(issues) >= 2

    def test_issue_tracking_number_set(self):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(stalled=True),
            sp_client=MockAmazonSPClient(),
        )
        issues = tracker.detect_issues(TN)
        assert all(i.tracking_number == TN for i in issues)

    def test_shipment_issue_amazon_id_set(self):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(),
            sp_client=MockAmazonSPClient(action_required=True),
        )
        issues = tracker.detect_issues(TN, AMZN_ID)
        ar = next(i for i in issues if i.issue_type == IssueType.ACTION_REQUIRED)
        assert ar.amazon_shipment_id == AMZN_ID


# ══════════════════════════════════════════════════════════════════════════════
# 5. TelegramNotifier.send_invoice_ready
# ══════════════════════════════════════════════════════════════════════════════


class TestSendInvoiceReady:
    def _make_notifier(self, token="tok", chat_id="cid") -> TelegramNotifier:
        return TelegramNotifier(token=token, chat_id=chat_id)

    def test_returns_true_on_success(self, tmp_path):
        pdf = tmp_path / "INV-2026W19-0001.pdf"
        pdf.write_bytes(b"%PDF-1.4 mock")
        notifier = self._make_notifier()
        with patch.object(notifier, "send", return_value=True) as mock_send:
            result = notifier.send_invoice_ready(pdf)
        assert result is True
        mock_send.assert_called_once()

    def test_message_contains_korean_phrase(self, tmp_path):
        pdf = tmp_path / "invoice.pdf"
        pdf.write_bytes(b"%PDF")
        notifier = self._make_notifier()
        captured: list[str] = []
        with patch.object(notifier, "send", side_effect=lambda msg, **kw: captured.append(msg) or True):
            notifier.send_invoice_ready(pdf)
        assert "아마존 소명 자료가 준비되었습니다" in captured[0]

    def test_message_contains_absolute_pdf_path(self, tmp_path):
        pdf = tmp_path / "INV-TEST.pdf"
        pdf.write_bytes(b"%PDF")
        notifier = self._make_notifier()
        captured: list[str] = []
        with patch.object(notifier, "send", side_effect=lambda msg, **kw: captured.append(msg) or True):
            notifier.send_invoice_ready(pdf)
        abs_path = str(pdf.resolve())
        assert abs_path in captured[0]

    def test_message_level_is_critical(self, tmp_path):
        pdf = tmp_path / "invoice.pdf"
        pdf.write_bytes(b"%PDF")
        notifier = self._make_notifier()
        captured_kwargs: list[dict] = []
        with patch.object(
            notifier, "send",
            side_effect=lambda msg, level=AlertLevel.INFO, **kw: captured_kwargs.append({"level": level}) or True
        ):
            notifier.send_invoice_ready(pdf)
        assert captured_kwargs[0]["level"] == AlertLevel.CRITICAL

    def test_context_appended_to_message(self, tmp_path):
        pdf = tmp_path / "invoice.pdf"
        pdf.write_bytes(b"%PDF")
        notifier = self._make_notifier()
        captured: list[str] = []
        with patch.object(notifier, "send", side_effect=lambda msg, **kw: captured.append(msg) or True):
            notifier.send_invoice_ready(pdf, context="ACTION_REQUIRED: FBA123456789")
        assert "ACTION_REQUIRED" in captured[0]

    def test_no_telegram_config_returns_false(self, tmp_path):
        pdf = tmp_path / "invoice.pdf"
        pdf.write_bytes(b"%PDF")
        notifier = TelegramNotifier(token="", chat_id="")
        result = notifier.send_invoice_ready(pdf)
        assert result is False

    def test_http_error_returns_false(self, tmp_path):
        pdf = tmp_path / "invoice.pdf"
        pdf.write_bytes(b"%PDF")
        notifier = self._make_notifier()
        with patch.object(notifier, "send", return_value=False):
            result = notifier.send_invoice_ready(pdf)
        assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# 6. 통합: 이슈 감지 → 인보이스 생성 → Telegram 알림
# ══════════════════════════════════════════════════════════════════════════════


class TestResolutionHubIntegration:
    """
    ACTION_REQUIRED 이슈 감지 → 인보이스 PDF 생성 → 텔레그램 알림 전체 플로우.
    """

    def _build_invoice_for_issue(self, issue: ShipmentIssue, generator: InvoiceGenerator) -> Path:
        inv_num = InvoiceGenerator.build_invoice_number("2026-W19", 1)
        data = InvoiceData(
            invoice_number=inv_num,
            issued_date=datetime.utcnow(),
            purpose="Amazon IP Dispute" if issue.issue_type == IssueType.ACTION_REQUIRED
                    else "FBA Inbound Delay",
            buyer_name="Amazon.com Services LLC",
            buyer_address="410 Terry Ave N\nSeattle, WA 98109",
            line_items=[
                LineItem(description="Test Product", qty=10, unit_price=15.0, asin="B09TEST0001"),
            ],
            notes=issue.message,
        )
        return generator.generate(data)

    def test_action_required_triggers_invoice_and_alert(self, tmp_path):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(),
            sp_client=MockAmazonSPClient(action_required=True),
        )
        generator = InvoiceGenerator(output_dir=tmp_path)
        notifier = TelegramNotifier(token="tok", chat_id="cid")

        issues = tracker.detect_issues(TN, AMZN_ID)
        assert any(i.issue_type == IssueType.ACTION_REQUIRED for i in issues)

        critical_issues = [i for i in issues if i.urgency == "CRITICAL"]
        invoice_path = self._build_invoice_for_issue(critical_issues[0], generator)

        assert invoice_path.exists()
        assert invoice_path.stat().st_size > 0

        with patch.object(notifier, "send", return_value=True) as mock_send:
            result = notifier.send_invoice_ready(
                invoice_path,
                context=f"이슈: {critical_issues[0].issue_type.value}",
            )

        assert result is True
        call_args = mock_send.call_args[0][0]
        assert "아마존 소명 자료가 준비되었습니다" in call_args
        assert str(invoice_path.resolve()) in call_args

    def test_inbound_delay_triggers_invoice_and_alert(self, tmp_path):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(stalled=True),
            sp_client=MockAmazonSPClient(),
        )
        generator = InvoiceGenerator(output_dir=tmp_path)
        notifier = TelegramNotifier(token="tok", chat_id="cid")

        issues = tracker.detect_issues(TN)
        assert any(i.issue_type == IssueType.INBOUND_DELAY for i in issues)

        delay_issue = next(i for i in issues if i.issue_type == IssueType.INBOUND_DELAY)
        invoice_path = self._build_invoice_for_issue(delay_issue, generator)
        assert invoice_path.exists()

        with patch.object(notifier, "send", return_value=True) as mock_send:
            notifier.send_invoice_ready(invoice_path)

        call_args = mock_send.call_args[0][0]
        assert str(invoice_path.resolve()) in call_args

    def test_no_alert_when_no_issues(self, tmp_path):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(status="DELIVERED"),
            sp_client=MockAmazonSPClient(amazon_status="CLOSED"),
        )
        notifier_send = MagicMock()

        issues = tracker.detect_issues(TN, AMZN_ID)
        critical = [i for i in issues if i.urgency == "CRITICAL"]

        if not critical:
            notifier_send.assert_not_called()

    def test_invoice_number_format_in_integration(self, tmp_path):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(),
            sp_client=MockAmazonSPClient(action_required=True),
        )
        generator = InvoiceGenerator(output_dir=tmp_path)
        issues = tracker.detect_issues(TN, AMZN_ID)
        issue = next(i for i in issues if i.issue_type == IssueType.ACTION_REQUIRED)

        invoice_path = self._build_invoice_for_issue(issue, generator)
        assert re.match(r"INV-\d{4}W\d{2}-\d{4}", invoice_path.stem)

    def test_pdf_contains_pdf_header(self, tmp_path):
        tracker = LogisticsTracker(
            ups_client=MockUPSClient(),
            sp_client=MockAmazonSPClient(action_required=True),
        )
        generator = InvoiceGenerator(output_dir=tmp_path)
        issues = tracker.detect_issues(TN, AMZN_ID)
        issue = next(i for i in issues if i.issue_type == IssueType.ACTION_REQUIRED)

        invoice_path = self._build_invoice_for_issue(issue, generator)
        with open(invoice_path, "rb") as f:
            assert f.read(4) == b"%PDF"
