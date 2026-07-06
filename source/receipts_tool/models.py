from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


MONTH_NAMES = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]

TEMPLATE_VERSION = "receipt-tool-v1"


@dataclass
class Profile:
    id: int | None = None
    child_name: str = ""
    status: str = "Full-Time"
    parent1_name: str = ""
    parent2_name: str = ""
    email: str = ""
    address_line1: str = ""
    address_line2: str = ""
    phone1: str = ""
    phone2: str = ""
    active: bool = True


@dataclass(frozen=True)
class Payment:
    payment_date: date
    amount_cents: int
    marker: str = ""
    row_order: int = 0


@dataclass(frozen=True)
class ReceiptRecord:
    id: int
    profile_id: int
    child_name: str
    receipt_month: int
    receipt_year: int
    pdf_path: str
    note: str
    total_cents: int
    generated_at: str


DEFAULT_SETTINGS = {
    "business_name": "Your Business Name",
    "business_address_line1": "",
    "business_address_line2": "",
    "business_phone": "",
    "business_email": "",
    "filename_token": "Protected",
}


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def month_name(month: int) -> str:
    if month < 1 or month > 12:
        raise ValueError("Month must be between 1 and 12.")
    return MONTH_NAMES[month - 1]


def parse_payment_date(raw_value: str) -> date:
    value = raw_value.strip()
    if not value:
        raise ValueError("Payment date is required.")

    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue

    raise ValueError(f"Invalid payment date: {raw_value}")


def parse_money_to_cents(raw_value: str) -> int:
    value = raw_value.strip().replace("$", "").replace(",", "")
    if not value:
        raise ValueError("Payment amount is required.")

    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid payment amount: {raw_value}") from exc

    if amount <= 0:
        raise ValueError("Payment amount must be greater than zero.")

    cents = (amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def format_money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def format_payment_date(value: date) -> str:
    return value.strftime("%m/%d/%Y")


def sanitize_filename_part(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "Receipt"


def receipt_relative_path(child_name: str, token: str, month: int, year: int) -> Path:
    filename = (
        f"{sanitize_filename_part(child_name)} - Payment Receipt - "
        f"{sanitize_filename_part(token)} - {month_name(month)} {year}.pdf"
    )
    return Path("receipts") / str(year) / filename
