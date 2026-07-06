from __future__ import annotations

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
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

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


def main() -> None:
    if "--self-test" in sys.argv:
        run_self_test()
        return

    no_browser = "--no-browser" in sys.argv
    port_file = _arg_value("--port-file")
    paths = get_paths()
    store = ReceiptStore(paths)
    update_checker = UpdateCheckState(paths)
    update_checker.start()
    last_heartbeat = {"value": time.monotonic()}
    handler = make_handler(paths, store, last_heartbeat, update_checker)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(f"Spring Flowers Receipts is running at {url}")
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
    tmp = tempfile.mkdtemp(prefix="spring_flowers_receipts_")
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
                address_line1="123 Test St",
                address_line2="Mill Creek, WA 98012",
                phone1="(425) 269-5805",
            )
        )
        profile = store.get_profile(profile_id)
        if profile is None:
            raise RuntimeError("Self-test profile was not saved.")
        payments = [
            Payment(parse_payment_date("04/06/2026"), parse_money_to_cents("375"), row_order=1),
            Payment(parse_payment_date("04/13/2026"), parse_money_to_cents("425"), row_order=2),
        ]
        relative_path = receipt_relative_path(profile.child_name, "SpringProtected", 4, 2026)
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
        print("Spring Flowers Receipts self-test passed.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def make_handler(
    paths: AppPaths,
    store: ReceiptStore,
    last_heartbeat: dict[str, float] | None = None,
    update_checker: UpdateCheckState | None = None,
):
    class SpringFlowersHandler(BaseHTTPRequestHandler):
        server_version = "SpringFlowersReceipts/0.1"

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
                        "update": update_checker.snapshot() if update_checker else {},
                    }
                )
                return
            if parsed.path == "/api/update-status":
                self._send_json(update_checker.snapshot() if update_checker else {})
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

            payments = parse_payments(data.get("payments") or [])
            note = str(data.get("note", "")).strip()
            settings = store.get_settings()
            relative_path = receipt_relative_path(
                profile.child_name,
                settings.get("filename_token", "SpringProtected"),
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
                logo_path=paths.logo_path,
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

    return SpringFlowersHandler


def parse_payments(raw_payments: list[dict]) -> list[Payment]:
    payments: list[Payment] = []
    for index, row in enumerate(raw_payments, start=1):
        raw_date = str(row.get("date", "")).strip()
        raw_amount = str(row.get("amount", "")).strip()
        raw_marker = str(row.get("marker", "")).strip()
        if not (raw_date or raw_amount or raw_marker):
            continue
        try:
            payments.append(
                Payment(
                    payment_date=parse_payment_date(raw_date),
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


def profile_to_json(profile: Profile | None) -> dict:
    if profile is None:
        return {}
    return {
        "id": profile.id,
        "child_name": profile.child_name,
        "status": profile.status,
        "parent1_name": profile.parent1_name,
        "parent2_name": profile.parent2_name,
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
  <title>Spring Flowers Receipts</title>
  <style>
    :root {
      --accent: #df7477;
      --accent-dark: #c95f63;
      --ink: #202124;
      --muted: #667085;
      --line: #d6d8dc;
      --panel: #ffffff;
      --bg: #f5f6f4;
      --green: #3d7b65;
      --gold: #b88835;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", Arial, sans-serif;
      color: var(--ink);
      background: var(--bg);
    }
    header {
      display: flex;
      justify-content: space-between;
      gap: 24px;
      align-items: center;
      padding: 22px 28px 14px;
    }
    h1 { margin: 0; font-size: 28px; font-weight: 700; }
    .sub { color: var(--muted); margin-top: 4px; }
    .shell { padding: 0 28px 28px; }
    .tabs { display: flex; gap: 8px; border-bottom: 1px solid var(--line); }
    .tab {
      border: 0;
      background: transparent;
      padding: 12px 16px;
      font: inherit;
      font-weight: 650;
      color: var(--muted);
      cursor: pointer;
      border-bottom: 3px solid transparent;
    }
    .tab.active { color: var(--ink); border-bottom-color: var(--accent); }
    .view { display: none; padding-top: 16px; }
    .view.active { display: block; }
    .grid { display: grid; grid-template-columns: minmax(260px, 0.9fr) minmax(360px, 1.6fr); gap: 16px; }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      min-width: 0;
    }
    .panel h2 { margin: 0 0 14px; font-size: 17px; }
    label { display: block; font-weight: 650; margin: 12px 0 6px; }
    input, select, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px 11px;
      font: inherit;
      background: #fff;
    }
    textarea { resize: vertical; min-height: 88px; }
    .row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .actions { display: flex; gap: 10px; justify-content: flex-end; align-items: center; margin-top: 16px; flex-wrap: wrap; }
    button {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px 13px;
      font: inherit;
      font-weight: 650;
      cursor: pointer;
      background: #fff;
      color: var(--ink);
    }
    button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
    button.primary:hover { background: var(--accent-dark); }
    button.success { background: var(--green); border-color: var(--green); color: #fff; }
    button.warn { background: #fff8e8; border-color: #e3c57f; color: #6c4d08; }
    button.danger { background: #fff5f5; border-color: #f0b4b4; color: #9f1c1c; }
    button:disabled { cursor: not-allowed; opacity: .55; }
    .list { display: grid; gap: 8px; }
    .list button { text-align: left; display: block; width: 100%; }
    .list button.selected { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(223,116,119,.18); }
    .pill { display: inline-block; padding: 3px 8px; border-radius: 999px; font-size: 12px; background: #eef5f1; color: var(--green); margin-left: 6px; }
    .inactive { opacity: .62; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid var(--line); padding: 10px 8px; text-align: left; }
    th { color: var(--muted); font-size: 13px; }
    td.money, th.money { text-align: right; }
    .payment-grid {
      display: grid;
      grid-template-columns: 38px 58px minmax(150px, 1fr) minmax(110px, 160px) 76px;
      gap: 8px;
      align-items: center;
      margin-bottom: 8px;
    }
    .payment-grid input[type="checkbox"] { width: 18px; height: 18px; justify-self: start; }
    .payment-head { color: var(--muted); font-weight: 700; font-size: 13px; }
    .total {
      font-size: 22px;
      font-weight: 750;
      color: var(--green);
      margin-left: auto;
    }
    .status {
      min-height: 24px;
      color: var(--muted);
      margin-top: 10px;
    }
    .status.error { color: #b42318; }
    .status.ok { color: var(--green); }
    .footer-actions { display: flex; gap: 10px; justify-content: space-between; align-items: center; }
    .tab-dot {
      display: inline-block;
      width: 8px;
      height: 8px;
      border-radius: 50%;
      margin-left: 6px;
      background: var(--accent);
      vertical-align: middle;
    }
    .update-badge {
      display: inline-flex;
      align-items: center;
      border: 1px solid #e3c57f;
      border-radius: 999px;
      padding: 3px 8px;
      margin-left: 8px;
      font-size: 12px;
      color: #6c4d08;
      background: #fff8e8;
    }
    .toast {
      position: fixed;
      right: 22px;
      bottom: 22px;
      z-index: 10;
      width: min(360px, calc(100vw - 44px));
      border: 1px solid var(--line);
      border-left: 5px solid var(--accent);
      border-radius: 8px;
      background: #fff;
      box-shadow: 0 16px 36px rgba(32, 33, 36, .18);
      padding: 14px;
    }
    .toast-title { font-weight: 750; margin-bottom: 4px; }
    .toast-body { color: var(--muted); line-height: 1.35; }
    .toast-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }
    [hidden] { display: none !important; }
    @media (max-width: 860px) {
      header { align-items: flex-start; flex-direction: column; }
      .grid, .row { grid-template-columns: 1fr; }
      .payment-grid { grid-template-columns: 36px 58px 1fr; }
      .payment-grid > :nth-child(4), .payment-grid > :nth-child(5) { grid-column: span 1; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Spring Flowers Receipts</h1>
      <div class="sub">Local profiles, standardized payment receipts, and PDF history. Closing this browser tab closes the app.</div>
    </div>
    <button class="warn" id="quitBtn">Close App</button>
  </header>
  <main class="shell">
    <nav class="tabs">
      <button class="tab active" data-view="profiles">Profiles</button>
      <button class="tab" data-view="generate">Generate Receipt</button>
      <button class="tab" data-view="history">History</button>
      <button class="tab" data-view="settings">Settings<span class="tab-dot" id="settingsUpdateDot" hidden></span></button>
    </nav>

    <section id="profiles" class="view active">
      <div class="grid">
        <div class="panel">
          <h2>Saved Profiles</h2>
          <div id="profileList" class="list"></div>
          <div class="actions" style="justify-content:flex-start">
            <button id="newProfileBtn">New Profile</button>
            <button id="refreshProfilesBtn">Refresh</button>
          </div>
        </div>
        <div class="panel">
          <h2>Profile Details</h2>
          <input type="hidden" id="profileId">
          <div class="row">
            <div><label>Child name</label><input id="childName" autocomplete="off"></div>
            <div><label>Status</label><input id="status" value="Full-Time"></div>
          </div>
          <div class="row">
            <div><label>Parent/guardian 1</label><input id="parent1"></div>
            <div><label>Parent/guardian 2</label><input id="parent2"></div>
          </div>
          <div class="row">
            <div><label>Address line 1</label><input id="address1"></div>
            <div><label>Address line 2</label><input id="address2"></div>
          </div>
          <div class="row">
            <div><label>Phone 1</label><input id="phone1"></div>
            <div><label>Phone 2</label><input id="phone2"></div>
          </div>
          <label><input id="active" type="checkbox" style="width:auto" checked> Active profile</label>
          <div class="actions">
            <button class="primary" id="saveProfileBtn">Save Profile</button>
          </div>
          <div id="profileStatus" class="status"></div>
        </div>
      </div>
    </section>

    <section id="generate" class="view">
      <div class="panel">
        <h2>Receipt Details</h2>
        <div class="row">
          <div><label>Profile</label><select id="receiptProfile"></select></div>
          <div class="row">
            <div><label>Month</label><select id="receiptMonth"></select></div>
            <div><label>Year</label><input id="receiptYear" type="number" min="2020" max="2035"></div>
          </div>
        </div>
        <h2 style="margin-top:24px">Payments</h2>
        <div class="payment-grid payment-head"><div>#</div><div>Note</div><div>Date</div><div>Amount</div><div></div></div>
        <div id="paymentRows"></div>
        <div class="actions">
          <button id="addPaymentBtn">Add Payment</button>
          <div class="total" id="receiptTotal">$0.00</div>
        </div>
        <label>Optional Note</label>
        <textarea id="receiptNote"></textarea>
        <div class="footer-actions" style="margin-top:16px">
          <button id="openReceiptsBtn">Open Receipts Folder</button>
          <button class="success" id="generateBtn">Generate PDF</button>
        </div>
        <div id="generateStatus" class="status"></div>
      </div>
    </section>

    <section id="history" class="view">
      <div class="panel">
        <div class="footer-actions">
          <h2>Generated Receipts</h2>
          <button id="refreshHistoryBtn">Refresh</button>
        </div>
        <table>
          <thead><tr><th>Generated</th><th>Child</th><th>Period</th><th class="money">Total</th><th>PDF</th><th></th></tr></thead>
          <tbody id="historyRows"></tbody>
        </table>
      </div>
    </section>

    <section id="settings" class="view">
      <div class="panel">
        <h2>Business Defaults</h2>
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
      <div class="panel" style="margin-top:16px">
        <h2>App Updates <span class="update-badge" id="updateBadge" hidden>Update available</span></h2>
        <div class="sub" id="updateDescription"></div>
        <div class="actions" style="justify-content:flex-start">
          <button class="success" id="updateBtn">Update From GitHub</button>
        </div>
        <div id="updateStatus" class="status"></div>
      </div>
    </section>
  </main>

  <div class="toast" id="updateToast" hidden>
    <div class="toast-title">Update Available</div>
    <div class="toast-body">A newer version is ready to install from GitHub.</div>
    <div class="toast-actions">
      <button id="toastDismissBtn">Dismiss</button>
      <button class="success" id="toastSettingsBtn">Open Settings</button>
    </div>
  </div>

  <script>
    const state = { profiles: [], history: [], meta: {}, paymentRows: [], updateNotified: false };
    const $ = (id) => document.getElementById(id);

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

    function blankProfile() {
      $("profileId").value = "";
      $("childName").value = "";
      $("status").value = "Full-Time";
      $("parent1").value = "";
      $("parent2").value = "";
      $("address1").value = "";
      $("address2").value = "";
      $("phone1").value = "";
      $("phone2").value = "";
      $("active").checked = true;
      document.querySelectorAll("#profileList button").forEach(btn => btn.classList.remove("selected"));
      setStatus("profileStatus", "");
    }

    function loadProfile(profile) {
      $("profileId").value = profile.id || "";
      $("childName").value = profile.child_name || "";
      $("status").value = profile.status || "";
      $("parent1").value = profile.parent1_name || "";
      $("parent2").value = profile.parent2_name || "";
      $("address1").value = profile.address_line1 || "";
      $("address2").value = profile.address_line2 || "";
      $("phone1").value = profile.phone1 || "";
      $("phone2").value = profile.phone2 || "";
      $("active").checked = !!profile.active;
      document.querySelectorAll("#profileList button").forEach(btn => btn.classList.toggle("selected", Number(btn.dataset.id) === profile.id));
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
        list.innerHTML = "<div class='sub'>No profiles yet.</div>";
        return;
      }
      state.profiles.forEach(profile => {
        const btn = document.createElement("button");
        btn.dataset.id = profile.id;
        btn.className = profile.active ? "" : "inactive";
        btn.innerHTML = `<strong>${escapeHtml(profile.child_name)}</strong><span class="pill">${escapeHtml(profile.status || "No status")}</span><div class="sub">${escapeHtml(profile.parent1_name || "")}</div>`;
        btn.onclick = () => loadProfile(profile);
        list.appendChild(btn);
      });
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

    function renderPaymentRows() {
      const root = $("paymentRows");
      root.innerHTML = "";
      state.paymentRows.forEach((row, index) => {
        const wrap = document.createElement("div");
        wrap.className = "payment-grid";
        wrap.innerHTML = `
          <div>${index + 1}</div>
          <input type="checkbox" data-field="marker" data-index="${index}" ${row.marker ? "checked" : ""} title="Print an asterisk for the optional note">
          <input data-field="date" data-index="${index}" value="${escapeAttr(row.date)}" placeholder="mm/dd/yyyy">
          <input data-field="amount" data-index="${index}" value="${escapeAttr(row.amount)}" placeholder="425.00">
          <button data-remove="${index}">Remove</button>
        `;
        root.appendChild(wrap);
      });
      root.querySelectorAll("input").forEach(input => {
        const syncInput = event => {
          const target = event.target;
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
        const data = {
          marker: marker && marker.checked ? "*" : "",
          date: date ? date.value : "",
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
          if (confirm(`${error.payload.error}\n\nReplace it?`)) {
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
        body.innerHTML = "<tr><td colspan='6' class='sub'>No generated receipts yet.</td></tr>";
        return;
      }
      state.history.forEach(item => {
        const row = document.createElement("tr");
        row.innerHTML = `
          <td>${escapeHtml(item.generated_at)}</td>
          <td>${escapeHtml(item.child_name)}</td>
          <td>${escapeHtml(item.period)}</td>
          <td class="money">${escapeHtml(item.total)}</td>
          <td><a href="${item.pdf_url}" target="_blank">${escapeHtml(item.pdf_path)}</a></td>
          <td>
            <button data-open="${escapeAttr(item.pdf_path)}">Open</button>
            <button class="danger" data-delete="${item.id}" data-label="${escapeAttr(item.child_name + " " + item.period)}">Delete</button>
          </td>
        `;
        body.appendChild(row);
      });
      body.querySelectorAll("button[data-open]").forEach(button => {
        button.onclick = () => api("/api/open", { method: "POST", body: JSON.stringify({ kind: "pdf", path: button.dataset.open }) });
      });
      body.querySelectorAll("button[data-delete]").forEach(button => {
        button.onclick = async () => {
          if (!confirm(`Delete this receipt from history and remove its PDF file?\n\n${button.dataset.label}`)) return;
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

    function renderUpdateInfo() {
      const update = state.meta.update || {};
      const isAvailable = !!update.updateAvailable;
      $("settingsUpdateDot").hidden = !isAvailable;
      $("updateBadge").hidden = !isAvailable;
      $("updateDescription").textContent = update.message || "";
      $("updateBtn").disabled = !update.supported;
      setStatus("updateStatus", update.message || "", isAvailable ? "ok" : (update.state === "error" ? "error" : ""));
      if (isAvailable && !state.updateNotified) {
        showUpdateToast();
        state.updateNotified = true;
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
          setTimeout(refreshUpdateStatus, 2000);
        }
      } catch (error) {
        setStatus("updateStatus", error.message, "error");
      }
    }

    async function updateApp() {
      if (!confirm("Download the latest app from GitHub, close this app, and reopen it after updating?")) return;
      $("updateBtn").disabled = true;
      setStatus("updateStatus", "Downloading latest version from GitHub...");
      try {
        const result = await api("/api/update", { method: "POST", body: "{}" });
        if (result.alreadyCurrent) {
          setStatus("updateStatus", result.message || "This app is already up to date.", "ok");
          $("updateBtn").disabled = false;
          return;
        }
        setStatus("updateStatus", result.message || "Update downloaded. Restarting app.", "ok");
        setTimeout(() => {
          document.body.innerHTML = "<main class='shell'><div class='panel'><h1>Updating Spring Flowers Receipts</h1><p class='sub'>The app will reopen automatically after the update installs.</p></div></main>";
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
    $("updateBtn").onclick = updateApp;
    $("toastDismissBtn").onclick = () => $("updateToast").hidden = true;
    $("toastSettingsBtn").onclick = () => {
      $("updateToast").hidden = true;
      showView("settings");
    };
    $("openReceiptsBtn").onclick = () => api("/api/open", { method: "POST", body: JSON.stringify({ kind: "receipts" }) });
    $("quitBtn").onclick = async () => {
      await api("/api/shutdown", { method: "POST", body: "{}" });
      document.body.innerHTML = "<main class='shell'><div class='panel'><h1>Spring Flowers Receipts is closed.</h1><p class='sub'>You can close this browser tab.</p></div></main>";
    };
    setInterval(() => api("/api/heartbeat", { method: "POST", body: "{}" }).catch(() => {}), 5000);
    window.addEventListener("pagehide", () => {
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/api/shutdown", new Blob(["{}"], { type: "application/json" }));
      }
    });

    async function boot() {
      state.meta = await api("/api/meta");
      $("receiptYear").value = state.meta.currentYear;
      $("receiptMonth").innerHTML = state.meta.months.map((name, index) => `<option value="${index + 1}">${name}</option>`).join("");
      $("receiptMonth").value = state.meta.currentMonth;
      renderUpdateInfo();
      refreshUpdateStatus();
      await Promise.all([loadProfiles(), loadSettings(), loadHistory()]);
      addPaymentRow();
    }
    boot().catch(error => alert(error.message));
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
