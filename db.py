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
  payment_source TEXT DEFAULT 'wallet',    -- 'wallet' | 'credit' | 'liquid'
  expense_type   TEXT DEFAULT 'variable',  -- 'fixed' | 'variable' | 'one-off' | 'saving'
  cadence        TEXT                      -- 'daily' | 'weekly' | 'monthly' | 'annual' | NULL
);

CREATE TABLE IF NOT EXISTS categories (
  name            TEXT PRIMARY KEY,
  color           TEXT NOT NULL,
  expense_type    TEXT NOT NULL,          -- default expense_type for new transactions in this category
  cadence         TEXT,                   -- default cadence for new transactions in this category
  monthly_cap     REAL,
  parent_category TEXT REFERENCES categories(name),  -- e.g. an investment vehicle nested under 'Investments'
  target_amount   REAL,  -- optional savings/investment goal for this category
  target_date     TEXT,  -- optional 'YYYY-MM-DD' goal date
  account_link    TEXT   -- NULL | 'liquid': this saving category's deposits also feed the Liquid account balance
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
  category            TEXT NOT NULL,  -- an expense_type='saving' category name — each is its own "vehicle"
  month               TEXT NOT NULL,  -- 'YYYY-MM'
  value               REAL NOT NULL,  -- total value of this vehicle as of this month (manually entered)
  note                TEXT,
  updated_at          TEXT,           -- when this snapshot was last set, for "updated N days ago"
  contributed_override REAL,          -- optional manual correction to the running "contributed" total as
                                       -- of this month (see get_cumulative_savings_contributions) — lets a
                                       -- user back-fill money put into a vehicle before they started using
                                       -- this app, which the auto-computed sum-of-transactions figure can
                                       -- never see. NULL means no correction; auto-computed sum is used.
  PRIMARY KEY (category, month)
);

CREATE TABLE IF NOT EXISTS ai_recaps (
  date          TEXT PRIMARY KEY,  -- 'YYYY-MM-DD', the day the recap covers
  text          TEXT NOT NULL,     -- newline-separated analysis lines
  source        TEXT NOT NULL,     -- 'ai' | 'fallback'
  generated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_transactions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  txn_date    TEXT NOT NULL,   -- 'YYYY-MM-DD'
  account     TEXT NOT NULL,   -- 'wallet' | 'credit' | 'liquid'
  amount      REAL NOT NULL,   -- signed: + increases the account's balance-in-your-favor, - decreases
  txn_kind    TEXT NOT NULL,   -- 'income' (wallet only) | 'settlement' (credit paid down from wallet) |
                                -- 'correction' (any account — a direct balance fix, see correct_*_balance)
  note        TEXT,
  raw_message TEXT,
  source      TEXT DEFAULT 'telegram'
);
"""

# columns that may be missing on a DB created by an earlier version of the app
TRANSACTION_MIGRATIONS = {
    "payment_source": "TEXT DEFAULT 'wallet'",
    "expense_type": "TEXT DEFAULT 'variable'",
    "cadence": "TEXT",
}

CATEGORY_MIGRATIONS = {
    "parent_category": "TEXT REFERENCES categories(name)",
    "target_amount": "REAL",
    "target_date": "TEXT",
    "expense_type": "TEXT DEFAULT 'variable'",
    "cadence": "TEXT",
    "account_link": "TEXT",
}

INVESTMENT_SNAPSHOT_MIGRATIONS = {
    "contributed_override": "REAL",
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


def _backfill_expense_type_cadence(conn, existing_txn_cols):
    """One-time backfill when expense_type/cadence are freshly added: derive
    them from the legacy categories.kind (still present at this point,
    before _migrate_categories touches it) + transactions.recurrence, so
    existing data keeps its meaning under the new single taxonomy instead of
    silently resetting to the 'variable' default."""
    cat_cols = {row["name"] for row in conn.execute("PRAGMA table_info(categories)")}
    cat_kind = {}
    if "kind" in cat_cols:
        for row in conn.execute("SELECT name, kind FROM categories"):
            cat_kind[row["name"]] = row["kind"]
    has_recurrence = "recurrence" in existing_txn_cols
    query = "SELECT id, category" + (", recurrence" if has_recurrence else "") + " FROM transactions"
    for row in conn.execute(query).fetchall():
        kind = cat_kind.get(row["category"])
        recurrence = row["recurrence"] if has_recurrence else "one-off"
        if kind in ("saving", "liquid"):
            expense_type = "saving"
            cadence = "monthly" if recurrence == "monthly" else ("annual" if recurrence == "yearly" else None)
        elif recurrence == "monthly":
            expense_type, cadence = "fixed", "monthly"
        elif recurrence == "yearly":
            expense_type, cadence = "fixed", "annual"
        else:
            # old recurrence='one-off' covered both routine variable spend
            # and true anomalies (travel, a big one-time purchase) — that
            # distinction didn't exist before, so default to 'variable' (the
            # safer/more common case) and let the user re-tag specific past
            # rows to 'one-off' going forward.
            expense_type, cadence = "variable", None
        conn.execute(
            "UPDATE transactions SET expense_type = ?, cadence = ? WHERE id = ?",
            (expense_type, cadence, row["id"]),
        )


def _migrate_transactions(conn):
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)")}
    is_fresh_tagging = "expense_type" not in existing
    for column, decl in TRANSACTION_MIGRATIONS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {column} {decl}")

    if is_fresh_tagging:
        _backfill_expense_type_cadence(conn, existing)

    # 'salary' was the old name for the wallet payment source.
    conn.execute("UPDATE transactions SET payment_source = 'wallet' WHERE payment_source = 'salary'")

    # recurrence/period are fully superseded by expense_type+cadence — drop
    # them if this SQLite build supports DROP COLUMN (3.35+); a no-op catch
    # on older builds since app code never reads them again either way.
    for legacy_col in ("recurrence", "period"):
        if legacy_col in existing:
            try:
                conn.execute(f"ALTER TABLE transactions DROP COLUMN {legacy_col}")
            except sqlite3.OperationalError:
                pass


def _migrate_categories(conn):
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(categories)")}
    is_fresh_expense_type = "expense_type" not in existing
    for column, decl in CATEGORY_MIGRATIONS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE categories ADD COLUMN {column} {decl}")

    if is_fresh_expense_type and "kind" in existing:
        for row in conn.execute("SELECT name, kind FROM categories").fetchall():
            if row["kind"] == "saving":
                expense_type, account_link = "saving", None
            elif row["kind"] == "liquid":
                expense_type, account_link = "saving", "liquid"
            else:
                expense_type, account_link = "variable", None
            conn.execute(
                "UPDATE categories SET expense_type = ?, account_link = ? WHERE name = ?",
                (expense_type, account_link, row["name"]),
            )

    if "kind" in existing:
        try:
            conn.execute("ALTER TABLE categories DROP COLUMN kind")
        except sqlite3.OperationalError:
            pass


def _migrate_bills_to_fixed(conn):
    # kind couldn't distinguish 'fixed' from 'variable' (it only ever split
    # essential/discretionary), so the generic backfill above defaults every
    # non-saving category to 'variable' — including 'Bills', whose seed
    # keywords (electricity, rent, wifi, emi) are quintessentially fixed
    # costs. One-time targeted fix, guarded so it never fights a user's own
    # later edit back to 'variable' via the Categories panel.
    already_migrated = conn.execute(
        "SELECT 1 FROM settings WHERE key = 'migrated_bills_to_fixed'"
    ).fetchone()
    if already_migrated:
        return
    conn.execute(
        "UPDATE categories SET expense_type = 'fixed', cadence = 'monthly' "
        "WHERE name = 'Bills' AND expense_type = 'variable'"
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('migrated_bills_to_fixed', '1')"
    )


def _migrate_investments_kind(conn):
    # Fixes a DB created before Investments was reclassified 'essential' ->
    # 'saving', back when 'kind' was still the taxonomy. Must run before
    # _migrate_transactions/_migrate_categories, which read 'kind' one last
    # time to backfill expense_type — otherwise Investments' transactions
    # and category would backfill to 'variable' instead of 'saving'.
    # No-ops on a fresh install (categories never had a 'kind' column) or
    # once already applied (guarded so it never fights a user's own later
    # edit back to 'essential' via the Categories panel).
    cat_cols = {row["name"] for row in conn.execute("PRAGMA table_info(categories)")}
    if "kind" not in cat_cols:
        return
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


def _migrate_investment_snapshot_columns(conn):
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(investment_snapshots)")}
    for column, decl in INVESTMENT_SNAPSHOT_MIGRATIONS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE investment_snapshots ADD COLUMN {column} {decl}")


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Must run before _migrate_transactions/_migrate_categories, which
        # read the legacy 'kind' column one last time to backfill
        # expense_type — otherwise Investments would backfill to 'variable'.
        _migrate_investments_kind(conn)
        _migrate_transactions(conn)
        _migrate_categories(conn)
        _migrate_bills_to_fixed(conn)
        _migrate_investment_snapshots(conn)
        _migrate_investment_snapshot_columns(conn)

        for name, meta in config.DEFAULT_CATEGORIES.items():
            conn.execute(
                "INSERT OR IGNORE INTO categories (name, color, expense_type, cadence, monthly_cap) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, meta["color"], meta["expense_type"], meta["cadence"], meta["monthly_cap"]),
            )

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
                        source="telegram", guessed=False, payment_source="wallet",
                        expense_type="variable", cadence=None):
    spent_on = spent_on or datetime.now().strftime("%Y-%m-%d")
    ts = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO transactions (ts, spent_on, category, note, amount, "
            "raw_message, source, guessed, payment_source, expense_type, cadence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, spent_on, category, note, amount, raw_message, source,
             int(guessed), payment_source, expense_type, cadence),
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
                        payment_source=None, expense_type=None, cadence=-1, spent_on=None):
    """Full edit of a logged transaction from the dashboard. Any field left
    as None keeps its current value (cadence uses -1 as its 'leave unchanged'
    sentinel since None/clearing cadence is itself a valid value). Clears
    `guessed` since an edit is an explicit human correction, not a Gemini
    fallback anymore."""
    with get_conn() as conn:
        current = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        if not current:
            return False
        conn.execute(
            "UPDATE transactions SET category = ?, note = ?, amount = ?, "
            "payment_source = ?, expense_type = ?, cadence = ?, spent_on = ?, guessed = 0 WHERE id = ?",
            (
                category if category is not None else current["category"],
                note if note is not None else current["note"],
                amount if amount is not None else current["amount"],
                payment_source if payment_source is not None else current["payment_source"],
                expense_type if expense_type is not None else current["expense_type"],
                cadence if cadence != -1 else current["cadence"],
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


def get_all_transactions():
    """Every transaction, oldest first — for the Excel export's full history."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM transactions ORDER BY spent_on, id").fetchall()
        return [dict(r) for r in rows]


def get_all_months():
    """Every distinct 'YYYY-MM' with any activity (transactions or account
    income/settlements), sorted oldest first — for the Excel export's
    month-by-month sheets, which shouldn't assume a fixed lookback window."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT substr(spent_on, 1, 7) AS month FROM transactions "
            "UNION SELECT DISTINCT substr(txn_date, 1, 7) AS month FROM account_transactions "
            "ORDER BY month"
        ).fetchall()
        return [r["month"] for r in rows]


def get_recent_transactions(limit=20):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_monthly_totals(months, expense_type=None, category=None):
    """Returns [{month, total}] for the given list of 'YYYY-MM' strings.
    If expense_type is given, only sums transactions with that expense_type
    (e.g. 'saving' for the savings trend). If category is given, only sums
    that one category (for the per-category drill-down)."""
    with get_conn() as conn:
        out = []
        for month in months:
            if category:
                row = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) AS total FROM transactions "
                    "WHERE spent_on LIKE ? AND category = ?",
                    (f"{month}%", category),
                ).fetchone()
            elif expense_type == "saving":
                # excludes account_link='liquid' saving categories — those
                # feed the Liquid balance (see get_liquid_balance), not this
                # investment-vehicle trend.
                row = conn.execute(
                    "SELECT COALESCE(SUM(t.amount), 0) AS total FROM transactions t "
                    "JOIN categories c ON c.name = t.category "
                    "WHERE t.spent_on LIKE ? AND t.expense_type = 'saving' AND c.account_link IS NULL",
                    (f"{month}%",),
                ).fetchone()
            elif expense_type:
                row = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) AS total FROM transactions "
                    "WHERE spent_on LIKE ? AND expense_type = ?",
                    (f"{month}%", expense_type),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) AS total FROM transactions "
                    "WHERE spent_on LIKE ?",
                    (f"{month}%",),
                ).fetchone()
            out.append({"month": month, "total": row["total"]})
        return out


def _contributed_for_category_as_of(category, month):
    """A vehicle's running 'contributed' total as of `month`: the manual
    contributed_override at or before this month (if one was ever set),
    plus whatever's been logged since — or, absent any override, the plain
    all-time sum of the vehicle's transactions. The override exists because
    the auto-computed sum only sees transactions logged through this app; a
    vehicle that already held money before the user started tracking it
    here needs a way to back-fill that pre-app principal (see set_investment_snapshot)."""
    override = get_contributed_override_as_of(category, month)
    if override:
        since = get_contributions_between(category, override["month"], month)
        return override["contributed_override"] + since
    return get_contributions_between(category, None, month)


def get_cumulative_savings_contributions(months, category=None):
    """Returns [{month, contributed}] where contributed is the all-time
    running total through the end of that month (not just that month's own
    contribution) — of real investment-vehicle transactions (expense_type=
    'saving', account_link IS NULL — excludes liquid-linked saving
    categories), or of one category's transactions if `category` is given
    (for a single investment vehicle). Honors each vehicle's own manual
    contributed_override, if any (see _contributed_for_category_as_of)."""
    if category:
        categories_to_sum = [category]
    else:
        with get_conn() as conn:
            categories_to_sum = [
                row["name"] for row in conn.execute(
                    "SELECT name FROM categories WHERE expense_type = 'saving' AND account_link IS NULL"
                )
            ]
    out = []
    for month in months:
        total = sum(_contributed_for_category_as_of(cat, month) for cat in categories_to_sum)
        out.append({"month": month, "contributed": total})
    return out


def get_liquid_balance(through_month=None):
    """Liquid account balance: cumulative deposits into account_link='liquid'
    saving categories, minus cumulative expenses paid *from* the liquid
    account (payment_source='liquid' — e.g. a one-off anomaly drawn from
    it), plus any direct balance corrections (see correct_liquid_balance),
    optionally only counting through the end of a given month. Carries
    forward automatically since it's a running sum, not a monthly reset."""
    with get_conn() as conn:
        date_clause = " AND spent_on <= ?" if through_month else ""
        params = (f"{through_month}-31",) if through_month else ()
        deposits = conn.execute(
            "SELECT COALESCE(SUM(t.amount), 0) AS total FROM transactions t "
            "JOIN categories c ON c.name = t.category "
            f"WHERE c.account_link = 'liquid'{date_clause}",
            params,
        ).fetchone()["total"]
        withdrawals = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM transactions "
            f"WHERE payment_source = 'liquid'{date_clause}",
            params,
        ).fetchone()["total"]
        date_clause2 = " AND txn_date <= ?" if through_month else ""
        corrections = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM account_transactions "
            f"WHERE account = 'liquid' AND txn_kind = 'correction'{date_clause2}",
            params,
        ).fetchone()["total"]
        return deposits - withdrawals + corrections


DEFAULT_LIQUID_CATEGORY = "Liquid Fund"


def get_or_create_liquid_category():
    """Returns the name of an account_link='liquid' category, creating a
    default one (DEFAULT_LIQUID_CATEGORY) if none exists yet — so logging a
    liquid deposit never requires the user to set up a category first."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM categories WHERE account_link = 'liquid' ORDER BY name LIMIT 1"
        ).fetchone()
        if row:
            return row["name"]
    add_category(DEFAULT_LIQUID_CATEGORY, "#818cf8", "saving", account_link="liquid")
    return DEFAULT_LIQUID_CATEGORY


def deposit_to_liquid(amount, note=None, raw_message=None, spent_on=None, source="telegram"):
    """Move money from the Wallet into the Liquid reserve: a single
    transaction that both counts against the Wallet (payment_source='wallet'
    — real money left it) and deposits into the Liquid balance (its category
    is account_link='liquid'). Returns the new transaction's id."""
    category = get_or_create_liquid_category()
    return insert_transaction(
        category=category, note=note, amount=amount, raw_message=raw_message,
        spent_on=spent_on, source=source, payment_source="wallet", expense_type="saving",
    )


def get_fixed_monthly_costs():
    """Latest logged instance of each distinct (category, note) pair tagged
    expense_type='fixed' cadence='monthly' — a snapshot of current fixed
    monthly costs (rent, subscriptions), not a growing history, since the
    user re-logs these every month. Grouped by (category, note) rather than
    just category so two different fixed costs in the same category (e.g.
    Rent and a maintenance fee, both 'Bills') don't collapse into one."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT t.* FROM transactions t "
            "INNER JOIN ("
            "  SELECT category, COALESCE(note, '') AS note_key, MAX(id) AS max_id "
            "  FROM transactions WHERE expense_type = 'fixed' AND cadence = 'monthly' "
            "  GROUP BY category, COALESCE(note, '')"
            ") latest ON t.id = latest.max_id "
            "ORDER BY t.amount DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_annual_costs(limit=100):
    """All-time list of expense_type='fixed' cadence='annual' transactions,
    most recent first — these are rare enough that a full history stays
    readable."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE expense_type = 'fixed' AND cadence = 'annual' "
            "ORDER BY spent_on DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_oneoff_transactions(months=None, limit=100):
    """All-time (or scoped to given months) list of expense_type='one-off'
    transactions — anomalous, unplanned spend (travel, a big one-time
    purchase) that shouldn't skew the monthly budget/projection."""
    with get_conn() as conn:
        if months:
            placeholders = ",".join("?" * len(months))
            rows = conn.execute(
                f"SELECT * FROM transactions WHERE expense_type = 'one-off' "
                f"AND substr(spent_on, 1, 7) IN ({placeholders}) "
                "ORDER BY spent_on DESC, id DESC LIMIT ?",
                (*months, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE expense_type = 'one-off' "
                "ORDER BY spent_on DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


# ---- categories ---------------------------------------------------------

def get_categories():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM categories").fetchall()
        return [dict(r) for r in rows]


def get_category(name):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM categories WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None


def get_category_names():
    with get_conn() as conn:
        rows = conn.execute("SELECT name FROM categories").fetchall()
        return [r["name"] for r in rows]


def add_category(name, color, expense_type, cadence=None, monthly_cap=None, parent_category=None,
                  target_amount=None, target_date=None, account_link=None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO categories (name, color, expense_type, cadence, monthly_cap, "
            "parent_category, target_amount, target_date, account_link) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, color, expense_type, cadence, monthly_cap, parent_category,
             target_amount, target_date, account_link),
        )


def update_category(name, color=None, expense_type=None, cadence=-1, monthly_cap=-1,
                     parent_category=-1, target_amount=-1, target_date=-1, account_link=-1):
    """-1 is the sentinel for 'leave unchanged' on nullable fields, since
    None is itself a valid value (no cadence/cap/parent/target/account_link)."""
    with get_conn() as conn:
        current = conn.execute(
            "SELECT * FROM categories WHERE name = ?", (name,)
        ).fetchone()
        if not current:
            return False
        conn.execute(
            "UPDATE categories SET color = ?, expense_type = ?, cadence = ?, monthly_cap = ?, "
            "parent_category = ?, target_amount = ?, target_date = ?, account_link = ? WHERE name = ?",
            (
                color if color is not None else current["color"],
                expense_type if expense_type is not None else current["expense_type"],
                cadence if cadence != -1 else current["cadence"],
                monthly_cap if monthly_cap != -1 else current["monthly_cap"],
                parent_category if parent_category != -1 else current["parent_category"],
                target_amount if target_amount != -1 else current["target_amount"],
                target_date if target_date != -1 else current["target_date"],
                account_link if account_link != -1 else current["account_link"],
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


# ---- accounts: wallet (income) / credit (settlement) ----------------------
#
# Wallet and Credit are real running-balance accounts, distinct from the
# per-expense `transactions` table: wallet gains money via logged income
# (salary, bonus, freelance, refund — always positive) and loses it both to
# expenses paid payment_source='wallet' and to credit settlements; credit is
# revolving debt that only grows via expenses paid payment_source='credit'
# and shrinks via an explicit settlement, so an unsettled balance carries
# forward across months rather than resetting. Liquid has no such ledger —
# it's tracked via account_link='liquid' categories (see get_liquid_balance).

def add_account_transaction(account, amount, txn_kind, note=None, raw_message=None,
                             txn_date=None, source="telegram"):
    txn_date = txn_date or datetime.now().strftime("%Y-%m-%d")
    ts = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO account_transactions (ts, txn_date, account, amount, txn_kind, "
            "note, raw_message, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, txn_date, account, amount, txn_kind, note, raw_message, source),
        )
        return cur.lastrowid


def get_account_transactions(account=None, limit=50):
    with get_conn() as conn:
        if account:
            rows = conn.execute(
                "SELECT * FROM account_transactions WHERE account = ? ORDER BY id DESC LIMIT ?",
                (account, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM account_transactions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def log_income(amount, note=None, raw_message=None, txn_date=None, source="telegram"):
    """Log a deposit into the wallet — salary, bonus, freelance, a refund."""
    return add_account_transaction("wallet", amount, "income", note=note,
                                    raw_message=raw_message, txn_date=txn_date, source=source)


def settle_credit(amount, note=None, txn_date=None, source="telegram"):
    """Pay down credit by `amount` from the wallet: records both sides of
    the transfer (credit outstanding decreases, wallet balance decreases)."""
    txn_date = txn_date or datetime.now().strftime("%Y-%m-%d")
    add_account_transaction("credit", amount, "settlement", note=note, txn_date=txn_date, source=source)
    add_account_transaction("wallet", -amount, "settlement", note=note or "Credit card settlement",
                             txn_date=txn_date, source=source)


def get_wallet_balance(through_month=None):
    """Cumulative wallet balance: all logged income (and settlement
    outflows) in account_transactions, minus all expense transactions paid
    from the wallet. Carries forward month to month like a real account."""
    with get_conn() as conn:
        date_clause = " AND txn_date <= ?" if through_month else ""
        params = (f"{through_month}-31",) if through_month else ()
        income = conn.execute(
            f"SELECT COALESCE(SUM(amount), 0) AS total FROM account_transactions WHERE account = 'wallet'{date_clause}",
            params,
        ).fetchone()["total"]
        date_clause2 = " AND spent_on <= ?" if through_month else ""
        spent = conn.execute(
            f"SELECT COALESCE(SUM(amount), 0) AS total FROM transactions WHERE payment_source = 'wallet'{date_clause2}",
            params,
        ).fetchone()["total"]
        return income - spent


def get_credit_outstanding(through_month=None):
    """Credit is revolving: outstanding = all-time charges minus all-time
    settlements, carried forward until paid off — never resets on its own
    at month boundaries."""
    with get_conn() as conn:
        date_clause = " AND spent_on <= ?" if through_month else ""
        params = (f"{through_month}-31",) if through_month else ()
        charged = conn.execute(
            f"SELECT COALESCE(SUM(amount), 0) AS total FROM transactions WHERE payment_source = 'credit'{date_clause}",
            params,
        ).fetchone()["total"]
        date_clause2 = " AND txn_date <= ?" if through_month else ""
        settled = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM account_transactions "
            f"WHERE account = 'credit' AND txn_kind IN ('settlement', 'correction'){date_clause2}",
            params,
        ).fetchone()["total"]
        return charged - settled


# ---- direct balance corrections (Wallet/Credit/Liquid) --------------------
#
# A correction is a neutral "set the balance to X" action, distinct from an
# income/settlement/deposit — it doesn't represent money actually moving, just
# fixing drift between what the app has tracked and the real-world balance
# (e.g. after starting to use the app partway through an account's history).
# Each function computes the signed delta needed and records it the same way
# an income/settlement would, so it carries forward through the normal
# balance queries above with no special-casing at read time.

def correct_wallet_balance(new_balance, note=None, txn_date=None, source="dashboard"):
    delta = new_balance - get_wallet_balance()
    return add_account_transaction("wallet", delta, "correction", note=note or "Balance correction",
                                    txn_date=txn_date, source=source)


def correct_credit_outstanding(new_outstanding, note=None, txn_date=None, source="dashboard"):
    # settled/corrections are subtracted from charged (see get_credit_outstanding), so
    # the delta needed to land on new_outstanding is the current outstanding minus it —
    # the opposite sign of a plain "new - current" balance delta.
    delta = get_credit_outstanding() - new_outstanding
    return add_account_transaction("credit", delta, "correction", note=note or "Balance correction",
                                    txn_date=txn_date, source=source)


def correct_liquid_balance(new_balance, note=None, txn_date=None, source="dashboard"):
    delta = new_balance - get_liquid_balance()
    return add_account_transaction("liquid", delta, "correction", note=note or "Balance correction",
                                    txn_date=txn_date, source=source)


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


def set_investment_snapshot(category, month, value, note=None, contributed_override=None):
    """Returns the updated_at timestamp that was written. `contributed_override`
    is optional and, when omitted (None), preserves whatever override was
    already set for this (category, month) — a plain value update (e.g. the
    bot's /portfolio command) never wipes out a correction made elsewhere."""
    updated_at = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO investment_snapshots (category, month, value, note, contributed_override, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(category, month) DO UPDATE SET value = excluded.value, "
            "note = excluded.note, "
            "contributed_override = COALESCE(excluded.contributed_override, investment_snapshots.contributed_override), "
            "updated_at = excluded.updated_at",
            (category, month, value, note, contributed_override, updated_at),
        )
    return updated_at


def get_contributed_override_as_of(category, month):
    """The most recent manual 'contributed' correction at or before `month`,
    or None if the vehicle has never had one set (in which case the plain
    sum-of-transactions figure is used as-is)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT month, contributed_override FROM investment_snapshots "
            "WHERE category = ? AND month <= ? AND contributed_override IS NOT NULL "
            "ORDER BY month DESC LIMIT 1",
            (category, month),
        ).fetchone()
        return dict(row) if row else None


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
