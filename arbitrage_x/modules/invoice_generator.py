"""
Arbitrage-X — Invoice Generator
회사 정보 + 로고/직인을 기반으로 전문 PDF 인보이스를 자동 생성한다.
라이브러리: reportlab
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


@dataclass
class LineItem:
    description: str
    qty: int
    unit_price: float

    @property
    def total(self) -> float:
        return self.qty * self.unit_price


@dataclass
class InvoiceData:
    invoice_number: str
    issued_date: datetime
    purpose: str

    buyer_name: str
    buyer_address: str

    line_items: list[LineItem]
    tax_rate: float = 0.0
    notes: str = ""

    @property
    def subtotal(self) -> float:
        return sum(i.total for i in self.line_items)

    @property
    def tax_amount(self) -> float:
        return self.subtotal * self.tax_rate

    @property
    def total_amount(self) -> float:
        return self.subtotal + self.tax_amount


class InvoiceGenerator:
    """
    사용 예:
        gen = InvoiceGenerator()
        path = gen.generate(invoice_data)
    """

    def __init__(self, output_dir: Optional[Path] = None):
        self.output_dir = output_dir or INVOICES_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, data: InvoiceData) -> Path:
        """PDF 인보이스를 생성하고 파일 경로를 반환한다."""
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.units import mm
            from reportlab.platypus import (
                SimpleDocTemplate, Table, TableStyle,
                Paragraph, Spacer, Image, HRFlowable,
            )
        except ImportError:
            raise ImportError(
                "reportlab 패키지가 필요합니다: pip install reportlab"
            )

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
        story = []

        # ── 헤더: 로고 + 회사 정보 ────────────────────────────────────────
        header_data = [[
            self._logo_cell(),
            self._company_info_cell(styles),
        ]]
        header_table = Table(header_data, colWidths=[80 * mm, 100 * mm])
        header_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ]))
        story.append(header_table)
        story.append(Spacer(1, 8 * mm))
        story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor("#2C3E50")))
        story.append(Spacer(1, 5 * mm))

        # ── 인보이스 제목 + 번호 ──────────────────────────────────────────
        title_style = ParagraphStyle(
            "InvTitle",
            parent=styles["Heading1"],
            fontSize=22,
            textColor=colors.HexColor("#2C3E50"),
            spaceAfter=4,
        )
        story.append(Paragraph("INVOICE", title_style))

        meta_data = [
            ["Invoice Number:", data.invoice_number],
            ["Date Issued:", data.issued_date.strftime("%Y-%m-%d")],
            ["Purpose:", data.purpose],
        ]
        meta_table = Table(meta_data, colWidths=[45 * mm, 135 * mm])
        meta_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        story.append(meta_table)
        story.append(Spacer(1, 8 * mm))

        # ── 수신자 정보 ───────────────────────────────────────────────────
        bill_to_style = ParagraphStyle(
            "BillTo", parent=styles["Normal"],
            fontSize=10, leading=14,
        )
        story.append(Paragraph("<b>BILL TO</b>", bill_to_style))
        story.append(Paragraph(data.buyer_name, bill_to_style))
        story.append(Paragraph(data.buyer_address.replace("\n", "<br/>"), bill_to_style))
        story.append(Spacer(1, 8 * mm))

        # ── 품목 테이블 ───────────────────────────────────────────────────
        item_header = ["#", "Description", "Qty", "Unit Price", "Total"]
        item_rows = [item_header]
        for i, item in enumerate(data.line_items, 1):
            item_rows.append([
                str(i),
                item.description,
                str(item.qty),
                f"${item.unit_price:,.2f}",
                f"${item.total:,.2f}",
            ])

        col_widths = [10 * mm, 95 * mm, 15 * mm, 25 * mm, 25 * mm]
        item_table = Table(item_rows, colWidths=col_widths)
        item_table.setStyle(TableStyle([
            # 헤더 행
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2C3E50")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 10),
            ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            # 데이터 행
            ("FONTSIZE", (0, 1), (-1, -1), 9),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F2F2")]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(item_table)
        story.append(Spacer(1, 5 * mm))

        # ── 합계 ──────────────────────────────────────────────────────────
        summary_rows = [
            ["", "Subtotal:", f"${data.subtotal:,.2f}"],
            ["", f"Tax ({data.tax_rate * 100:.1f}%):", f"${data.tax_amount:,.2f}"],
            ["", "TOTAL:", f"${data.total_amount:,.2f}"],
        ]
        summary_table = Table(summary_rows, colWidths=[120 * mm, 30 * mm, 30 * mm])
        summary_table.setStyle(TableStyle([
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("FONTNAME", (1, 2), (-1, 2), "Helvetica-Bold"),
            ("FONTSIZE", (1, 2), (-1, 2), 12),
            ("LINEABOVE", (1, 2), (-1, 2), 1, colors.HexColor("#2C3E50")),
            ("TEXTCOLOR", (1, 2), (-1, 2), colors.HexColor("#2C3E50")),
        ]))
        story.append(summary_table)

        # ── 메모 ──────────────────────────────────────────────────────────
        if data.notes:
            story.append(Spacer(1, 8 * mm))
            story.append(Paragraph("<b>Notes</b>", styles["Normal"]))
            story.append(Paragraph(data.notes, styles["Normal"]))

        # ── 직인 ──────────────────────────────────────────────────────────
        story.append(Spacer(1, 12 * mm))
        story.append(self._seal_cell())

        # ── 푸터 ──────────────────────────────────────────────────────────
        story.append(Spacer(1, 8 * mm))
        story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#CCCCCC")))
        footer_style = ParagraphStyle(
            "Footer", parent=styles["Normal"],
            fontSize=8, textColor=colors.gray, alignment=1,
        )
        story.append(Paragraph(
            f"{COMPANY_NAME} | {COMPANY_ADDRESS} | {COMPANY_EMAIL} | {COMPANY_PHONE}",
            footer_style,
        ))

        doc.build(story)
        return path

    # ──────────────────────────────────────────────────────────────────────────
    # 헬퍼
    # ──────────────────────────────────────────────────────────────────────────

    def _logo_cell(self):
        from reportlab.platypus import Image, Paragraph
        from reportlab.lib.styles import getSampleStyleSheet
        if os.path.exists(COMPANY_LOGO_PATH):
            return Image(COMPANY_LOGO_PATH, width=60, height=30)
        styles = getSampleStyleSheet()
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib import colors
        bold = ParagraphStyle(
            "LogoText", parent=styles["Normal"],
            fontSize=16, fontName="Helvetica-Bold",
            textColor=colors.HexColor("#2C3E50"),
        )
        return Paragraph(COMPANY_NAME, bold)

    def _company_info_cell(self, styles):
        from reportlab.platypus import Paragraph
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib import colors
        s = ParagraphStyle(
            "CompInfo", parent=styles["Normal"],
            fontSize=9, alignment=2, leading=13,
            textColor=colors.HexColor("#555555"),
        )
        lines = [
            f"<b>{COMPANY_NAME}</b>",
            COMPANY_ADDRESS,
            COMPANY_EMAIL,
            COMPANY_PHONE,
        ]
        from reportlab.platypus import KeepTogether
        from io import StringIO
        return Paragraph("<br/>".join(lines), s)

    def _seal_cell(self):
        from reportlab.platypus import Image, Paragraph
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors
        if os.path.exists(COMPANY_SEAL_PATH):
            return Image(COMPANY_SEAL_PATH, width=60, height=60)
        styles = getSampleStyleSheet()
        s = ParagraphStyle(
            "SealText", parent=styles["Normal"],
            fontSize=8, textColor=colors.gray,
        )
        return Paragraph(
            f"Authorized by {COMPANY_NAME}<br/>"
            f"Date: {datetime.utcnow().strftime('%Y-%m-%d')}",
            s,
        )

    @staticmethod
    def build_invoice_number(week_key: str, sequence: int) -> str:
        """예: INV-2026W18-0001"""
        clean = week_key.replace("-", "")  # "2026W18"
        return f"INV-{clean}-{sequence:04d}"
