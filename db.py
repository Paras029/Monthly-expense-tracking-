"""SQLite schema init and all queries. This module owns the on-disk shape
of the ledger; both the bot and the dashboard only ever go through here."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ts             TEXT NOT NULL,
  spent_on       TEXT NOT NULL,
  category       TEXT NOT NULL,
  note           TEXT,
  amount         REAL NOT NULL,
  raw_message    TEXT,
  source         TEXT DEFAULT 'telegram',
  guessed        INTEGER DEFAULT 0,
  payment_source TEXT DEFAULT 'salary',
  period         TEXT DEFAULT 'monthly',    -- deprecated, superseded by recurrence
  recurrence     TEXT DEFAULT 'one-off'     -- 'one-off' | 'monthly' | 'yearly' (fixed/cross-cutting)
);

CREATE TABLE IF NOT EXISTS categories (
  name            TEXT PRIMARY KEY,
  color           TEXT NOT NULL,
  kind            TEXT NOT NULL,
  monthly_cap     REAL,
  parent_category TEXT REFERENCES categories(name),  -- e.g. an investment vehicle nested under 'Investments'
  target_amount   REAL,  -- optional savings/investment goal for this category
  target_date     TEXT   -- optional 'YYYY-MM-DD' goal date
);

CREATE TABLE IF NOT EXISTS keywords (
  keyword  TEXT PRIMARY KEY,
  category TEXT NOT NULL REFERENCES categories(name)
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS investment_snapshots (
  category   TEXT NOT NULL,  -- a kind='saving' category name — each is its own "vehicle"
  month      TEXT NOT NULL,  -- 'YYYY-MM'
  value      REAL NOT NULL,  -- total value of this vehicle as of this month (manually entered)
  note       TEXT,
  updated_at TEXT,           -- when this snapshot was last set, for "updated N days ago"
  PRIMARY KEY (category, month)
);

CREATE TABLE IF NOT EXISTS ai_recaps (
  date          TEXT PRIMARY KEY,  -- 'YYYY-MM-DD', the day the recap covers
  text          TEXT NOT NULL,     -- newline-separated analysis lines
  source        TEXT NOT NULL,     -- 'ai' | 'fallback'
  generated_at  TEXT NOT NULL
);
"""

# columns that may be missing on a DB created by an earlier version of the app
TRANSACTION_MIGRATIONS = {
    "payment_source": "TEXT DEFAULT 'salary'",
    "period": "TEXT DEFAULT 'monthly'",
    "recurrence": "TEXT DEFAULT 'one-off'",
}

CATEGORY_MIGRATIONS = {
    "parent_category": "TEXT REFERENCES categories(name)",
    "target_amount": "REAL",
    "target_date": "TEXT",
}


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


def _migrate_transactions(conn):
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)")}
    is_fresh_recurrence_column = "recurrence" not in existing
    for column, decl in TRANSACTION_MIGRATIONS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {column} {decl}")

    if is_fresh_recurrence_column:
        # backfill from the old period column: it only ever distinguished
        # 'yearly' (cross-cutting) from its default 'monthly' (which really
        # just meant "not yearly", i.e. an ordinary one-off transaction)
        conn.execute("UPDATE transactions SET recurrence = 'yearly' WHERE period = 'yearly'")


def _migrate_categories(conn):
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(categories)")}
    for column, decl in CATEGORY_MIGRATIONS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE categories ADD COLUMN {column} {decl}")


def _migrate_investments_kind(conn):
    # INSERT OR IGNORE (below) never touches a category that already exists,
    # so a DB created before Investments was reclassified 'essential' ->
    # 'saving' is stuck on the old value forever unless fixed explicitly.
    # Guarded by a one-time flag so it never fights a user's own later edit
    # back to 'essential' via the Categories panel.
    already_migrated = conn.execute(
        "SELECT 1 FROM settings WHERE key = 'migrated_investments_kind'"
    ).fetchone()
    if already_migrated:
        return
    conn.execute(
        "UPDATE categories SET kind = 'saving' WHERE name = 'Investments' AND kind = 'essential'"
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('migrated_investments_kind', '1')"
    )


def _migrate_investment_snapshots(conn):
    # old shape (single global vehicle): month TEXT PRIMARY KEY, value, note.
    # New shape tracks multiple vehicles, keyed by (category, month). CREATE
    # TABLE IF NOT EXISTS above is a no-op against an existing old-shape
    # table, so detect and migrate it explicitly here.
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(investment_snapshots)")}
    if not cols or "category" in cols:
        return
    old_rows = conn.execute("SELECT month, value, note FROM investment_snapshots").fetchall()
    conn.execute("ALTER TABLE investment_snapshots RENAME TO investment_snapshots_old")
    conn.execute(
        "CREATE TABLE investment_snapshots ("
        "  category TEXT NOT NULL, month TEXT NOT NULL, value REAL NOT NULL, "
        "  note TEXT, updated_at TEXT, PRIMARY KEY (category, month))"
    )
    for row in old_rows:
        # the only vehicle that existed before was always 'Investments'
        conn.execute(
            "INSERT INTO investment_snapshots (category, month, value, note) VALUES (?, ?, ?, ?)",
            ("Investments", row["month"], row["value"], row["note"]),
        )
    conn.execute("DROP TABLE investment_snapshots_old")


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_transactions(conn)
        _migrate_categories(conn)
        _migrate_investment_snapshots(conn)

        for name, meta in config.DEFAULT_CATEGORIES.items():
            conn.execute(
                "INSERT OR IGNORE INTO categories (name, color, kind, monthly_cap) "
                "VALUES (?, ?, ?, ?)",
                (name, meta["color"], meta["kind"], meta["monthly_cap"]),
            )

        _migrate_investments_kind(conn)

        for category, keywords in config.CATEGORY_KEYWORDS.items():
            for keyword in keywords:
                conn.execute(
                    "INSERT OR IGNORE INTO keywords (keyword, category) VALUES (?, ?)",
                    (keyword, category),
                )

        seed_settings = {
            "monthly_salary": str(config.MONTHLY_SALARY),
            "credit_limit": str(config.CREDIT_LIMIT),
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
                        source="telegram", guessed=False, payment_source="salary",
                        recurrence="one-off"):
    spent_on = spent_on or datetime.now().strftime("%Y-%m-%d")
    ts = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO transactions (ts, spent_on, category, note, amount, "
            "raw_message, source, guessed, payment_source, recurrence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, spent_on, category, note, amount, raw_message, source,
             int(guessed), payment_source, recurrence),
        )
        return cur.lastrowid


def get_last_transaction():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM transactions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_transaction(txn_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        return dict(row) if row else None


def update_transaction(txn_id, category=None, note=None, amount=None,
                        payment_source=None, recurrence=None, spent_on=None):
    """Full edit of a logged transaction from the dashboard. Any field left
    as None keeps its current value. Clears `guessed` since an edit is an
    explicit human correction, not a Gemini fallback anymore."""
    with get_conn() as conn:
        current = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        if not current:
            return False
        conn.execute(
            "UPDATE transactions SET category = ?, note = ?, amount = ?, "
            "payment_source = ?, recurrence = ?, spent_on = ?, guessed = 0 WHERE id = ?",
            (
                category if category is not None else current["category"],
                note if note is not None else current["note"],
                amount if amount is not None else current["amount"],
                payment_source if payment_source is not None else current["payment_source"],
                recurrence if recurrence is not None else current["recurrence"],
                spent_on if spent_on is not None else current["spent_on"],
                txn_id,
            ),
        )
        return True


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


def get_monthly_totals(months, kind=None, category=None):
    """Returns [{month, total}] for the given list of 'YYYY-MM' strings.
    If kind is given, only sums transactions whose category has that kind
    (e.g. kind='saving' for the savings trend). If category is given, only
    sums that one category (for the per-category drill-down)."""
    with get_conn() as conn:
        out = []
        for month in months:
            if category:
                row = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) AS total FROM transactions "
                    "WHERE spent_on LIKE ? AND category = ?",
                    (f"{month}%", category),
                ).fetchone()
            elif kind:
                row = conn.execute(
                    "SELECT COALESCE(SUM(t.amount), 0) AS total FROM transactions t "
                    "JOIN categories c ON c.name = t.category "
                    "WHERE t.spent_on LIKE ? AND c.kind = ?",
                    (f"{month}%", kind),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) AS total FROM transactions "
                    "WHERE spent_on LIKE ?",
                    (f"{month}%",),
                ).fetchone()
            out.append({"month": month, "total": row["total"]})
        return out


def get_cumulative_savings_contributions(months, category=None):
    """Returns [{month, contributed}] where contributed is the all-time
    running total through the end of that month (not just that month's own
    contribution) — of kind='saving' transactions, or of one category's
    transactions if `category` is given (for a single investment vehicle)."""
    with get_conn() as conn:
        out = []
        for month in months:
            if category:
                row = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) AS total FROM transactions "
                    "WHERE category = ? AND spent_on <= ?",
                    (category, f"{month}-31"),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COALESCE(SUM(t.amount), 0) AS total FROM transactions t "
                    "JOIN categories c ON c.name = t.category "
                    "WHERE c.kind = 'saving' AND t.spent_on <= ?",
                    (f"{month}-31",),
                ).fetchone()
            out.append({"month": month, "contributed": row["total"]})
        return out


def get_cumulative_balance_by_kind(kind, through_month=None):
    """All-time cumulative sum of transactions whose category has this kind
    (e.g. 'liquid' for the liquid fund balance, 'saving' for total savings),
    optionally only counting through the end of a given month."""
    with get_conn() as conn:
        if through_month:
            row = conn.execute(
                "SELECT COALESCE(SUM(t.amount), 0) AS total FROM transactions t "
                "JOIN categories c ON c.name = t.category "
                "WHERE c.kind = ? AND t.spent_on <= ?",
                (kind, f"{through_month}-31"),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(t.amount), 0) AS total FROM transactions t "
                "JOIN categories c ON c.name = t.category WHERE c.kind = ?",
                (kind,),
            ).fetchone()
        return row["total"]


def get_fixed_monthly_costs():
    """Latest logged instance of each distinct (category, note) pair tagged
    recurrence='monthly' — a snapshot of current fixed monthly costs (rent,
    subscriptions), not a growing history, since the user re-logs these
    every month. Grouped by (category, note) rather than just category so
    two different fixed costs in the same category (e.g. Rent and a
    maintenance fee, both 'Bills') don't collapse into one."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT t.* FROM transactions t "
            "INNER JOIN ("
            "  SELECT category, COALESCE(note, '') AS note_key, MAX(id) AS max_id "
            "  FROM transactions WHERE recurrence = 'monthly' "
            "  GROUP BY category, COALESCE(note, '')"
            ") latest ON t.id = latest.max_id "
            "ORDER BY t.amount DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_yearly_costs(limit=100):
    """All-time list of recurrence='yearly' transactions, most recent
    first — these are rare enough that a full history stays readable."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE recurrence = 'yearly' "
            "ORDER BY spent_on DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---- categories ---------------------------------------------------------

def get_categories():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM categories").fetchall()
        return [dict(r) for r in rows]


def get_category_names():
    with get_conn() as conn:
        rows = conn.execute("SELECT name FROM categories").fetchall()
        return [r["name"] for r in rows]


def add_category(name, color, kind, monthly_cap=None, parent_category=None,
                  target_amount=None, target_date=None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO categories (name, color, kind, monthly_cap, parent_category, "
            "target_amount, target_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, color, kind, monthly_cap, parent_category, target_amount, target_date),
        )


def update_category(name, color=None, kind=None, monthly_cap=-1,
                     parent_category=-1, target_amount=-1, target_date=-1):
    """-1 is the sentinel for 'leave unchanged' on nullable fields, since
    None is itself a valid value (no cap/parent/target)."""
    with get_conn() as conn:
        current = conn.execute(
            "SELECT * FROM categories WHERE name = ?", (name,)
        ).fetchone()
        if not current:
            return False
        conn.execute(
            "UPDATE categories SET color = ?, kind = ?, monthly_cap = ?, "
            "parent_category = ?, target_amount = ?, target_date = ? WHERE name = ?",
            (
                color if color is not None else current["color"],
                kind if kind is not None else current["kind"],
                monthly_cap if monthly_cap != -1 else current["monthly_cap"],
                parent_category if parent_category != -1 else current["parent_category"],
                target_amount if target_amount != -1 else current["target_amount"],
                target_date if target_date != -1 else current["target_date"],
                name,
            ),
        )
        return True


def delete_category(name, reassign_to="Other"):
    with get_conn() as conn:
        conn.execute(
            "UPDATE transactions SET category = ? WHERE category = ?",
            (reassign_to, name),
        )
        conn.execute("DELETE FROM keywords WHERE category = ?", (name,))
        conn.execute("DELETE FROM categories WHERE name = ?", (name,))


# ---- keywords -------------------------------------------------------------

def get_keywords():
    """Returns {keyword: category}."""
    with get_conn() as conn:
        rows = conn.execute("SELECT keyword, category FROM keywords").fetchall()
        return {r["keyword"]: r["category"] for r in rows}


def get_keywords_by_category():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT keyword, category FROM keywords ORDER BY category, keyword"
        ).fetchall()
        out = {}
        for r in rows:
            out.setdefault(r["category"], []).append(r["keyword"])
        return out


def add_keyword(keyword, category):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO keywords (keyword, category) VALUES (?, ?)",
            (keyword.lower(), category),
        )


def delete_keyword(keyword):
    with get_conn() as conn:
        conn.execute("DELETE FROM keywords WHERE keyword = ?", (keyword.lower(),))


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


# ---- investment snapshots (long-term, per-vehicle portfolio tracking) ----

def get_investment_snapshots(category):
    """Returns {month: {value, note, updated_at}} for one vehicle."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT month, value, note, updated_at FROM investment_snapshots "
            "WHERE category = ? ORDER BY month",
            (category,),
        ).fetchall()
        return {
            r["month"]: {"value": r["value"], "note": r["note"], "updated_at": r["updated_at"]}
            for r in rows
        }


def get_latest_investment_snapshot(category):
    """Most recent snapshot for a vehicle, or None if it's never been set."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT month, value, note, updated_at FROM investment_snapshots "
            "WHERE category = ? ORDER BY month DESC LIMIT 1",
            (category,),
        ).fetchone()
        return dict(row) if row else None


def get_snapshot_as_of(category, month):
    """The most recent snapshot at or before `month` — the value as it was
    known as of that point, forward-filled from whenever it was last set."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT month, value, note, updated_at FROM investment_snapshots "
            "WHERE category = ? AND month <= ? ORDER BY month DESC LIMIT 1",
            (category, month),
        ).fetchone()
        return dict(row) if row else None


def get_previous_snapshot_before(category, month):
    """The snapshot immediately before `month` (strictly earlier) — the
    baseline used to compute incremental gain for the snapshot at `month`."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT month, value, note, updated_at FROM investment_snapshots "
            "WHERE category = ? AND month < ? ORDER BY month DESC LIMIT 1",
            (category, month),
        ).fetchone()
        return dict(row) if row else None


def get_contributions_between(category, after_month_exclusive, through_month_inclusive):
    """Sum of a category's transactions after the end of after_month_exclusive
    (or all-time if None) through the end of through_month_inclusive — the
    real money added between two investment snapshots, used to compute
    incremental gain (new value vs. previous value + what was added since)."""
    with get_conn() as conn:
        if after_month_exclusive:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS total FROM transactions "
                "WHERE category = ? AND spent_on > ? AND spent_on <= ?",
                (category, f"{after_month_exclusive}-31", f"{through_month_inclusive}-31"),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS total FROM transactions "
                "WHERE category = ? AND spent_on <= ?",
                (category, f"{through_month_inclusive}-31"),
            ).fetchone()
        return row["total"]


def set_investment_snapshot(category, month, value, note=None):
    """Returns the updated_at timestamp that was written."""
    updated_at = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO investment_snapshots (category, month, value, note, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(category, month) DO UPDATE SET value = excluded.value, "
            "note = excluded.note, updated_at = excluded.updated_at",
            (category, month, value, note, updated_at),
        )
    return updated_at


# ---- AI daily recap (cached, at most once/day unless force-refreshed) ----

def get_ai_recap(date_str):
    """Returns {text, source, generated_at} for a cached recap, or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT text, source, generated_at FROM ai_recaps WHERE date = ?",
            (date_str,),
        ).fetchone()
        return dict(row) if row else None


def set_ai_recap(date_str, text, source):
    """Returns the generated_at timestamp that was written."""
    generated_at = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ai_recaps (date, text, source, generated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET text = excluded.text, source = excluded.source, "
            "generated_at = excluded.generated_at",
            (date_str, text, source, generated_at),
        )
    return generated_at
