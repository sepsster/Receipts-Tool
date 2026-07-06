from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import DEFAULT_SETTINGS, Payment, Profile, ReceiptRecord, TEMPLATE_VERSION, now_iso
from .paths import AppPaths, ensure_base_dirs


class ReceiptStore:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        ensure_base_dirs(paths)
        self._backup_existing_database()
        self._migrate()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.paths.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _backup_existing_database(self) -> None:
        if not self.paths.db_path.exists():
            return

        stamp = now_iso().replace(":", "").replace("-", "").replace(" ", "-")
        backup_path = self.paths.backup_dir / f"receipts-{stamp}.sqlite"
        shutil.copy2(self.paths.db_path, backup_path)

    def _migrate(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    child_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT '',
                    parent1_name TEXT NOT NULL DEFAULT '',
                    parent2_name TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    address_line1 TEXT NOT NULL DEFAULT '',
                    address_line2 TEXT NOT NULL DEFAULT '',
                    phone1 TEXT NOT NULL DEFAULT '',
                    phone2 TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id INTEGER NOT NULL REFERENCES profiles(id),
                    receipt_month INTEGER NOT NULL,
                    receipt_year INTEGER NOT NULL,
                    pdf_path TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    total_cents INTEGER NOT NULL,
                    template_version TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    UNIQUE(profile_id, receipt_month, receipt_year)
                );

                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_id INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
                    row_order INTEGER NOT NULL,
                    payment_date TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    marker TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            profile_columns = {row["name"] for row in conn.execute("PRAGMA table_info(profiles)").fetchall()}
            if "email" not in profile_columns:
                conn.execute("ALTER TABLE profiles ADD COLUMN email TEXT NOT NULL DEFAULT ''")
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (key, value),
                )
            conn.commit()

    def get_settings(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        settings = DEFAULT_SETTINGS.copy()
        settings.update({row["key"]: row["value"] for row in rows})
        return settings

    def save_settings(self, settings: dict[str, str]) -> None:
        with self.connect() as conn:
            for key in DEFAULT_SETTINGS:
                value = settings.get(key, DEFAULT_SETTINGS[key]).strip()
                conn.execute(
                    """
                    INSERT INTO settings (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )
            conn.commit()

    def profiles(self, include_inactive: bool = True) -> list[Profile]:
        sql = "SELECT * FROM profiles"
        params: tuple[object, ...] = ()
        if not include_inactive:
            sql += " WHERE active = 1"
        sql += " ORDER BY active DESC, child_name COLLATE NOCASE"

        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._profile_from_row(row) for row in rows]

    def get_profile(self, profile_id: int) -> Profile | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        return self._profile_from_row(row) if row else None

    def save_profile(self, profile: Profile) -> int:
        child_name = profile.child_name.strip()
        if not child_name:
            raise ValueError("Child name is required.")

        timestamp = now_iso()
        params = (
            child_name,
            profile.status.strip(),
            profile.parent1_name.strip(),
            profile.parent2_name.strip(),
            profile.email.strip(),
            profile.address_line1.strip(),
            profile.address_line2.strip(),
            profile.phone1.strip(),
            profile.phone2.strip(),
            1 if profile.active else 0,
            timestamp,
        )

        with self.connect() as conn:
            if profile.id is None:
                cur = conn.execute(
                    """
                    INSERT INTO profiles (
                        child_name, status, parent1_name, parent2_name,
                        email, address_line1, address_line2, phone1, phone2,
                        active, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    params + (timestamp,),
                )
                profile_id = int(cur.lastrowid)
            else:
                conn.execute(
                    """
                    UPDATE profiles
                    SET child_name = ?, status = ?, parent1_name = ?, parent2_name = ?,
                        email = ?, address_line1 = ?, address_line2 = ?, phone1 = ?, phone2 = ?,
                        active = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    params + (profile.id,),
                )
                profile_id = profile.id
            conn.commit()
        return profile_id

    def save_receipt(
        self,
        profile_id: int,
        receipt_month: int,
        receipt_year: int,
        relative_pdf_path: Path,
        note: str,
        payments: Iterable[Payment],
    ) -> int:
        payments = list(payments)
        total_cents = sum(payment.amount_cents for payment in payments)
        timestamp = now_iso()

        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO receipts (
                    profile_id, receipt_month, receipt_year, pdf_path, note,
                    total_cents, template_version, generated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, receipt_month, receipt_year)
                DO UPDATE SET
                    pdf_path = excluded.pdf_path,
                    note = excluded.note,
                    total_cents = excluded.total_cents,
                    template_version = excluded.template_version,
                    generated_at = excluded.generated_at
                RETURNING id
                """,
                (
                    profile_id,
                    receipt_month,
                    receipt_year,
                    str(relative_pdf_path),
                    note.strip(),
                    total_cents,
                    TEMPLATE_VERSION,
                    timestamp,
                ),
            )
            receipt_id = int(cur.fetchone()["id"])
            conn.execute("DELETE FROM payments WHERE receipt_id = ?", (receipt_id,))
            conn.executemany(
                """
                INSERT INTO payments (receipt_id, row_order, payment_date, amount_cents, marker)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        receipt_id,
                        payment.row_order or index,
                        payment.payment_date.isoformat(),
                        payment.amount_cents,
                        payment.marker.strip(),
                    )
                    for index, payment in enumerate(payments, start=1)
                ],
            )
            conn.commit()
        return receipt_id

    def receipt_history(self) -> list[ReceiptRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    r.id, r.profile_id, p.child_name, r.receipt_month,
                    r.receipt_year, r.pdf_path, r.note, r.total_cents,
                    r.generated_at
                FROM receipts r
                JOIN profiles p ON p.id = r.profile_id
                ORDER BY r.generated_at DESC
                """
            ).fetchall()
        return [self._receipt_from_row(row) for row in rows]

    def receipt_exists(self, profile_id: int, receipt_month: int, receipt_year: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM receipts
                WHERE profile_id = ? AND receipt_month = ? AND receipt_year = ?
                """,
                (profile_id, receipt_month, receipt_year),
            ).fetchone()
        return row is not None

    def delete_receipt(self, receipt_id: int) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT pdf_path FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
            if row is None:
                return None
            pdf_path = row["pdf_path"]
            conn.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))
            conn.commit()
        return pdf_path

    @staticmethod
    def _profile_from_row(row: sqlite3.Row) -> Profile:
        return Profile(
            id=row["id"],
            child_name=row["child_name"],
            status=row["status"],
            parent1_name=row["parent1_name"],
            parent2_name=row["parent2_name"],
            email=row["email"],
            address_line1=row["address_line1"],
            address_line2=row["address_line2"],
            phone1=row["phone1"],
            phone2=row["phone2"],
            active=bool(row["active"]),
        )

    @staticmethod
    def _receipt_from_row(row: sqlite3.Row) -> ReceiptRecord:
        return ReceiptRecord(
            id=row["id"],
            profile_id=row["profile_id"],
            child_name=row["child_name"],
            receipt_month=row["receipt_month"],
            receipt_year=row["receipt_year"],
            pdf_path=row["pdf_path"],
            note=row["note"],
            total_cents=row["total_cents"],
            generated_at=row["generated_at"],
        )
