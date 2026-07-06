from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from spring_flowers_receipts.models import Payment, Profile, format_money, parse_payment_date, parse_money_to_cents
from spring_flowers_receipts.paths import APP_DIR_ENV, get_paths
from spring_flowers_receipts.pdf_generator import generate_receipt_pdf
from spring_flowers_receipts.storage import ReceiptStore


def main() -> None:
    tmp_root = ROOT / "tmp" / "smoke_portable_app"
    if tmp_root.exists():
        shutil.rmtree(tmp_root)
    tmp_root.mkdir(parents=True)
    (tmp_root / "assets").mkdir()
    shutil.copy2(ROOT / "assets" / "logo.png", tmp_root / "assets" / "logo.png")
    os.environ[APP_DIR_ENV] = str(tmp_root)

    paths = get_paths()
    store = ReceiptStore(paths)

    profile = Profile(
        child_name="Adaure Parks",
        status="Part-Time",
        parent1_name="Brett Parks",
        parent2_name="Jacqueline Parks",
        address_line1="7096 44th PL West",
        address_line2="Mukilteo, WA 98275",
        phone1="(773) 573-8876",
        phone2="(360) 590-4556",
    )
    profile_id = store.save_profile(profile)
    saved = store.get_profile(profile_id)
    assert saved is not None

    payments = [
        Payment(parse_payment_date("04/06/2026"), parse_money_to_cents("375"), row_order=1),
        Payment(parse_payment_date("04/06/2026"), parse_money_to_cents("50"), row_order=2),
        Payment(parse_payment_date("04/13/2026"), parse_money_to_cents("425"), row_order=3),
        Payment(parse_payment_date("04/21/2026"), parse_money_to_cents("425"), row_order=4),
        Payment(parse_payment_date("04/28/2026"), parse_money_to_cents("850"), marker="*", row_order=5),
    ]
    total = sum(payment.amount_cents for payment in payments)
    assert format_money(total) == "$2,125.00"

    note = "Payment 5 dated Apr 28th, 2026 is for 4 days of care in April 2025 and 6 days in May 2026"
    pdf_path = paths.app_dir / "receipts" / "2026" / "Adaure - smoke.pdf"
    generate_receipt_pdf(
        output_path=pdf_path,
        profile=saved,
        settings=store.get_settings(),
        receipt_month=4,
        receipt_year=2026,
        payments=payments,
        note=note,
        logo_path=paths.logo_path,
    )
    assert pdf_path.exists() and pdf_path.stat().st_size > 10_000
    assert_pdf_edit_protected(pdf_path)
    store.save_receipt(profile_id, 4, 2026, Path("receipts") / "2026" / "Adaure - smoke.pdf", note, payments)
    assert store.receipt_exists(profile_id, 4, 2026)
    assert len(store.receipt_history()) == 1

    no_note_path = paths.app_dir / "receipts" / "2026" / "Adaure - no-note.pdf"
    generate_receipt_pdf(
        output_path=no_note_path,
        profile=saved,
        settings=store.get_settings(),
        receipt_month=5,
        receipt_year=2026,
        payments=payments[:2],
        note="",
        logo_path=paths.logo_path,
    )
    assert no_note_path.exists() and no_note_path.stat().st_size > 10_000

    for bad_date in ("4/28//2026", "", "13/1/2026"):
        try:
            parse_payment_date(bad_date)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Bad date accepted: {bad_date}")

    for bad_amount in ("", "-1", "0", "abc"):
        try:
            parse_money_to_cents(bad_amount)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Bad amount accepted: {bad_amount}")

    render_pdf(pdf_path, ROOT / "tmp" / "pdfs" / "smoke_receipt")
    print(f"Smoke test passed: {pdf_path}")


def render_pdf(pdf_path: Path, output_prefix: Path) -> None:
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    poppler = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "native"
        / "poppler"
        / "Library"
        / "bin"
        / "pdftoppm.exe"
    )
    if not poppler.exists():
        print("Poppler render skipped; pdftoppm.exe not found.")
        return
    subprocess.run([str(poppler), "-png", str(pdf_path), str(output_prefix)], check=True)


def assert_pdf_edit_protected(pdf_path: Path) -> None:
    pdfinfo = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "native"
        / "poppler"
        / "Library"
        / "bin"
        / "pdfinfo.exe"
    )
    if not pdfinfo.exists():
        print("PDF permission check skipped; pdfinfo.exe not found.")
        return

    result = subprocess.run([str(pdfinfo), str(pdf_path)], check=True, capture_output=True, text=True)
    info = result.stdout
    assert "Encrypted:       yes" in info
    assert "change:no" in info
    assert "addNotes:no" in info


if __name__ == "__main__":
    main()
