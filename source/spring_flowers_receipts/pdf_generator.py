from __future__ import annotations

import textwrap
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from .models import Payment, Profile, format_money, format_payment_date, month_name


MAX_PAYMENT_ROWS = 12
ACCENT = colors.HexColor("#e17a7c")
ORANGE = colors.HexColor("#ff7f3f")
LIGHT_ROW = colors.HexColor("#f2f2f2")
GRID = colors.HexColor("#bfbfbf")
TEXT = colors.HexColor("#111111")
DARK_TOTAL = colors.HexColor("#364152")


def _register_fonts() -> tuple[str, str]:
    regular = "Helvetica"
    bold = "Helvetica-Bold"
    windows_fonts = Path("C:/Windows/Fonts")
    arial = windows_fonts / "arial.ttf"
    arial_bold = windows_fonts / "arialbd.ttf"

    try:
        if arial.exists():
            pdfmetrics.registerFont(TTFont("ReceiptArial", str(arial)))
            regular = "ReceiptArial"
        if arial_bold.exists():
            pdfmetrics.registerFont(TTFont("ReceiptArialBold", str(arial_bold)))
            bold = "ReceiptArialBold"
    except Exception:
        regular = "Helvetica"
        bold = "Helvetica-Bold"

    return regular, bold


def generate_receipt_pdf(
    output_path: Path,
    profile: Profile,
    settings: dict[str, str],
    receipt_month: int,
    receipt_year: int,
    payments: list[Payment],
    note: str = "",
    logo_path: Path | None = None,
) -> None:
    if not payments:
        raise ValueError("At least one payment is required.")
    if len(payments) > MAX_PAYMENT_ROWS:
        raise ValueError(f"Receipts can include at most {MAX_PAYMENT_ROWS} payment rows.")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    font_regular, font_bold = _register_fonts()
    pdf = canvas.Canvas(str(output_path), pagesize=letter)
    pdf.setTitle(
        f"{profile.child_name} - Payment Receipt - {month_name(receipt_month)} {receipt_year}"
    )

    _draw_receipt(
        pdf=pdf,
        profile=profile,
        settings=settings,
        receipt_month=receipt_month,
        receipt_year=receipt_year,
        payments=payments,
        note=note.strip(),
        logo_path=logo_path,
        font_regular=font_regular,
        font_bold=font_bold,
    )
    pdf.save()


def _draw_receipt(
    pdf: canvas.Canvas,
    profile: Profile,
    settings: dict[str, str],
    receipt_month: int,
    receipt_year: int,
    payments: list[Payment],
    note: str,
    logo_path: Path | None,
    font_regular: str,
    font_bold: str,
) -> None:
    _page_width, _page_height = letter
    content_x = 88.4
    content_w = 323.8
    left_x = 113.5
    right_x = 286.2

    pdf.setFillColor(ACCENT)
    pdf.rect(content_x, 733.1, content_w, 13.4, fill=1, stroke=0)

    if logo_path and logo_path.exists():
        pdf.drawImage(str(logo_path), 270.5, 602.0, width=141.1, height=129.8, mask="auto")

    pdf.setFillColor(ORANGE)
    pdf.setFont(font_regular, 24)
    pdf.drawString(115.4, 661, "Payment")
    pdf.drawString(115.4, 631, "Receipt")

    pdf.setFillColor(TEXT)
    business_lines = [
        settings.get("business_name", ""),
        settings.get("business_address_line1", ""),
        settings.get("business_address_line2", ""),
        settings.get("business_phone", ""),
        settings.get("business_email", ""),
    ]
    business_x = 113.8
    y = 578.3
    for index, line in enumerate([line for line in business_lines if line.strip()]):
        pdf.setFont(font_bold if index == 0 else font_regular, 11)
        pdf.drawString(business_x, y, line)
        if "@" in line:
            pdf.setStrokeColor(TEXT)
            pdf.line(business_x, y - 2, business_x + stringWidth(line, font_regular, 11), y - 2)
        y -= 17.5

    _draw_info_block(
        pdf,
        x=left_x,
        y=475,
        width=154.3,
        heading="Parent Inforamtion",
        lines=[
            profile.parent1_name,
            profile.parent2_name,
            profile.address_line1,
            profile.address_line2,
            "   |   ".join([p for p in (profile.phone1, profile.phone2) if p.strip()]),
        ],
        font_regular=font_regular,
        font_bold=font_bold,
    )
    _draw_info_block(
        pdf,
        x=right_x,
        y=475,
        width=126,
        heading="Child Information",
        lines=[
            profile.child_name,
            f"Status: {profile.status}" if profile.status.strip() else "",
        ],
        font_regular=font_regular,
        font_bold=font_bold,
    )

    table_y = _draw_payment_table(
        pdf,
        x=content_x,
        y=323.9,
        width=content_w,
        payments=payments,
        font_regular=font_regular,
        font_bold=font_bold,
    )

    bar_y = table_y - 38
    pdf.setFillColor(ACCENT)
    pdf.rect(content_x, bar_y, content_w, 19, fill=1, stroke=0)

    if note:
        _draw_note(
            pdf,
            x=content_x,
            y=bar_y,
            width=content_w,
            note=note,
            font_regular=font_regular,
            font_bold=font_bold,
        )

    pdf.showPage()


def _draw_info_block(
    pdf: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    heading: str,
    lines: list[str],
    font_regular: str,
    font_bold: str,
) -> None:
    pdf.setFillColor(ORANGE)
    pdf.setFont(font_bold, 9)
    pdf.drawString(x, y, heading)
    pdf.setStrokeColor(GRID)
    pdf.line(x, y - 4, x + width, y - 4)

    pdf.setFillColor(TEXT)
    current_y = y - 22
    for index, line in enumerate([line for line in lines if line.strip()]):
        pdf.setFont(font_bold if current_y == y - 22 else font_regular, 10)
        wrapped = _wrap_line(pdf, line, font_regular, 10, width)
        for part in wrapped[:2]:
            indent = 8 if index in (2, 3) else 0
            pdf.drawString(x + indent, current_y, part)
            current_y -= 16


def _draw_payment_table(
    pdf: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    payments: list[Payment],
    font_regular: str,
    font_bold: str,
) -> float:
    col_marker = 23.2
    col_number = 27.7
    col_date = 145.1
    col_amount = width - col_marker - col_number - col_date
    row_h = 18
    total_h = row_h

    pdf.setFillColor(ACCENT)
    pdf.rect(x, y, width, 17.5, fill=1, stroke=0)
    pdf.setFillColor(TEXT)
    pdf.setFont(font_bold, 9)
    pdf.drawCentredString(x + col_marker + col_number / 2, y + 5, "#")
    pdf.drawCentredString(x + col_marker + col_number + col_date / 2, y + 5, "Date Of Payment")
    pdf.drawCentredString(x + width - col_amount / 2, y + 5, "Amount")

    current_y = y - row_h
    pdf.setStrokeColor(GRID)
    total_cents = 0
    for index, payment in enumerate(payments, start=1):
        total_cents += payment.amount_cents
        if index % 2 == 0:
            pdf.setFillColor(LIGHT_ROW)
            pdf.rect(x + col_marker, current_y, width - col_marker, row_h, fill=1, stroke=0)

        pdf.setFillColor(TEXT)
        pdf.setFont(font_regular, 9)
        if payment.marker:
            pdf.drawCentredString(x + col_marker / 2, current_y + 7, payment.marker)
        pdf.drawCentredString(x + col_marker + col_number / 2, current_y + 7, str(index))
        pdf.drawCentredString(
            x + col_marker + col_number + col_date / 2,
            current_y + 7,
            format_payment_date(payment.payment_date),
        )
        pdf.drawString(x + col_marker + col_number + col_date + 5.8, current_y + 7, "$")
        pdf.drawRightString(x + width - 5, current_y + 7, f"{payment.amount_cents / 100:,.2f}")

        pdf.rect(x + col_marker, current_y, width - col_marker, row_h, fill=0, stroke=1)
        for col_x in (
            x + col_marker + col_number,
            x + col_marker + col_number + col_date,
        ):
            pdf.line(col_x, current_y, col_x, current_y + row_h)
        current_y -= row_h

    total_y = current_y
    amount_x = x + col_marker + col_number + col_date
    if (len(payments) + 1) % 2 == 0:
        pdf.setFillColor(LIGHT_ROW)
    else:
        pdf.setFillColor(colors.white)
    pdf.rect(amount_x, total_y, col_amount, total_h, fill=1, stroke=0)
    pdf.setStrokeColor(GRID)
    pdf.rect(amount_x, total_y, col_amount, total_h, fill=0, stroke=1)

    pdf.setFont(font_bold, 9)
    pdf.setFillColor(DARK_TOTAL)
    pdf.drawRightString(amount_x - 2, total_y + 7, "TOTAL")
    pdf.setFillColor(TEXT)
    pdf.drawString(amount_x + 5.8, total_y + 7, "$")
    pdf.drawRightString(x + width - 5, total_y + 7, f"{total_cents / 100:,.2f}")

    return total_y


def _draw_note(
    pdf: canvas.Canvas,
    x: float,
    y: float,
    width: float,
    note: str,
    font_regular: str,
    font_bold: str,
) -> None:
    pdf.setFillColor(ACCENT)
    pdf.rect(x, y, width, 20, fill=1, stroke=0)

    note_y = y - 26
    pdf.setFillColor(TEXT)
    pdf.setFont(font_regular, 11)
    pdf.drawString(x + 20, note_y, "*")
    pdf.setFont(font_bold, 11)
    pdf.drawString(x + 34, note_y, "Note")

    wrapped = textwrap.wrap(note, width=58)
    pdf.setFont(font_regular, 11)
    text_x = x + 84
    for index, line in enumerate(wrapped[:4]):
        pdf.drawString(text_x, note_y - (index * 15), line)


def _wrap_line(
    pdf: canvas.Canvas,
    line: str,
    font_name: str,
    font_size: int,
    max_width: float,
) -> list[str]:
    words = line.split()
    if not words:
        return []

    result: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if pdf.stringWidth(candidate, font_name, font_size) <= max_width:
            current = candidate
        else:
            result.append(current)
            current = word
    result.append(current)
    return result
