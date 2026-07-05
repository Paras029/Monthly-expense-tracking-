"""SQLite schema init and all queries. This module owns the on-disk shape
of the ledger; both the bot and the dashboard only ever go through here."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  spent_on    TEXT NOT NULL,
  category    TEXT NOT NULL,
  note        TEXT,
  amount      REAL NOT NULL,
  raw_message TEXT,
  source      TEXT DEFAULT 'telegram',
  guessed     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS categories (
  name         TEXT PRIMARY KEY,
  color        TEXT NOT NULL,
  kind         TEXT NOT NULL,
  monthly_cap  REAL
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)

        for name, meta in config.DEFAULT_CATEGORIES.items():
            conn.execute(
                "INSERT OR IGNORE INTO categories (name, color, kind, monthly_cap) "
                "VALUES (?, ?, ?, ?)",
                (name, meta["color"], meta["kind"], meta["monthly_cap"]),
            )

        seed_settings = {
            "monthly_budget": str(config.MONTHLY_BUDGET),
            "currency": config.CURRENCY,
            "timezone": config.TIMEZONE,
        }
        for key, value in seed_settings.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )


# ---- transactions -----------------------------------------------------

def insert_transaction(category, note, amount, raw_message, spent_on=None,
                        source="telegram", guessed=False):
    spent_on = spent_on or datetime.now().strftime("%Y-%m-%d")
    ts = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO transactions (ts, spent_on, category, note, amount, "
            "raw_message, source, guessed) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, spent_on, category, note, amount, raw_message, source, int(guessed)),
        )
        return cur.lastrowid


def get_last_transaction():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM transactions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def delete_transaction(txn_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))


def update_transaction_category(txn_id, category):
    with get_conn() as conn:
        conn.execute(
            "UPDATE transactions SET category = ?, guessed = 0 WHERE id = ?",
            (category, txn_id),
        )


def get_transactions_for_month(month):
    """month is 'YYYY-MM'."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE spent_on LIKE ? ORDER BY spent_on DESC, id DESC",
            (f"{month}%",),
        ).fetchall()
        return [dict(r) for r in rows]


def get_transactions_for_day(day):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE spent_on = ? ORDER BY id DESC",
            (day,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_transactions(limit=20):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_monthly_totals(months):
    """Returns [{month, total}] for the given list of 'YYYY-MM' strings."""
    with get_conn() as conn:
        out = []
        for month in months:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS total FROM transactions "
                "WHERE spent_on LIKE ?",
                (f"{month}%",),
            ).fetchone()
            out.append({"month": month, "total": row["total"]})
        return out


# ---- categories ---------------------------------------------------------

def get_categories():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM categories").fetchall()
        return [dict(r) for r in rows]


def get_category_names():
    with get_conn() as conn:
        rows = conn.execute("SELECT name FROM categories").fetchall()
        return [r["name"] for r in rows]


# ---- settings ------------------------------------------------------------

def get_setting(key, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
