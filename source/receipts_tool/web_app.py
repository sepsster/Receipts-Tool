from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image, UnidentifiedImageError

from . import __version__
from .models import (
    MONTH_NAMES,
    Payment,
    Profile,
    format_money,
    month_name,
    parse_money_to_cents,
    parse_payment_date,
    receipt_relative_path,
)
from .paths import AppPaths, get_paths
from .pdf_generator import MAX_PAYMENT_ROWS, generate_receipt_pdf
from .storage import ReceiptStore
from .updater import UpdateCheckState, UpdateError, prepare_self_update


MAX_LOGO_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_LOGO_DIMENSION = 1600


def main() -> None:
    if "--self-test" in sys.argv:
        run_self_test()
        return

    no_browser = "--no-browser" in sys.argv
    port_file = _arg_value("--port-file")
    paths = get_paths()
    ensure_persistent_logo(paths)
    store = ReceiptStore(paths)
    update_checker = UpdateCheckState(paths)
    update_checker.start()
    last_heartbeat = {"value": time.monotonic()}
    handler = make_handler(paths, store, last_heartbeat, update_checker)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(f"Receipts Tool is running at {url}")
    if port_file:
        Path(port_file).write_text(url, encoding="utf-8")
    if not no_browser:
        webbrowser.open(url)
    threading.Thread(
        target=_shutdown_after_browser_close,
        args=(server, last_heartbeat),
        daemon=True,
    ).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _shutdown_after_browser_close(server: ThreadingHTTPServer, last_heartbeat: dict[str, float]) -> None:
    while True:
        time.sleep(15)
        if time.monotonic() - last_heartbeat["value"] > 120:
            server.shutdown()
            return


def _arg_value(flag: str) -> str | None:
    if flag not in sys.argv:
        return None
    index = sys.argv.index(flag)
    if index + 1 >= len(sys.argv):
        return None
    return sys.argv[index + 1]


def run_self_test() -> None:
    from .paths import APP_DIR_ENV

    original_logo = get_paths().logo_path
    tmp = tempfile.mkdtemp(prefix="receipts_tool_")
    try:
        os.environ[APP_DIR_ENV] = tmp
        tmp_path = Path(tmp)
        (tmp_path / "assets").mkdir(parents=True, exist_ok=True)
        if original_logo.exists():
            shutil.copy2(original_logo, tmp_path / "assets" / "logo.png")

        paths = get_paths()
        store = ReceiptStore(paths)
        profile_id = store.save_profile(
            Profile(
                child_name="Self Test Child",
                status="Part-Time",
                parent1_name="Self Test Parent",
                email="selftest@example.com",
                address_line1="123 Test St",
                address_line2="Anytown, WA 98000",
                phone1="(555) 010-1000",
            )
        )
        profile = store.get_profile(profile_id)
        if profile is None:
            raise RuntimeError("Self-test profile was not saved.")
        payments = [
            Payment(parse_payment_date("04/06/2026"), parse_money_to_cents("375"), row_order=1),
            Payment(parse_payment_date("04/13/2026"), parse_money_to_cents("400"), row_order=2),
        ]
        relative_path = receipt_relative_path(profile.child_name, "Protected", 4, 2026)
        output_path = paths.app_dir / relative_path
        generate_receipt_pdf(
            output_path=output_path,
            profile=profile,
            settings=store.get_settings(),
            receipt_month=4,
            receipt_year=2026,
            payments=payments,
            note="Self-test receipt.",
            logo_path=paths.logo_path,
        )
        if not output_path.exists() or output_path.stat().st_size < 10_000:
            raise RuntimeError("Self-test PDF was not generated correctly.")
        store.save_receipt(profile_id, 4, 2026, relative_path, "Self-test receipt.", payments)
        if not store.receipt_history():
            raise RuntimeError("Self-test receipt history was not saved.")
        print("Receipts Tool self-test passed.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def make_handler(
    paths: AppPaths,
    store: ReceiptStore,
    last_heartbeat: dict[str, float] | None = None,
    update_checker: UpdateCheckState | None = None,
):
    class ReceiptsToolHandler(BaseHTTPRequestHandler):
        server_version = "ReceiptsTool/0.1"

        def log_message(self, _format: str, *_args) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._mark_heartbeat()
                self._send_html(APP_HTML)
                return
            if parsed.path == "/api/profiles":
                self._send_json([profile_to_json(profile) for profile in store.profiles()])
                return
            if parsed.path == "/api/settings":
                self._send_json(store.get_settings())
                return
            if parsed.path == "/api/history":
                self._send_json([receipt_to_json(receipt) for receipt in store.receipt_history()])
                return
            if parsed.path == "/api/meta":
                self._send_json(
                    {
                        "months": MONTH_NAMES,
                        "currentYear": date.today().year,
                        "currentMonth": date.today().month,
                        "maxPaymentRows": MAX_PAYMENT_ROWS,
                        "appDir": str(paths.app_dir),
                        "receiptsDir": str(paths.receipts_dir),
                        "appVersion": __version__,
                        "update": update_checker.snapshot() if update_checker else {},
                    }
                )
                return
            if parsed.path == "/api/update-status":
                self._send_json(update_checker.snapshot() if update_checker else {})
                return
            if parsed.path == "/logo":
                self._serve_logo_file()
                return
            if parsed.path == "/receipt-pdf":
                self._serve_receipt_file(parsed.query)
                return
            self._send_error(HTTPStatus.NOT_FOUND, "Not found.")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/profiles":
                    self._save_profile()
                    return
                if parsed.path == "/api/settings":
                    self._save_settings()
                    return
                if parsed.path == "/api/logo":
                    self._save_logo()
                    return
                if parsed.path == "/api/generate":
                    self._generate_receipt()
                    return
                if parsed.path == "/api/open":
                    self._open_requested_path()
                    return
                if parsed.path == "/api/heartbeat":
                    self._mark_heartbeat()
                    self._send_json({"ok": True})
                    return
                if parsed.path == "/api/delete-receipt":
                    self._delete_receipt()
                    return
                if parsed.path == "/api/update":
                    self._update_app()
                    return
                if parsed.path == "/api/check-update":
                    self._check_update()
                    return
                if parsed.path == "/api/shutdown":
                    self._send_json({"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                self._send_error(HTTPStatus.NOT_FOUND, "Not found.")
            except ValueError as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            except UpdateError as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception as exc:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def _save_profile(self) -> None:
            data = self._read_json()
            raw_id = data.get("id")
            profile = Profile(
                id=int(raw_id) if raw_id else None,
                child_name=str(data.get("child_name", "")),
                status=str(data.get("status", "")),
                parent1_name=str(data.get("parent1_name", "")),
                parent2_name=str(data.get("parent2_name", "")),
                email=str(data.get("email", "")),
                address_line1=str(data.get("address_line1", "")),
                address_line2=str(data.get("address_line2", "")),
                phone1=str(data.get("phone1", "")),
                phone2=str(data.get("phone2", "")),
                active=bool(data.get("active", True)),
            )
            profile_id = store.save_profile(profile)
            saved = store.get_profile(profile_id)
            self._send_json(profile_to_json(saved) if saved else {"id": profile_id})

        def _save_settings(self) -> None:
            data = self._read_json()
            store.save_settings({key: str(value) for key, value in data.items()})
            self._send_json(store.get_settings())

        def _generate_receipt(self) -> None:
            data = self._read_json()
            profile_id = int(data.get("profile_id") or 0)
            profile = store.get_profile(profile_id)
            if profile is None or not profile.active:
                raise ValueError("Select an active profile first.")

            receipt_month = int(data.get("month") or 0)
            receipt_year = int(data.get("year") or 0)
            if receipt_month < 1 or receipt_month > 12:
                raise ValueError("Select a valid receipt month.")
            if receipt_year < 2020 or receipt_year > 2035:
                raise ValueError("Receipt year must be between 2020 and 2035.")

            payments = parse_payments(data.get("payments") or [], receipt_month, receipt_year)
            note = str(data.get("note", "")).strip()
            settings = store.get_settings()
            relative_path = receipt_relative_path(
                profile.child_name,
                settings.get("filename_token", "Protected"),
                receipt_month,
                receipt_year,
            )
            output_path = paths.app_dir / relative_path
            replace = bool(data.get("replace", False))
            if (output_path.exists() or store.receipt_exists(profile_id, receipt_month, receipt_year)) and not replace:
                self._send_json(
                    {
                        "error": (
                            f"A receipt already exists for {profile.child_name} "
                            f"in {month_name(receipt_month)} {receipt_year}."
                        ),
                        "replaceRequired": True,
                    },
                    status=HTTPStatus.CONFLICT,
                )
                return

            generate_receipt_pdf(
                output_path=output_path,
                profile=profile,
                settings=settings,
                receipt_month=receipt_month,
                receipt_year=receipt_year,
                payments=payments,
                note=note,
                logo_path=current_logo_path(paths),
            )
            store.save_receipt(profile_id, receipt_month, receipt_year, relative_path, note, payments)
            open_path(output_path)
            self._send_json(
                {
                    "ok": True,
                    "pdfPath": str(output_path),
                    "relativePath": str(relative_path),
                    "pdfUrl": f"/receipt-pdf?path={quote_path(relative_path)}",
                    "total": format_money(sum(payment.amount_cents for payment in payments)),
                }
            )

        def _open_requested_path(self) -> None:
            data = self._read_json()
            kind = str(data.get("kind", "receipts"))
            if kind == "receipts":
                paths.receipts_dir.mkdir(parents=True, exist_ok=True)
                open_path(paths.receipts_dir)
                self._send_json({"ok": True})
                return

            relative = Path(str(data.get("path", "")))
            target = safe_receipt_path(paths, relative)
            if not target.exists():
                raise ValueError("The selected file was not found.")
            open_path(target.parent if kind == "folder" else target)
            self._send_json({"ok": True})

        def _delete_receipt(self) -> None:
            data = self._read_json()
            receipt_id = int(data.get("id") or 0)
            relative_path = store.delete_receipt(receipt_id)
            if relative_path is None:
                raise ValueError("Receipt history item was not found.")
            target = safe_receipt_path(paths, Path(relative_path))
            if target.exists():
                target.unlink()
            self._send_json({"ok": True})

        def _update_app(self) -> None:
            checked_update = update_checker.take_downloaded_update() if update_checker else None
            result = prepare_self_update(paths, checked_update)
            self._send_json({"ok": True, **result})
            if result.get("restartRequired"):
                threading.Thread(target=self.server.shutdown, daemon=True).start()

        def _check_update(self) -> None:
            self._send_json(update_checker.check_now() if update_checker else {})

        def _save_logo(self) -> None:
            data = self._read_json()
            logo_path = save_logo_upload(paths, str(data.get("image", "")))
            self._send_json({"ok": True, "logoPath": str(logo_path), "logoUrl": f"/logo?ts={int(time.time())}"})

        def _serve_logo_file(self) -> None:
            target = current_logo_path(paths)
            if not target.exists():
                self._send_error(HTTPStatus.NOT_FOUND, "Logo image not found.")
                return
            data = target.read_bytes()
            content_type = mimetypes.guess_type(target.name)[0] or "image/png"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _serve_receipt_file(self, query: str) -> None:
            params = parse_qs(query)
            raw_path = unquote((params.get("path") or [""])[0])
            target = safe_receipt_path(paths, Path(raw_path))
            if not target.exists():
                self._send_error(HTTPStatus.NOT_FOUND, "Receipt PDF not found.")
                return
            data = target.read_bytes()
            content_type = mimetypes.guess_type(target.name)[0] or "application/pdf"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'inline; filename="{target.name}"')
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw)

        def _send_html(self, html: str) -> None:
            payload = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            self._send_json({"error": message}, status=status)

        def _mark_heartbeat(self) -> None:
            if last_heartbeat is not None:
                last_heartbeat["value"] = time.monotonic()

    return ReceiptsToolHandler


def parse_payments(raw_payments: list[dict], receipt_month: int, receipt_year: int) -> list[Payment]:
    payments: list[Payment] = []
    for index, row in enumerate(raw_payments, start=1):
        raw_date = str(row.get("date", "")).strip()
        raw_amount = str(row.get("amount", "")).strip()
        raw_marker = str(row.get("marker", "")).strip()
        if not (raw_date or raw_amount or raw_marker):
            continue
        try:
            payment_date = parse_payment_date(raw_date)
            if payment_date.month != receipt_month or payment_date.year != receipt_year:
                raise ValueError(
                    f"Payment date must be in {month_name(receipt_month)} {receipt_year}. "
                    "Check the selected receipt month/year or the payment date."
                )
            payments.append(
                Payment(
                    payment_date=payment_date,
                    amount_cents=parse_money_to_cents(raw_amount),
                    marker=raw_marker,
                    row_order=index,
                )
            )
        except ValueError as exc:
            raise ValueError(f"Payment row {index}: {exc}") from exc

    if not payments:
        raise ValueError("Add at least one payment before generating a receipt.")
    if len(payments) > MAX_PAYMENT_ROWS:
        raise ValueError(f"Receipts can include at most {MAX_PAYMENT_ROWS} payment rows.")
    return payments


def current_logo_path(paths: AppPaths) -> Path:
    custom_logo = paths.assets_dir / "logo.png"
    if custom_logo.exists():
        return custom_logo
    return paths.logo_path


def ensure_persistent_logo(paths: AppPaths) -> Path:
    custom_logo = paths.assets_dir / "logo.png"
    if custom_logo.exists():
        return custom_logo

    bundled_logo = paths.logo_path
    if not bundled_logo.exists():
        return custom_logo

    try:
        if bundled_logo.resolve() == custom_logo.resolve():
            return custom_logo
    except OSError:
        pass

    paths.assets_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundled_logo, custom_logo)
    return custom_logo


def save_logo_upload(paths: AppPaths, data_url: str) -> Path:
    if not data_url.startswith("data:image/"):
        raise ValueError("Choose a PNG, JPG, or other standard image file.")
    try:
        _header, encoded = data_url.split(",", 1)
    except ValueError as exc:
        raise ValueError("The logo upload was not a valid image.") from exc

    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("The logo upload could not be decoded.") from exc

    if not raw:
        raise ValueError("Choose a logo image first.")
    if len(raw) > MAX_LOGO_UPLOAD_BYTES:
        raise ValueError("Logo images must be smaller than 8 MB.")

    try:
        with Image.open(BytesIO(raw)) as check:
            check.verify()
        with Image.open(BytesIO(raw)) as image:
            if image.width < 1 or image.height < 1:
                raise ValueError("The logo image is empty.")
            image.thumbnail((MAX_LOGO_DIMENSION, MAX_LOGO_DIMENSION), Image.LANCZOS)
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA")
            paths.assets_dir.mkdir(parents=True, exist_ok=True)
            logo_path = paths.assets_dir / "logo.png"
            image.save(logo_path, format="PNG")
            return logo_path
    except UnidentifiedImageError as exc:
        raise ValueError("The selected file was not a readable image.") from exc


def profile_to_json(profile: Profile | None) -> dict:
    if profile is None:
        return {}
    return {
        "id": profile.id,
        "child_name": profile.child_name,
        "status": profile.status,
        "parent1_name": profile.parent1_name,
        "parent2_name": profile.parent2_name,
        "email": profile.email,
        "address_line1": profile.address_line1,
        "address_line2": profile.address_line2,
        "phone1": profile.phone1,
        "phone2": profile.phone2,
        "active": profile.active,
    }


def receipt_to_json(receipt) -> dict:
    return {
        "id": receipt.id,
        "profile_id": receipt.profile_id,
        "child_name": receipt.child_name,
        "receipt_month": receipt.receipt_month,
        "receipt_year": receipt.receipt_year,
        "period": f"{month_name(receipt.receipt_month)} {receipt.receipt_year}",
        "pdf_path": receipt.pdf_path,
        "note": receipt.note,
        "total": format_money(receipt.total_cents),
        "generated_at": receipt.generated_at,
        "pdf_url": f"/receipt-pdf?path={quote_path(Path(receipt.pdf_path))}",
    }


def quote_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace(" ", "%20")


def safe_receipt_path(paths: AppPaths, relative: Path) -> Path:
    if relative.is_absolute():
        raise ValueError("Absolute receipt paths are not allowed.")
    target = (paths.app_dir / relative).resolve()
    receipts_root = paths.receipts_dir.resolve()
    try:
        target.relative_to(receipts_root)
    except ValueError as exc:
        raise ValueError("Receipt path is outside the receipts folder.") from exc
    return target


def open_path(path: Path) -> None:
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except AttributeError:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
    except OSError:
        pass


APP_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Receipts Tool</title>
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect x='10' y='4' width='44' height='56' rx='10' fill='%232e6b4f'/%3E%3Crect x='20' y='18' width='24' height='6' rx='3' fill='%23e9f2ec'/%3E%3Crect x='20' y='30' width='24' height='5' rx='2.5' fill='%23a8cbb7'/%3E%3Crect x='20' y='41' width='15' height='5' rx='2.5' fill='%23a8cbb7'/%3E%3C/svg%3E">
  <style>
    :root {
      /* Warm neutrals */
      --bg: #f7f6f3;
      --surface: #ffffff;
      --surface-2: #f1efe9;
      --line: #e6e3db;
      --line-strong: #d2cec4;
      --ink: #20281f;
      --ink-2: #46514a;
      --muted: #5e6b64;
      /* Brand — botanical green */
      --brand: #2e6b4f;
      --brand-hover: #275c43;
      --brand-active: #1f4c37;
      --brand-tint: #e9f2ec;
      --brand-tint-2: #d7e7dd;
      /* Semantic */
      --success: #1e7a46;
      --success-bg: #e6f4ea;
      --warning: #9a6700;
      --warning-bg: #fff3d6;
      --danger: #b3261e;
      --danger-bg: #fdecea;
      /* Depth & shape */
      --shadow-sm: 0 1px 2px rgba(32, 40, 31, .06);
      --shadow-md: 0 4px 14px rgba(32, 40, 31, .08);
      --shadow-lg: 0 18px 44px rgba(32, 40, 31, .18);
      --radius-sm: 8px;
      --radius-md: 12px;
      --radius-lg: 16px;
      --ring: 0 0 0 3px rgba(46, 107, 79, .25);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, sans-serif;
      font-size: 14px;
      line-height: 1.5;
      color: var(--ink);
      background: var(--bg);
      -webkit-font-smoothing: antialiased;
      accent-color: var(--brand);
    }
    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-thumb { background: var(--line-strong); border-radius: 999px; border: 2px solid var(--bg); }
    svg { flex-shrink: 0; }

    /* Top bar */
    header.topbar {
      display: flex;
      justify-content: space-between;
      gap: 24px;
      align-items: center;
      padding: 18px 28px 14px;
    }
    .brand { display: flex; gap: 14px; align-items: center; min-width: 0; }
    .brand-mark {
      width: 40px;
      height: 40px;
      border-radius: var(--radius-md);
      background: var(--brand-tint);
      color: var(--brand);
      display: grid;
      place-items: center;
    }
    h1 { margin: 0; font-size: 20px; font-weight: 700; letter-spacing: -.01em; }
    .version-chip {
      font-size: 11px;
      font-weight: 600;
      color: var(--brand);
      background: var(--brand-tint);
      padding: 2px 8px;
      border-radius: 999px;
      vertical-align: 2px;
      margin-left: 8px;
    }
    .sub { color: var(--muted); font-size: 13px; }
    .banner {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin: 0 28px 12px;
      padding: 10px 14px;
      background: var(--danger-bg);
      border: 1px solid #f3c2be;
      border-radius: var(--radius-md);
      color: var(--danger);
      font-weight: 600;
    }
    .shell { padding: 0 28px 32px; }

    /* Segmented tabs */
    .tabs {
      display: inline-flex;
      gap: 4px;
      padding: 4px;
      background: var(--surface-2);
      border: 1px solid var(--line);
      border-radius: 999px;
      max-width: 100%;
      overflow-x: auto;
    }
    .tab {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border: 0;
      background: transparent;
      padding: 8px 16px;
      border-radius: 999px;
      font: inherit;
      font-weight: 600;
      color: var(--muted);
      cursor: pointer;
      white-space: nowrap;
      transition: background .15s ease, color .15s ease;
    }
    .tab:hover:not(.active) { color: var(--ink); }
    .tab.active { background: var(--surface); color: var(--brand); box-shadow: var(--shadow-sm); }
    .tab:focus-visible { outline: none; box-shadow: var(--ring); }
    .tab-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--warning);
      animation: pulse 2s ease-in-out infinite;
    }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }

    .view { display: none; padding-top: 18px; }
    .view.active { display: block; animation: viewIn .18s ease-out; }
    @keyframes viewIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
    .stack { display: grid; gap: 16px; }
    .grid { display: grid; grid-template-columns: minmax(280px, 0.9fr) minmax(360px, 1.6fr); gap: 16px; align-items: start; }

    .panel {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow-sm);
      padding: 20px;
      min-width: 0;
    }
    .panel h2 {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 0 0 14px;
      font-size: 16px;
      font-weight: 650;
      letter-spacing: -.01em;
    }
    .panel h2 > svg { color: var(--brand); }
    .panel-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
    .panel-head h2 { margin-bottom: 0; }
    .panel-head + .list, .panel-head + .table-wrap { margin-top: 14px; }
    .section-title { margin-top: 26px !important; }

    label { display: block; font-size: 13px; font-weight: 600; color: var(--ink-2); margin: 14px 0 6px; }
    input, select, textarea {
      width: 100%;
      border: 1px solid var(--line-strong);
      border-radius: 10px;
      padding: 9px 12px;
      font: inherit;
      color: var(--ink);
      background: var(--surface);
      transition: border-color .15s ease, box-shadow .15s ease;
    }
    input:hover, select:hover, textarea:hover { border-color: var(--muted); }
    input:focus, select:focus, textarea:focus { outline: none; border-color: var(--brand); box-shadow: var(--ring); }
    input::placeholder, textarea::placeholder { color: #9aa59e; }
    input[type="file"] { padding: 8px; }
    input[type="file"]::file-selector-button {
      font: inherit;
      font-weight: 600;
      border: 1px solid var(--line-strong);
      border-radius: var(--radius-sm);
      padding: 6px 12px;
      margin-right: 12px;
      background: var(--surface);
      color: var(--ink);
      cursor: pointer;
    }
    input[type="file"]::file-selector-button:hover { background: var(--surface-2); }
    textarea { resize: vertical; min-height: 88px; }
    .row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .actions { display: flex; gap: 10px; justify-content: flex-end; align-items: center; margin-top: 18px; flex-wrap: wrap; }

    /* Buttons */
    button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      border: 1px solid var(--line-strong);
      border-radius: 10px;
      padding: 9px 14px;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
      background: var(--surface);
      color: var(--ink);
      transition: background .15s ease, border-color .15s ease, box-shadow .15s ease, color .15s ease;
    }
    button:hover { background: var(--surface-2); }
    button:focus-visible { outline: none; box-shadow: var(--ring); }
    button.primary, button.success { background: var(--brand); border-color: var(--brand); color: #fff; }
    button.primary:hover, button.success:hover { background: var(--brand-hover); border-color: var(--brand-hover); }
    button.primary:active, button.success:active { background: var(--brand-active); }
    button.warn { background: var(--warning-bg); border-color: #ecd9a0; color: #6c4d08; }
    button.danger { background: transparent; border-color: transparent; color: var(--danger); }
    button.danger:hover { background: var(--danger-bg); }
    button.danger-solid { background: var(--danger); border-color: var(--danger); color: #fff; }
    button.danger-solid:hover { background: #9a1f18; border-color: #9a1f18; }
    button.ghost { background: transparent; border-color: transparent; color: var(--muted); }
    button.ghost:hover { background: var(--surface-2); color: var(--ink); }
    button.ghost.quit:hover { background: var(--danger-bg); color: var(--danger); }
    button.dashed { background: transparent; border: 1px dashed var(--line-strong); color: var(--brand); }
    button.dashed:hover { background: var(--brand-tint); border-color: var(--brand); }
    button.icon-btn { padding: 7px; border-color: transparent; background: transparent; color: var(--muted); border-radius: var(--radius-sm); }
    button.icon-btn:hover { background: var(--surface-2); color: var(--ink); }
    button.icon-btn.danger-hover:hover { background: var(--danger-bg); color: var(--danger); }
    button:disabled { cursor: not-allowed; opacity: .5; }

    /* Profile list */
    .list { display: grid; gap: 8px; }
    .list > button {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) auto;
      align-items: center;
      gap: 12px;
      width: 100%;
      text-align: left;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius-md);
      background: var(--surface);
      font-weight: 500;
    }
    .list > button:hover { background: var(--brand-tint); border-color: var(--brand-tint-2); }
    .list > button.selected { border-color: var(--brand); background: var(--brand-tint); }
    .list > button.selected .avatar { background: var(--brand); color: #fff; }
    .selected-check { color: var(--brand); }
    .avatar {
      width: 36px;
      height: 36px;
      border-radius: 50%;
      background: var(--brand-tint-2);
      color: var(--brand);
      font-size: 13px;
      font-weight: 700;
      display: grid;
      place-items: center;
    }
    .profile-body { display: grid; gap: 2px; min-width: 0; }
    .profile-title { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
    .pill {
      display: inline-block;
      padding: 2px 9px;
      border-radius: 999px;
      font-size: 11.5px;
      font-weight: 600;
      background: var(--surface-2);
      color: var(--ink-2);
    }
    .pill.active-pill { background: var(--success-bg); color: var(--success); }
    .pill.selected-pill { background: var(--brand); color: #fff; }
    .pill.inactive-pill { background: var(--surface-2); color: var(--muted); }
    .inactive { opacity: .6; }
    .check-row { display: flex; align-items: center; gap: 8px; margin-top: 16px; font-size: 14px; font-weight: 500; color: var(--ink); }
    .check-row input { width: 16px; height: 16px; }

    /* Payments */
    .payments-well {
      background: var(--surface-2);
      border: 1px solid var(--line);
      border-radius: var(--radius-md);
      padding: 14px;
    }
    .payments-well .dashed { width: 100%; margin-top: 4px; }
    .payment-grid {
      display: grid;
      grid-template-columns: 28px 32px minmax(140px, 1fr) minmax(130px, 180px) 38px;
      gap: 8px;
      align-items: center;
      margin-bottom: 8px;
    }
    .payment-grid input[type="checkbox"] { width: 16px; height: 16px; justify-self: center; }
    .payment-head {
      color: var(--muted);
      font-weight: 650;
      font-size: 11.5px;
      text-transform: uppercase;
      letter-spacing: .05em;
      margin-bottom: 10px;
    }
    .payment-head > div:nth-child(2) { text-align: center; cursor: help; }
    .row-num { color: var(--muted); font-size: 13px; text-align: center; font-variant-numeric: tabular-nums; }
    .amount-wrap { position: relative; display: block; min-width: 0; }
    .amount-prefix { position: absolute; left: 12px; top: 50%; transform: translateY(-50%); color: var(--muted); pointer-events: none; }
    .amount-wrap input { padding-left: 26px; }
    .total-row {
      display: flex;
      justify-content: flex-end;
      align-items: baseline;
      gap: 12px;
      border-top: 1px solid var(--line-strong);
      margin-top: 12px;
      padding-top: 10px;
    }
    .total-label { color: var(--muted); font-size: 11.5px; font-weight: 650; text-transform: uppercase; letter-spacing: .05em; }
    .total { font-size: 22px; font-weight: 750; color: var(--brand); font-variant-numeric: tabular-nums; letter-spacing: -.01em; }

    /* Status lines */
    .status {
      min-height: 24px;
      color: var(--muted);
      margin-top: 12px;
      font-size: 13px;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .status.error { color: var(--danger); font-weight: 600; }
    .status.ok { color: var(--success); font-weight: 600; }
    .status.error::before, .status.ok::before {
      content: "";
      width: 15px;
      height: 15px;
      flex-shrink: 0;
      background: currentColor;
      -webkit-mask: var(--status-icon) center / contain no-repeat;
      mask: var(--status-icon) center / contain no-repeat;
    }
    .status.ok { --status-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='10'/%3E%3Cpolyline points='8 12.5 11 15.5 16 9.5'/%3E%3C/svg%3E"); }
    .status.error { --status-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='10'/%3E%3Cline x1='12' y1='7.5' x2='12' y2='13'/%3E%3Ccircle cx='12' cy='16.5' r='.5'/%3E%3C/svg%3E"); }
    .footer-actions { display: flex; gap: 10px; justify-content: space-between; align-items: center; margin-top: 18px; flex-wrap: wrap; }

    /* Table */
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid var(--line); padding: 11px 10px; text-align: left; }
    th {
      color: var(--muted);
      font-size: 11.5px;
      font-weight: 650;
      text-transform: uppercase;
      letter-spacing: .05em;
      border-bottom-color: var(--line-strong);
    }
    tbody tr { transition: background .12s ease; }
    tbody tr:hover { background: var(--brand-tint); }
    td.money, th.money { text-align: right; font-variant-numeric: tabular-nums; }
    td.nowrap { white-space: nowrap; }
    .row-actions { display: flex; gap: 2px; justify-content: flex-end; }
    .pdf-cell a { color: var(--brand); font-weight: 600; text-decoration: none; overflow-wrap: anywhere; }
    .pdf-cell a:hover { text-decoration: underline; }

    /* Empty states */
    .empty {
      display: grid;
      justify-items: center;
      gap: 4px;
      padding: 30px 12px;
      text-align: center;
      color: var(--muted);
    }
    .empty svg { color: var(--line-strong); margin-bottom: 6px; }
    .empty strong { color: var(--ink-2); font-size: 14px; }
    .empty span { font-size: 13px; }

    /* Logo */
    .logo-row { display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
    .logo-preview {
      width: 116px;
      height: 116px;
      border: 1px solid var(--line);
      border-radius: var(--radius-md);
      object-fit: contain;
      background: var(--surface);
      padding: 10px;
      box-shadow: var(--shadow-sm);
    }

    /* Updates */
    .update-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid #ecd9a0;
      border-radius: 999px;
      padding: 3px 10px;
      margin-left: 4px;
      font-size: 11.5px;
      font-weight: 600;
      color: var(--warning);
      background: var(--warning-bg);
    }
    .update-badge .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--warning); animation: pulse 2s ease-in-out infinite; }
    .update-progress { display: flex; align-items: center; gap: 12px; margin: 12px 0 2px; max-width: 640px; }
    .progress-track {
      flex: 1;
      height: 8px;
      background: var(--surface-2);
      border: 1px solid var(--line);
      border-radius: 999px;
      overflow: hidden;
    }
    .progress-fill { height: 100%; width: 0; background: var(--brand); border-radius: 999px; transition: width .2s ease; }
    .progress-fill.indeterminate { width: 40%; animation: slide 1.2s ease-in-out infinite; }
    @keyframes slide { from { margin-left: -40%; } to { margin-left: 100%; } }
    .update-progress-label {
      font-size: 13px;
      color: var(--muted);
      min-width: 44px;
      text-align: right;
      font-variant-numeric: tabular-nums;
    }
    .spinner {
      width: 30px;
      height: 30px;
      border: 3px solid var(--line);
      border-top-color: var(--brand);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      margin: 18px 0;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .version-list {
      display: grid;
      gap: 0;
      margin: 12px 0 14px;
      max-width: 640px;
    }
    .version-row {
      display: grid;
      grid-template-columns: 150px minmax(0, 1fr);
      gap: 14px;
      align-items: center;
      border-top: 1px solid var(--line);
      padding: 9px 0;
    }
    .version-row:first-child { border-top: 0; }
    .version-label {
      color: var(--muted);
      font-size: 13px;
    }
    .version-value {
      color: var(--ink);
      font-weight: 650;
      overflow-wrap: anywhere;
      font-variant-numeric: tabular-nums;
    }

    /* Toast */
    .toast {
      position: fixed;
      right: 22px;
      bottom: 22px;
      z-index: 20;
      display: flex;
      gap: 12px;
      width: min(380px, calc(100vw - 44px));
      border: 1px solid var(--line);
      border-radius: var(--radius-md);
      background: var(--surface);
      box-shadow: var(--shadow-lg);
      padding: 16px;
      animation: toastIn .25s ease-out;
    }
    @keyframes toastIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }
    .toast-icon {
      width: 36px;
      height: 36px;
      border-radius: 50%;
      background: var(--brand-tint);
      color: var(--brand);
      display: grid;
      place-items: center;
    }
    .toast-title { font-weight: 700; margin-bottom: 2px; }
    .toast-body { color: var(--muted); line-height: 1.4; font-size: 13px; }
    .toast-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }

    /* Modal */
    .modal-overlay {
      position: fixed;
      inset: 0;
      z-index: 30;
      display: grid;
      place-items: center;
      background: rgba(32, 40, 31, .4);
      backdrop-filter: blur(2px);
      animation: fadeIn .15s ease-out;
      padding: 20px;
    }
    @keyframes fadeIn { from { opacity: 0; } }
    .modal {
      width: min(430px, 100%);
      background: var(--surface);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow-lg);
      padding: 22px;
      animation: modalIn .18s ease-out;
    }
    @keyframes modalIn { from { opacity: 0; transform: scale(.97) translateY(6px); } to { opacity: 1; transform: none; } }
    .modal h3 { margin: 0 0 8px; font-size: 17px; letter-spacing: -.01em; }
    .modal p { margin: 0; color: var(--muted); line-height: 1.5; }
    .modal-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 20px; }

    [hidden] { display: none !important; }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { animation: none !important; transition: none !important; }
    }
    @media (max-width: 860px) {
      header.topbar { align-items: flex-start; flex-direction: column; }
      .banner { margin: 0 16px 12px; }
      .shell { padding: 0 16px 24px; }
      .grid, .row { grid-template-columns: 1fr; }
      .version-row { grid-template-columns: 1fr; gap: 3px; }
      .payment-grid { grid-template-columns: 24px 28px minmax(110px, 1fr) minmax(110px, 1fr) 34px; }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand">
      <span class="brand-mark" aria-hidden="true">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3h14v18l-2.4-1.8-2.3 1.8-2.3-1.8L9.7 21l-2.3-1.8L5 21z"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="9" y1="12" x2="15" y2="12"/></svg>
      </span>
      <div>
        <h1>Receipts Tool<span class="version-chip" id="headerVersion" hidden></span></h1>
        <div class="sub">Profiles, payment receipts, and PDF history &mdash; all local. Closing this tab closes the app.</div>
      </div>
    </div>
    <button class="ghost quit" id="quitBtn"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>Close App</button>
  </header>
  <div class="banner" id="appError" hidden>
    <span id="appErrorText"></span>
    <button class="icon-btn" id="appErrorDismiss" aria-label="Dismiss error">&#10005;</button>
  </div>
  <main class="shell">
    <nav class="tabs">
      <button class="tab active" data-view="profiles"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>Profiles</button>
      <button class="tab" data-view="generate"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="18" x2="12" y2="12"/><line x1="9" y1="15" x2="15" y2="15"/></svg>Generate</button>
      <button class="tab" data-view="history"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>History</button>
      <button class="tab" data-view="settings"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>Settings<span class="tab-dot" id="settingsUpdateDot" hidden></span></button>
    </nav>

    <section id="profiles" class="view active">
      <div class="grid">
        <div class="panel">
          <div class="panel-head">
            <h2><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>Saved Profiles</h2>
            <button class="ghost" id="refreshProfilesBtn"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>Refresh</button>
          </div>
          <div id="profileList" class="list"></div>
          <div class="actions" style="justify-content:stretch">
            <button class="dashed" id="newProfileBtn" style="flex:1"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>New Profile</button>
          </div>
        </div>
        <div class="panel">
          <h2><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>Profile Details</h2>
          <input type="hidden" id="profileId">
          <div class="row">
            <div><label>Child name</label><input id="childName" autocomplete="off"></div>
            <div>
              <label>Status</label>
              <select id="status">
                <option value="Full-Time">Full-Time</option>
                <option value="Part-Time">Part-Time</option>
              </select>
            </div>
          </div>
          <div class="row">
            <div><label>Parent/guardian 1</label><input id="parent1"></div>
            <div><label>Parent/guardian 2</label><input id="parent2"></div>
          </div>
          <div><label>Email</label><input id="email" type="email" autocomplete="email"></div>
          <div class="row">
            <div><label>Address line 1</label><input id="address1"></div>
            <div><label>Address line 2</label><input id="address2"></div>
          </div>
          <div class="row">
            <div><label>Phone 1</label><input id="phone1"></div>
            <div><label>Phone 2</label><input id="phone2"></div>
          </div>
          <label class="check-row"><input id="active" type="checkbox" checked> Use this profile for receipts</label>
          <div class="actions">
            <button class="primary" id="saveProfileBtn">Save Profile</button>
          </div>
          <div id="profileStatus" class="status"></div>
        </div>
      </div>
    </section>

    <section id="generate" class="view">
      <div class="panel">
        <h2><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>Receipt Details</h2>
        <div class="row">
          <div><label>Profile</label><select id="receiptProfile"></select></div>
          <div class="row">
            <div><label>Month</label><select id="receiptMonth"></select></div>
            <div><label>Year</label><input id="receiptYear" type="number" min="2020" max="2035"></div>
          </div>
        </div>
        <h2 class="section-title"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>Payments</h2>
        <div class="payments-well">
          <div class="payment-grid payment-head"><div>#</div><div title="Check a row to print an asterisk that points to the note">&#65290;</div><div>Date</div><div>Amount</div><div></div></div>
          <div id="paymentRows"></div>
          <button class="dashed" id="addPaymentBtn"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>Add Payment</button>
          <div class="total-row"><span class="total-label">Total</span><span class="total" id="receiptTotal">$0.00</span></div>
        </div>
        <label>Optional Note</label>
        <textarea id="receiptNote" placeholder="Printed on the receipt next to the asterisk"></textarea>
        <div class="footer-actions">
          <button id="openReceiptsBtn"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>Open Receipts Folder</button>
          <button class="primary" id="generateBtn">Generate PDF</button>
        </div>
        <div id="generateStatus" class="status"></div>
      </div>
    </section>

    <section id="history" class="view">
      <div class="panel">
        <div class="panel-head">
          <h2><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>Generated Receipts</h2>
          <button class="ghost" id="refreshHistoryBtn"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>Refresh</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Generated</th><th>Child</th><th>Period</th><th class="money">Total</th><th>PDF</th><th></th></tr></thead>
            <tbody id="historyRows"></tbody>
          </table>
        </div>
      </div>
    </section>

    <section id="settings" class="view">
      <div class="stack">
        <div class="panel">
          <h2><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg>Business Defaults</h2>
          <div class="row">
            <div><label>Business name</label><input id="business_name"></div>
            <div><label>Filename token</label><input id="filename_token"></div>
          </div>
          <div class="row">
            <div><label>Address line 1</label><input id="business_address_line1"></div>
            <div><label>Address line 2</label><input id="business_address_line2"></div>
          </div>
          <div class="row">
            <div><label>Phone</label><input id="business_phone"></div>
            <div><label>Email</label><input id="business_email"></div>
          </div>
          <div class="actions"><button class="primary" id="saveSettingsBtn">Save Settings</button></div>
          <div id="settingsStatus" class="status"></div>
        </div>
        <div class="panel">
          <h2><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>Receipt Logo</h2>
          <div class="logo-row">
            <img class="logo-preview" id="logoPreview" src="/logo" alt="Current receipt logo">
            <div style="flex:1; min-width:240px">
              <label>Logo image</label>
              <input id="logoFile" type="file" accept="image/*">
            </div>
          </div>
          <div class="actions"><button class="primary" id="saveLogoBtn">Save Logo</button></div>
          <div id="logoStatus" class="status"></div>
        </div>
        <div class="panel">
          <h2><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>App Updates <span class="update-badge" id="updateBadge" hidden><span class="dot"></span>Update available</span></h2>
          <div class="version-list">
            <div class="version-row">
              <div class="version-label">Installed version</div>
              <div class="version-value" id="appVersion">-</div>
            </div>
            <div class="version-row">
              <div class="version-label">Latest version</div>
              <div class="version-value" id="latestVersion">-</div>
            </div>
            <div class="version-row">
              <div class="version-label">Update status</div>
              <div class="version-value" id="updateState">-</div>
            </div>
            <div class="version-row">
              <div class="version-label">Last checked</div>
              <div class="version-value" id="updateCheckedAt">-</div>
            </div>
          </div>
          <div class="sub" id="updateDescription"></div>
          <div class="update-progress" id="updateProgressWrap" hidden>
            <div class="progress-track"><div class="progress-fill" id="updateProgress"></div></div>
            <span class="update-progress-label" id="updateProgressLabel">0%</span>
          </div>
          <div class="actions" style="justify-content:flex-start">
            <button id="checkUpdateBtn"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>Check for Updates</button>
            <button class="primary" id="updateBtn">Install Update</button>
          </div>
          <div id="updateStatus" class="status"></div>
        </div>
      </div>
    </section>
  </main>

  <div class="toast" id="updateToast" hidden>
    <div class="toast-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="8 12 12 16 16 12"/><line x1="12" y1="8" x2="12" y2="16"/></svg></div>
    <div style="min-width:0; flex:1">
      <div class="toast-title">Update Available</div>
      <div class="toast-body">A newer version is ready to install from GitHub.</div>
      <div class="toast-actions">
        <button id="toastDismissBtn">Dismiss</button>
        <button class="primary" id="toastSettingsBtn">Open Settings</button>
      </div>
    </div>
  </div>

  <div class="modal-overlay" id="modalOverlay" hidden>
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
      <h3 id="modalTitle"></h3>
      <p id="modalBody"></p>
      <div class="modal-actions">
        <button id="modalCancelBtn">Cancel</button>
        <button class="primary" id="modalConfirmBtn">Confirm</button>
      </div>
    </div>
  </div>

  <script>
    const state = { profiles: [], history: [], meta: {}, paymentRows: [], updateNotified: false, selectedProfileId: null };
    const $ = (id) => document.getElementById(id);

    const ICONS = {
      trash: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>`,
      open: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>`,
      check: `<svg class="selected-check" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`,
      fileEmpty: `<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>`,
      usersEmpty: `<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>`
    };

    function initials(name) {
      const parts = String(name || "").trim().split(/\s+/).filter(Boolean);
      if (!parts.length) return "?";
      return parts.slice(0, 2).map(part => part[0].toUpperCase()).join("");
    }

    function baseName(path) {
      return String(path || "").split(/[\\/]/).pop();
    }

    function formatTimestamp(value) {
      const parsed = new Date(String(value || "").replace(" ", "T"));
      if (Number.isNaN(parsed.getTime())) return String(value || "");
      return parsed.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })
        + " · " + parsed.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    }

    function showAppError(message) {
      $("appErrorText").textContent = message;
      $("appError").hidden = false;
    }

    function showConfirm({ title, body, confirmLabel = "Confirm", cancelLabel = "Cancel", danger = false }) {
      return new Promise(resolve => {
        const overlay = $("modalOverlay");
        const confirmBtn = $("modalConfirmBtn");
        const cancelBtn = $("modalCancelBtn");
        $("modalTitle").textContent = title;
        $("modalBody").textContent = body;
        confirmBtn.textContent = confirmLabel;
        cancelBtn.textContent = cancelLabel;
        confirmBtn.className = danger ? "danger-solid" : "primary";
        const previousFocus = document.activeElement;
        function close(result) {
          overlay.hidden = true;
          document.removeEventListener("keydown", onKey);
          if (previousFocus && typeof previousFocus.focus === "function") previousFocus.focus();
          resolve(result);
        }
        function onKey(event) {
          if (event.key === "Escape") close(false);
        }
        confirmBtn.onclick = () => close(true);
        cancelBtn.onclick = () => close(false);
        overlay.onclick = (event) => { if (event.target === overlay) close(false); };
        document.addEventListener("keydown", onKey);
        overlay.hidden = false;
        confirmBtn.focus();
      });
    }

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(data.error || "Request failed.");
        error.payload = data;
        error.status = response.status;
        throw error;
      }
      return data;
    }

    function setStatus(id, text, kind = "") {
      const el = $(id);
      el.textContent = text;
      el.className = `status ${kind}`;
    }

    function showView(name) {
      document.querySelectorAll(".tab").forEach(btn => btn.classList.toggle("active", btn.dataset.view === name));
      document.querySelectorAll(".view").forEach(view => view.classList.toggle("active", view.id === name));
    }

    function normalizeStatus(value) {
      return value === "Part-Time" ? "Part-Time" : "Full-Time";
    }

    function blankProfile() {
      $("profileId").value = "";
      $("childName").value = "";
      $("status").value = "Full-Time";
      $("parent1").value = "";
      $("parent2").value = "";
      $("email").value = "";
      $("address1").value = "";
      $("address2").value = "";
      $("phone1").value = "";
      $("phone2").value = "";
      $("active").checked = true;
      state.selectedProfileId = null;
      renderProfiles();
      setStatus("profileStatus", "");
    }

    function loadProfile(profile) {
      state.selectedProfileId = Number(profile.id) || null;
      $("profileId").value = profile.id || "";
      $("childName").value = profile.child_name || "";
      $("status").value = normalizeStatus(profile.status);
      $("parent1").value = profile.parent1_name || "";
      $("parent2").value = profile.parent2_name || "";
      $("email").value = profile.email || "";
      $("address1").value = profile.address_line1 || "";
      $("address2").value = profile.address_line2 || "";
      $("phone1").value = profile.phone1 || "";
      $("phone2").value = profile.phone2 || "";
      $("active").checked = !!profile.active;
      renderProfiles();
    }

    async function loadProfiles() {
      state.profiles = await api("/api/profiles");
      renderProfiles();
      renderProfileOptions();
    }

    function renderProfiles() {
      const list = $("profileList");
      list.innerHTML = "";
      if (!state.profiles.length) {
        list.innerHTML = `<div class="empty">${ICONS.usersEmpty}<strong>No profiles yet</strong><span>Add a child profile to start generating receipts.</span></div>`;
        return;
      }
      state.profiles.forEach(profile => {
        const btn = document.createElement("button");
        btn.dataset.id = profile.id;
        const isSelected = Number(profile.id) === state.selectedProfileId;
        btn.className = [isSelected ? "selected" : "", profile.active ? "" : "inactive"].filter(Boolean).join(" ");
        const activeBadge = isSelected
          ? `<span class="pill selected-pill">Selected</span>`
          : (profile.active ? `<span class="pill active-pill">Active</span>` : `<span class="pill inactive-pill">Hidden</span>`);
        btn.innerHTML = `
          <span class="avatar" aria-hidden="true">${escapeHtml(initials(profile.child_name))}</span>
          <span class="profile-body">
            <span class="profile-title">
              <strong>${escapeHtml(profile.child_name)}</strong>
              <span class="pill">${escapeHtml(profile.status || "No status")}</span>
              ${activeBadge}
            </span>
            <span class="sub">${escapeHtml(profile.parent1_name || "")}</span>
          </span>
          ${isSelected ? ICONS.check : ""}
        `;
        btn.onclick = () => selectProfile(profile);
        list.appendChild(btn);
      });
    }

    async function selectProfile(profile) {
      const selectedProfile = { ...profile, active: true };
      const index = state.profiles.findIndex(item => Number(item.id) === Number(profile.id));
      if (index >= 0) state.profiles[index] = selectedProfile;
      loadProfile(selectedProfile);
      if (profile.active) return;

      try {
        const saved = await api("/api/profiles", { method: "POST", body: JSON.stringify(selectedProfile) });
        await loadProfiles();
        loadProfile(saved);
        setStatus("profileStatus", "Profile selected and active.", "ok");
      } catch (error) {
        if (index >= 0) state.profiles[index] = profile;
        loadProfile(profile);
        setStatus("profileStatus", error.message, "error");
      }
    }

    function renderProfileOptions() {
      const select = $("receiptProfile");
      const active = state.profiles.filter(p => p.active);
      select.innerHTML = active.map(p => `<option value="${p.id}">${escapeHtml(p.child_name)} (${escapeHtml(p.status || "Active")})</option>`).join("");
    }

    async function saveProfile() {
      try {
        const payload = {
          id: $("profileId").value || null,
          child_name: $("childName").value,
          status: $("status").value,
          parent1_name: $("parent1").value,
          parent2_name: $("parent2").value,
          email: $("email").value,
          address_line1: $("address1").value,
          address_line2: $("address2").value,
          phone1: $("phone1").value,
          phone2: $("phone2").value,
          active: $("active").checked
        };
        const saved = await api("/api/profiles", { method: "POST", body: JSON.stringify(payload) });
        await loadProfiles();
        loadProfile(saved);
        setStatus("profileStatus", "Profile saved.", "ok");
      } catch (error) {
        setStatus("profileStatus", error.message, "error");
      }
    }

    function addPaymentRow(values = {}) {
      if (state.paymentRows.length >= state.meta.maxPaymentRows) {
        setStatus("generateStatus", `Receipts can include at most ${state.meta.maxPaymentRows} payment rows.`, "error");
        return;
      }
      state.paymentRows.push({ marker: values.marker || "", date: values.date || "", amount: values.amount || "" });
      renderPaymentRows();
    }

    function formatPaymentDateInput(value) {
      const raw = String(value || "");
      if (raw.includes("/")) {
        const parts = raw.split("/");
        const month = (parts[0] || "").replace(/\D/g, "").slice(0, 2);
        let dayDigits = (parts[1] || "").replace(/\D/g, "");
        let yearDigits = parts.length > 2 ? parts.slice(2).join("").replace(/\D/g, "") : "";
        if (parts.length === 2 && dayDigits.length > 2) {
          yearDigits = dayDigits.slice(2);
          dayDigits = dayDigits.slice(0, 2);
        }
        const day = dayDigits.slice(0, 2);
        const year = yearDigits.slice(0, 4);
        if (parts.length > 2 || year) return `${month}/${day}/${year}`;
        if (parts.length > 1) return `${month}/${day}`;
        return month;
      }

      const digits = raw.replace(/\D/g, "").slice(0, 8);
      if (digits.length <= 2) return digits;
      if (digits.length <= 4) return `${digits.slice(0, 2)}/${digits.slice(2)}`;
      return `${digits.slice(0, 2)}/${digits.slice(2, 4)}/${digits.slice(4)}`;
    }

    function renderPaymentRows() {
      const root = $("paymentRows");
      root.innerHTML = "";
      state.paymentRows.forEach((row, index) => {
        const wrap = document.createElement("div");
        wrap.className = "payment-grid";
        wrap.innerHTML = `
          <div class="row-num">${index + 1}</div>
          <input type="checkbox" data-field="marker" data-index="${index}" ${row.marker ? "checked" : ""} title="Print an asterisk next to this payment, referencing the note">
          <input data-field="date" data-index="${index}" value="${escapeAttr(row.date)}" placeholder="mm/dd/yyyy" inputmode="numeric" maxlength="10" aria-label="Payment date">
          <span class="amount-wrap"><span class="amount-prefix">$</span><input data-field="amount" data-index="${index}" value="${escapeAttr(row.amount)}" placeholder="0.00" aria-label="Payment amount"></span>
          <button class="icon-btn danger-hover" data-remove="${index}" aria-label="Remove payment row" title="Remove row">${ICONS.trash}</button>
        `;
        root.appendChild(wrap);
      });
      root.querySelectorAll("input").forEach(input => {
        const syncInput = event => {
          const target = event.target;
          if (target.dataset.field === "date") {
            target.value = formatPaymentDateInput(target.value);
          }
          state.paymentRows[Number(target.dataset.index)][target.dataset.field] = target.type === "checkbox"
            ? (target.checked ? "*" : "")
            : target.value;
          updateTotal();
        };
        input.addEventListener("input", syncInput);
        input.addEventListener("change", syncInput);
      });
      root.querySelectorAll("button[data-remove]").forEach(button => {
        button.onclick = () => {
          state.paymentRows.splice(Number(button.dataset.remove), 1);
          renderPaymentRows();
        };
      });
      updateTotal();
    }

    function updateTotal() {
      let total = 0;
      for (const row of collectPaymentRowsFromDom()) {
        const amount = Number(String(row.amount || "").replace(/[$,]/g, ""));
        if (Number.isFinite(amount) && amount > 0) total += amount;
      }
      $("receiptTotal").textContent = total.toLocaleString(undefined, { style: "currency", currency: "USD" });
    }

    function collectPaymentRowsFromDom() {
      return Array.from(document.querySelectorAll("#paymentRows .payment-grid")).map((row, index) => {
        const marker = row.querySelector('input[data-field="marker"]');
        const date = row.querySelector('input[data-field="date"]');
        const amount = row.querySelector('input[data-field="amount"]');
        const formattedDate = date ? formatPaymentDateInput(date.value) : "";
        if (date) date.value = formattedDate;
        const data = {
          marker: marker && marker.checked ? "*" : "",
          date: formattedDate,
          amount: amount ? amount.value : ""
        };
        state.paymentRows[index] = data;
        return data;
      });
    }

    async function generateReceipt(replace = false) {
      try {
        const payload = {
          profile_id: Number($("receiptProfile").value),
          month: Number($("receiptMonth").value),
          year: Number($("receiptYear").value),
          note: $("receiptNote").value,
          payments: collectPaymentRowsFromDom(),
          replace
        };
        const result = await api("/api/generate", { method: "POST", body: JSON.stringify(payload) });
        setStatus("generateStatus", `Saved ${result.total}. PDF opened in your default viewer.`, "ok");
        window.open(result.pdfUrl, "_blank");
        await loadHistory();
      } catch (error) {
        if (error.status === 409 && error.payload && error.payload.replaceRequired) {
          const replaceIt = await showConfirm({
            title: "Replace existing receipt?",
            body: `${error.payload.error} Generating again will overwrite the existing PDF.`,
            confirmLabel: "Replace",
            danger: true
          });
          if (replaceIt) {
            await generateReceipt(true);
          }
          return;
        }
        setStatus("generateStatus", error.message, "error");
      }
    }

    async function loadHistory() {
      state.history = await api("/api/history");
      const body = $("historyRows");
      body.innerHTML = "";
      if (!state.history.length) {
        body.innerHTML = `<tr><td colspan="6"><div class="empty">${ICONS.fileEmpty}<strong>No receipts yet</strong><span>Receipts you generate will appear here.</span></div></td></tr>`;
        return;
      }
      state.history.forEach(item => {
        const row = document.createElement("tr");
        row.innerHTML = `
          <td class="nowrap">${escapeHtml(formatTimestamp(item.generated_at))}</td>
          <td>${escapeHtml(item.child_name)}</td>
          <td class="nowrap">${escapeHtml(item.period)}</td>
          <td class="money">${escapeHtml(item.total)}</td>
          <td class="pdf-cell"><a href="${item.pdf_url}" target="_blank" title="${escapeAttr(item.pdf_path)}">${escapeHtml(baseName(item.pdf_path))}</a></td>
          <td>
            <div class="row-actions">
              <button class="icon-btn" data-open="${escapeAttr(item.pdf_path)}" aria-label="Open PDF" title="Open PDF">${ICONS.open}</button>
              <button class="icon-btn danger-hover" data-delete="${item.id}" data-label="${escapeAttr(item.child_name + " " + item.period)}" aria-label="Delete receipt" title="Delete receipt">${ICONS.trash}</button>
            </div>
          </td>
        `;
        body.appendChild(row);
      });
      body.querySelectorAll("button[data-open]").forEach(button => {
        button.onclick = () => api("/api/open", { method: "POST", body: JSON.stringify({ kind: "pdf", path: button.dataset.open }) });
      });
      body.querySelectorAll("button[data-delete]").forEach(button => {
        button.onclick = async () => {
          const deleteIt = await showConfirm({
            title: "Delete receipt?",
            body: `${button.dataset.label} will be removed from history and its PDF file deleted. This cannot be undone.`,
            confirmLabel: "Delete",
            danger: true
          });
          if (!deleteIt) return;
          await api("/api/delete-receipt", { method: "POST", body: JSON.stringify({ id: Number(button.dataset.delete) }) });
          await loadHistory();
        };
      });
    }

    async function loadSettings() {
      const settings = await api("/api/settings");
      Object.keys(settings).forEach(key => {
        if ($(key)) $(key).value = settings[key];
      });
    }

    async function saveSettings() {
      try {
        const keys = ["business_name", "business_address_line1", "business_address_line2", "business_phone", "business_email", "filename_token"];
        const payload = {};
        keys.forEach(key => payload[key] = $(key).value);
        await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
        setStatus("settingsStatus", "Settings saved.", "ok");
      } catch (error) {
        setStatus("settingsStatus", error.message, "error");
      }
    }

    function refreshLogoPreview() {
      $("logoPreview").src = `/logo?ts=${Date.now()}`;
    }

    function readFileAsDataUrl(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error("Could not read the selected logo file."));
        reader.readAsDataURL(file);
      });
    }

    async function saveLogo() {
      try {
        const file = $("logoFile").files[0];
        if (!file) {
          setStatus("logoStatus", "Choose a logo image first.", "error");
          return;
        }
        if (file.size > 8 * 1024 * 1024) {
          setStatus("logoStatus", "Logo images must be smaller than 8 MB.", "error");
          return;
        }
        const image = await readFileAsDataUrl(file);
        await api("/api/logo", { method: "POST", body: JSON.stringify({ image }) });
        $("logoFile").value = "";
        refreshLogoPreview();
        setStatus("logoStatus", "Logo saved. New receipts will use this image.", "ok");
      } catch (error) {
        setStatus("logoStatus", error.message, "error");
      }
    }

    function renderUpdateInfo() {
      const update = state.meta.update || {};
      const isAvailable = !!update.updateAvailable;
      $("appVersion").textContent = state.meta.appVersion ? `v${state.meta.appVersion}` : "Unknown";
      $("latestVersion").textContent = update.latestVersion ? `v${update.latestVersion}` : "Unknown";
      $("updateState").textContent = updateStateLabel(update);
      $("updateCheckedAt").textContent = update.checkedAt || "Not checked yet";
      $("settingsUpdateDot").hidden = !isAvailable;
      $("updateBadge").hidden = !isAvailable;
      $("updateDescription").textContent = update.message || "";
      $("checkUpdateBtn").disabled = !update.supported || !!update.checking;
      $("updateBtn").disabled = !update.supported || !!update.checking;
      const downloading = update.state === "downloading";
      const pct = (typeof update.downloadPercent === "number") ? update.downloadPercent : null;
      $("updateProgressWrap").hidden = !downloading;
      if (downloading) {
        const bar = $("updateProgress");
        bar.classList.toggle("indeterminate", pct === null);
        bar.style.width = (pct === null) ? "40%" : `${pct}%`;
        $("updateProgressLabel").textContent = (pct === null) ? "…" : `${pct}%`;
      }
      setStatus("updateStatus", update.message || "", isAvailable ? "ok" : (update.state === "error" ? "error" : ""));
      if (isAvailable && !state.updateNotified) {
        showUpdateToast();
        state.updateNotified = true;
      }
    }

    function updateStateLabel(update) {
      switch (update.state) {
        case "available":
          return "Update available";
        case "downloading":
          return "Downloading";
        case "checking":
          return "Checking";
        case "current":
          return "Up to date";
        case "error":
          return "Check failed";
        case "unavailable":
          return "Unavailable";
        default:
          return "Idle";
      }
    }

    function showUpdateToast() {
      $("updateToast").hidden = false;
    }

    async function refreshUpdateStatus() {
      try {
        state.meta.update = await api("/api/update-status");
        renderUpdateInfo();
        if (state.meta.update.checking) {
          setTimeout(refreshUpdateStatus, state.meta.update.state === "downloading" ? 500 : 2000);
        }
      } catch (error) {
        setStatus("updateStatus", error.message, "error");
      }
    }

    async function checkForUpdates() {
      try {
        state.updateNotified = false;
        $("checkUpdateBtn").disabled = true;
        setStatus("updateStatus", "Checking GitHub for updates...");
        state.meta.update = await api("/api/check-update", { method: "POST", body: "{}" });
        renderUpdateInfo();
        if (state.meta.update.checking) {
          setTimeout(refreshUpdateStatus, state.meta.update.state === "downloading" ? 500 : 2000);
        }
      } catch (error) {
        setStatus("updateStatus", error.message, "error");
        $("checkUpdateBtn").disabled = !(state.meta.update && state.meta.update.supported);
      }
    }

    async function updateApp() {
      const installIt = await showConfirm({
        title: "Install update?",
        body: "The latest version will be downloaded from GitHub. The app closes, installs the update, and reopens automatically.",
        confirmLabel: "Install Update"
      });
      if (!installIt) return;
      $("updateBtn").disabled = true;
      setStatus("updateStatus", "Preparing update...");
      try {
        const result = await api("/api/update", { method: "POST", body: "{}" });
        if (result.alreadyCurrent) {
          setStatus("updateStatus", result.message || "This app is already up to date.", "ok");
          $("updateBtn").disabled = false;
          return;
        }
        setStatus("updateStatus", result.message || "Update downloaded. Restarting app.", "ok");
        setTimeout(() => {
          document.body.innerHTML = "<main class='shell'><div class='panel'><h1>Installing update…</h1><div class='spinner'></div><p class='sub'>The app will close, install the update, and reopen automatically. This usually takes about 10–20 seconds.</p></div></main>";
        }, 300);
      } catch (error) {
        setStatus("updateStatus", error.message, "error");
        $("updateBtn").disabled = !(state.meta.update && state.meta.update.supported);
      }
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[char]));
    }
    function escapeAttr(value) { return escapeHtml(value); }

    document.querySelectorAll(".tab").forEach(tab => tab.onclick = () => showView(tab.dataset.view));
    $("newProfileBtn").onclick = blankProfile;
    $("refreshProfilesBtn").onclick = loadProfiles;
    $("saveProfileBtn").onclick = saveProfile;
    $("addPaymentBtn").onclick = () => addPaymentRow();
    $("generateBtn").onclick = () => generateReceipt(false);
    $("refreshHistoryBtn").onclick = loadHistory;
    $("saveSettingsBtn").onclick = saveSettings;
    $("saveLogoBtn").onclick = saveLogo;
    $("logoFile").onchange = () => {
      const file = $("logoFile").files[0];
      if (file) $("logoPreview").src = URL.createObjectURL(file);
    };
    $("checkUpdateBtn").onclick = checkForUpdates;
    $("updateBtn").onclick = updateApp;
    $("appErrorDismiss").onclick = () => $("appError").hidden = true;
    $("toastDismissBtn").onclick = () => $("updateToast").hidden = true;
    $("toastSettingsBtn").onclick = () => {
      $("updateToast").hidden = true;
      showView("settings");
    };
    $("openReceiptsBtn").onclick = () => api("/api/open", { method: "POST", body: JSON.stringify({ kind: "receipts" }) });
    $("quitBtn").onclick = async () => {
      await api("/api/shutdown", { method: "POST", body: "{}" });
      document.body.innerHTML = "<main class='shell'><div class='panel'><h1>Receipts Tool is closed.</h1><p class='sub'>You can close this browser tab.</p></div></main>";
    };
    setInterval(() => api("/api/heartbeat", { method: "POST", body: "{}" }).catch(() => {}), 5000);
    window.addEventListener("pagehide", () => {
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/api/shutdown", new Blob(["{}"], { type: "application/json" }));
      }
    });

    async function boot() {
      state.meta = await api("/api/meta");
      if (state.meta.appVersion) {
        $("headerVersion").textContent = `v${state.meta.appVersion}`;
        $("headerVersion").hidden = false;
      }
      $("receiptYear").value = state.meta.currentYear;
      $("receiptMonth").innerHTML = state.meta.months.map((name, index) => `<option value="${index + 1}">${name}</option>`).join("");
      $("receiptMonth").value = state.meta.currentMonth;
      renderUpdateInfo();
      refreshUpdateStatus();
      await Promise.all([loadProfiles(), loadSettings(), loadHistory()]);
      addPaymentRow();
    }
    boot().catch(error => showAppError(`The app could not load: ${error.message}`));
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
