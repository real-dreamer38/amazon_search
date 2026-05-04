"""
Arbitrage-X — Resolution Hub: PDF 인보이스 자동 생성기

라이브러리: reportlab (순수 파이썬 — Termux 모바일 환경 포함 모든 플랫폼 동작)
아마존 소명(IP Dispute, FBA Inbound) 전용 Commercial Invoice 템플릿.

포함 섹션:
  1. 회사 정보 + 로고 Placeholder
  2. Invoice 메타데이터 (번호, 발행일, 목적)
  3. Bill To / Ship To (아마존 FC 주소)
  4. 상품 테이블 — #, Description, ASIN, Qty, Unit Price, Total
  5. 합계 (소계 / 세금 / 총합)
  6. 직인 Placeholder + 서명 영역
  7. 회사 정보 푸터
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from config.settings import (
    COMPANY_ADDRESS,
    COMPANY_EMAIL,
    COMPANY_LOGO_PATH,
    COMPANY_NAME,
    COMPANY_PHONE,
    COMPANY_SEAL_PATH,
    INVOICES_DIR,
)


# ══════════════════════════════════════════════════════════════════════════════
# 도메인 타입
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class LineItem:
    description: str
    qty: int
    unit_price: float
    asin: str = ""          # Amazon ASIN — 소명용 핵심 식별자

    @property
    def total(self) -> float:
        return round(self.qty * self.unit_price, 2)


@dataclass
class InvoiceData:
    invoice_number: str
    issued_date: datetime
    purpose: str            # "Amazon IP Dispute", "FBA Inbound Verification" 등

    # ── Bill To ──────────────────────────────────────────────────────────────
    buyer_name: str
    buyer_address: str

    # ── Ship To (아마존 FC 주소) ──────────────────────────────────────────────
    ship_to_name: str = ""
    ship_to_address: str = ""

    line_items: list[LineItem] = field(default_factory=list)
    tax_rate: float = 0.0
    notes: str = ""

    @property
    def subtotal(self) -> float:
        return round(sum(i.total for i in self.line_items), 2)

    @property
    def tax_amount(self) -> float:
        return round(self.subtotal * self.tax_rate, 2)

    @property
    def total_amount(self) -> float:
        return round(self.subtotal + self.tax_amount, 2)


# ══════════════════════════════════════════════════════════════════════════════
# 인보이스 생성기
# ══════════════════════════════════════════════════════════════════════════════


class InvoiceGenerator:
    """
    사용 예:
        gen = InvoiceGenerator()
        path = gen.generate(invoice_data)
        print(path.resolve())   # /absolute/path/to/INV-2026W19-0001.pdf
    """

    # 브랜드 컬러
    _DARK_BLUE = "#2C3E50"
    _MID_GRAY = "#555555"
    _LIGHT_GRAY = "#F2F2F2"
    _BORDER_GRAY = "#CCCCCC"

    def __init__(self, output_dir: Optional[Path] = None):
        self.output_dir = output_dir or INVOICES_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, data: InvoiceData) -> Path:
        """PDF 인보이스를 생성하고 절대 파일 경로를 반환한다."""
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.units import mm
            from reportlab.platypus import (
                HRFlowable, Image, Paragraph,
                SimpleDocTemplate, Spacer, Table, TableStyle,
            )
        except ImportError:
            raise ImportError("reportlab 패키지가 필요합니다: pip install reportlab")

        filename = f"{data.invoice_number}.pdf"
        path = self.output_dir / filename

        doc = SimpleDocTemplate(
            str(path),
            pagesize=A4,
            rightMargin=15 * mm,
            leftMargin=15 * mm,
            topMargin=15 * mm,
            bottomMargin=15 * mm,
        )

        styles = getSampleStyleSheet()
        c = {
            "dark_blue": colors.HexColor(self._DARK_BLUE),
            "mid_gray": colors.HexColor(self._MID_GRAY),
            "light_gray": colors.HexColor(self._LIGHT_GRAY),
            "border_gray": colors.HexColor(self._BORDER_GRAY),
        }
        story = []

        # ── 1. 헤더: 로고 + 회사 정보 ────────────────────────────────────────
        header_table = Table(
            [[self._logo_cell(styles, c), self._company_info_cell(styles, c)]],
            colWidths=[80 * mm, 100 * mm],
        )
        header_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ]))
        story.append(header_table)
        story.append(Spacer(1, 6 * mm))
        story.append(HRFlowable(width="100%", thickness=2, color=c["dark_blue"]))
        story.append(Spacer(1, 4 * mm))

        # ── 2. 인보이스 제목 + 메타데이터 ────────────────────────────────────
        title_style = ParagraphStyle(
            "InvTitle", parent=styles["Heading1"],
            fontSize=22, textColor=c["dark_blue"], spaceAfter=4,
        )
        story.append(Paragraph("COMMERCIAL INVOICE", title_style))

        meta_style = ParagraphStyle(
            "Meta", parent=styles["Normal"],
            fontSize=9, textColor=c["mid_gray"], leading=14,
        )
        meta_rows = [
            ["Invoice Number:", data.invoice_number],
            ["Date Issued:", data.issued_date.strftime("%Y-%m-%d")],
            ["Purpose:", data.purpose],
        ]
        meta_table = Table(meta_rows, colWidths=[40 * mm, 140 * mm])
        meta_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TEXTCOLOR", (0, 0), (0, -1), c["mid_gray"]),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        story.append(meta_table)
        story.append(Spacer(1, 6 * mm))

        # ── 3. Bill To / Ship To ──────────────────────────────────────────────
        addr_style = ParagraphStyle(
            "Addr", parent=styles["Normal"],
            fontSize=9, leading=13, textColor=c["mid_gray"],
        )
        label_style = ParagraphStyle(
            "AddrLabel", parent=styles["Normal"],
            fontSize=9, fontName="Helvetica-Bold", spaceAfter=2,
        )

        bill_cell = [
            Paragraph("BILL TO", label_style),
            Paragraph(data.buyer_name, addr_style),
            Paragraph(data.buyer_address.replace("\n", "<br/>"), addr_style),
        ]
        ship_cell = [
            Paragraph("SHIP TO", label_style),
            Paragraph(data.ship_to_name or "—", addr_style),
            Paragraph((data.ship_to_address or "—").replace("\n", "<br/>"), addr_style),
        ]

        addr_table = Table([[bill_cell, ship_cell]], colWidths=[90 * mm, 90 * mm])
        addr_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (0, 0), 0.5, c["border_gray"]),
            ("BOX", (1, 0), (1, 0), 0.5, c["border_gray"]),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(addr_table)
        story.append(Spacer(1, 6 * mm))

        # ── 4. 품목 테이블 (#, Description, ASIN, Qty, Unit Price, Total) ────
        item_header = ["#", "Description", "ASIN", "Qty", "Unit Price", "Total"]
        item_rows = [item_header]
        for i, item in enumerate(data.line_items, 1):
            item_rows.append([
                str(i),
                item.description,
                item.asin or "—",
                str(item.qty),
                f"${item.unit_price:,.2f}",
                f"${item.total:,.2f}",
            ])

        col_widths = [7 * mm, 68 * mm, 28 * mm, 13 * mm, 22 * mm, 22 * mm]
        item_table = Table(item_rows, colWidths=col_widths, repeatRows=1)
        item_table.setStyle(TableStyle([
            # 헤더
            ("BACKGROUND", (0, 0), (-1, 0), c["dark_blue"]),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 9),
            # 데이터
            ("FONTSIZE", (0, 1), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, c["light_gray"]]),
            ("GRID", (0, 0), (-1, -1), 0.5, c["border_gray"]),
            # 정렬
            ("ALIGN", (0, 0), (0, -1), "CENTER"),   # #
            ("ALIGN", (3, 0), (-1, -1), "RIGHT"),   # Qty, Unit Price, Total
            ("ALIGN", (2, 0), (2, -1), "CENTER"),   # ASIN
            # 패딩
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(item_table)
        story.append(Spacer(1, 4 * mm))

        # ── 5. 합계 ──────────────────────────────────────────────────────────
        summary_rows = [
            ["", "Subtotal:", f"${data.subtotal:,.2f}"],
            ["", f"Tax ({data.tax_rate * 100:.1f}%):", f"${data.tax_amount:,.2f}"],
            ["", "TOTAL DUE:", f"${data.total_amount:,.2f}"],
        ]
        summary_table = Table(summary_rows, colWidths=[118 * mm, 30 * mm, 32 * mm])
        summary_table.setStyle(TableStyle([
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("FONTNAME", (1, 2), (-1, 2), "Helvetica-Bold"),
            ("FONTSIZE", (1, 2), (-1, 2), 11),
            ("LINEABOVE", (1, 2), (-1, 2), 1.2, c["dark_blue"]),
            ("TEXTCOLOR", (1, 2), (-1, 2), c["dark_blue"]),
            ("TOPPADDING", (0, 2), (-1, 2), 5),
        ]))
        story.append(summary_table)

        # ── 메모 ─────────────────────────────────────────────────────────────
        if data.notes:
            story.append(Spacer(1, 6 * mm))
            note_style = ParagraphStyle(
                "Note", parent=styles["Normal"], fontSize=8, leading=12,
            )
            story.append(Paragraph("<b>Notes / 비고</b>", note_style))
            story.append(Paragraph(data.notes, note_style))

        # ── 6. 직인 & 서명 영역 ──────────────────────────────────────────────
        story.append(Spacer(1, 10 * mm))
        story.append(self._signature_block(styles, c))

        # ── 7. 푸터 ──────────────────────────────────────────────────────────
        story.append(Spacer(1, 6 * mm))
        story.append(HRFlowable(width="100%", thickness=0.8, color=c["border_gray"]))
        footer_style = ParagraphStyle(
            "Footer", parent=styles["Normal"],
            fontSize=7, textColor=colors.gray, alignment=1,
        )
        story.append(Paragraph(
            f"{COMPANY_NAME} &nbsp;|&nbsp; {COMPANY_ADDRESS} "
            f"&nbsp;|&nbsp; {COMPANY_EMAIL} &nbsp;|&nbsp; {COMPANY_PHONE}",
            footer_style,
        ))

        doc.build(story)
        return path.resolve()

    # ──────────────────────────────────────────────────────────────────────────
    # 헬퍼 메서드
    # ──────────────────────────────────────────────────────────────────────────

    def _logo_cell(self, styles, c: dict):
        from reportlab.platypus import Image, Paragraph
        from reportlab.lib.styles import ParagraphStyle
        if os.path.exists(COMPANY_LOGO_PATH):
            return Image(COMPANY_LOGO_PATH, width=60, height=30)
        s = ParagraphStyle(
            "LogoText", parent=styles["Normal"],
            fontSize=15, fontName="Helvetica-Bold",
            textColor=c["dark_blue"],
        )
        return Paragraph(COMPANY_NAME, s)

    def _company_info_cell(self, styles, c: dict):
        from reportlab.platypus import Paragraph
        from reportlab.lib.styles import ParagraphStyle
        s = ParagraphStyle(
            "CompInfo", parent=styles["Normal"],
            fontSize=8, alignment=2, leading=12,
            textColor=c["mid_gray"],
        )
        lines = [
            f"<b>{COMPANY_NAME}</b>",
            COMPANY_ADDRESS,
            COMPANY_EMAIL,
            COMPANY_PHONE,
        ]
        return Paragraph("<br/>".join(lines), s)

    def _signature_block(self, styles, c: dict):
        from reportlab.platypus import Image, Paragraph, Table, TableStyle
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib import colors

        sig_style = ParagraphStyle(
            "Sig", parent=styles["Normal"], fontSize=8, textColor=c["mid_gray"],
        )

        if os.path.exists(COMPANY_SEAL_PATH):
            seal = Image(COMPANY_SEAL_PATH, width=55, height=55)
        else:
            seal = Paragraph(
                f"[Official Seal / 직인]<br/>{COMPANY_NAME}<br/>"
                f"{datetime.utcnow().strftime('%Y-%m-%d')}",
                sig_style,
            )

        sig_line = Paragraph(
            "Authorized Signature / 권한자 서명<br/>"
            "______________________________<br/>"
            f"{COMPANY_NAME}",
            sig_style,
        )

        sig_table = Table([[seal, sig_line]], colWidths=[65, 115])
        sig_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ]))
        return sig_table

    @staticmethod
    def build_invoice_number(week_key: str, sequence: int) -> str:
        """예: INV-2026W19-0001"""
        clean = week_key.replace("-", "")
        return f"INV-{clean}-{sequence:04d}"
