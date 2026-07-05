# CLAUDE.md — Personal Cashflow Ledger

A self-hosted, single-user expense tracker. Expenses are logged by chatting with a
**Telegram bot**; a **web dashboard** shows spending analytics. Everything runs
locally in **Termux on an Android tablet**. No cloud accounts, no auth, no PC.

---

## 1. Core principles (read first)

1. **Decoupled by design.** Three independent layers that only share the SQLite file:
   - **Ingestion** (Telegram bot) → parses a message → writes a row.
   - **Storage** (SQLite, single file).
   - **Presentation** (FastAPI + static dashboard) → only *reads* the DB.
   This means the messaging layer can later be swapped for WhatsApp by rewriting
   **only `bot.py`** — nothing else. Keep it that way.

2. **Termux-friendly.** Assume a phone/tablet CPU, no GPU, flaky background execution,
   and no public IP. Prefer pure-Python, lightweight deps. The bot uses Telegram
   **long-polling** (reaches out to Telegram; never needs an inbound public URL).

3. **AI is a fallback, not the default.** Parsing is **regex-first** (free, instant).
   Gemini's free tier is called **only** when regex can't categorise a message, and
   for generating the monthly insight text. Most logging must cost zero API calls.

4. **Cheap to run.** Target: near-zero Gemini calls in normal daily use.

---

## 2. Tech stack (do not substitute without asking)

- **Language:** Python 3.11+
- **Bot:** `python-telegram-bot` (v21+, async), long-polling
- **Web:** `FastAPI` + `uvicorn`, serving a single static HTML dashboard
- **DB:** `sqlite3` (stdlib), one file `ledger.db`
- **AI (optional):** Google Gemini free tier via **plain REST calls** (`requests`),
  *not* the official `google-genai` SDK — that SDK pulls in `google-auth` →
  `cryptography`, a package with compiled Rust native code whose PyPI wheel is built
  for glibc and fails to `dlopen` on Termux's Bionic-libc Python. API-key auth needs no
  OAuth/JWT signing, so a bare HTTPS POST (`parser.call_gemini()`) avoids that whole
  native-dependency chain. Don't reintroduce `google-genai` without checking it
  actually installs and imports cleanly on-device first.
- **Frontend:** single `index.html` — **Tailwind (CDN)** + **Chart.js (CDN)**, dark theme
- **Export:** `openpyxl` (pure Python, no native deps) for the Excel workbook export
- **Process:** one entrypoint (`main.py`) runs bot **and** server together via asyncio

> ⚠️ **Verify at build time:** the current free Gemini model name and the
> `python-telegram-bot` API surface change over time. Before coding those parts, check
> the current free Gemini model name and the current PTB v21+ async API. Don't rely on
> memorised snippets.

---

## 3. How it runs (operational model)

- One command in Termux (`bash run.sh`) starts **both** the Telegram bot loop and the
  dashboard server on `localhost:8000`.
- **Logging:** user opens Telegram on *any* device (phone, web, another tablet) and
  messages the bot. Routes through Telegram's cloud → bot pulls it via polling.
- **Viewing:** user opens `http://localhost:8000` in the tablet browser.
- **Optional phone view:** same-wifi → open the tablet's LAN IP (e.g.
  `http://192.168.x.x:8000`). Bind uvicorn to `0.0.0.0` to allow this.
- **Staying alive:** `run.sh` calls `termux-wake-lock` so the bot survives screen-off.
  If Termux is killed, Telegram queues messages ~24h and the bot catches up on restart.

---

## 4. Project structure

```
expense-tracker/
├── CLAUDE.md            # this file
├── .env.example         # copy to .env and fill in
├── requirements.txt
├── run.sh               # termux-wake-lock + launch main.py
├── config.py            # env, categories, colors, keyword aliases
├── db.py                # schema init + all queries
├── parser.py            # regex-first parse + Gemini fallback classify
├── insights.py          # compute metrics + phrase insight bullets (no Gemini)
├── ai_insights.py       # AI daily recap: cached, ~1 Gemini call/day, rule-based fallback
├── export.py            # multi-sheet Excel export (openpyxl), reads db.py only
├── bot.py               # Telegram handlers  ← ONLY file to swap for WhatsApp
├── server.py            # FastAPI app + JSON API + serves dashboard
├── main.py              # entrypoint: asyncio.gather(bot, uvicorn)
└── static/
    └── index.html       # dashboard (Tailwind + Chart.js, single file)
```

---

## 5. Data model (SQLite)

```sql
CREATE TABLE IF NOT EXISTS transactions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ts             TEXT NOT NULL,          -- ISO datetime the row was logged
  spent_on       TEXT NOT NULL,          -- date of the expense (YYYY-MM-DD), default today
  category       TEXT NOT NULL,          -- must match a categories.name
  note           TEXT,                   -- e.g. "gym", "Zomato lunch"
  amount         REAL NOT NULL,          -- in rupees
  raw_message    TEXT,                   -- original message, for debugging/undo
  source         TEXT DEFAULT 'telegram',
  guessed        INTEGER DEFAULT 0,      -- 1 if category came from the Gemini fallback
  payment_source TEXT DEFAULT 'wallet',  -- 'wallet' | 'credit' | 'liquid' — which account paid for it
  expense_type   TEXT DEFAULT 'variable',-- 'fixed' | 'variable' | 'one-off' | 'saving'
  cadence        TEXT                    -- 'daily' | 'weekly' | 'monthly' | 'annual' | NULL
);

CREATE TABLE IF NOT EXISTS categories (
  name            TEXT PRIMARY KEY,
  color           TEXT NOT NULL,         -- hex, used by charts
  expense_type    TEXT NOT NULL,         -- default expense_type for new transactions in this category
  cadence         TEXT,                  -- default cadence for new transactions in this category
  monthly_cap     REAL,                  -- budget for Budgets & Alerts (nullable)
  parent_category TEXT REFERENCES categories(name),  -- nests an expense_type='saving' vehicle
                                          -- (SIP, a gold plan) under another, e.g. 'Investments' —
                                          -- organizational only, doesn't change its own tags/keywords/transactions
  target_amount   REAL,                  -- optional savings/investment goal for this category
  target_date     TEXT,                  -- optional 'YYYY-MM-DD' goal date
  account_link    TEXT                   -- NULL | 'liquid': this saving category's deposits also
                                          -- feed the Liquid account balance (see §5b)
);

CREATE TABLE IF NOT EXISTS keywords (
  keyword  TEXT PRIMARY KEY,          -- e.g. "zomato"
  category TEXT NOT NULL REFERENCES categories(name)
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);
-- seed: monthly_salary=60000, credit_limit=0, currency='INR', timezone='Asia/Kolkata'
-- monthly_salary is a *reference* income target for %-used displays, not the Wallet's
-- real balance — the Wallet's actual balance comes from logged income (see §5b).

CREATE TABLE IF NOT EXISTS investment_snapshots (
  category   TEXT NOT NULL,  -- an expense_type='saving' category name — each is its own "vehicle"
  month      TEXT NOT NULL,  -- 'YYYY-MM'
  value      REAL NOT NULL,  -- total value of this vehicle as of this month (manually entered)
  note       TEXT,
  updated_at TEXT,           -- when last set, for "updated N days ago"
  PRIMARY KEY (category, month)
);

CREATE TABLE IF NOT EXISTS account_transactions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  txn_date    TEXT NOT NULL,   -- 'YYYY-MM-DD'
  account     TEXT NOT NULL,   -- 'wallet' | 'credit'
  amount      REAL NOT NULL,   -- signed: + increases the account's balance-in-your-favor, - decreases
  txn_kind    TEXT NOT NULL,   -- 'income' (wallet only) | 'settlement' (credit paid down from wallet)
  note        TEXT,
  raw_message TEXT,
  source      TEXT DEFAULT 'telegram'
);
```

**Default categories** (seed on first run; colors mirror the reference UI):

| name        | color     | expense_type | cadence | example keywords                     |
|-------------|-----------|--------------|---------|---------------------------------------|
| Food        | `#f59e0b` | variable     | —       | zomato, swiggy, lunch, dinner, cafe   |
| Groceries   | `#22c55e` | variable     | —       | dmart, bigbasket, blinkit, grocery    |
| Bills       | `#3b82f6` | fixed        | monthly | electricity, rent, wifi, recharge     |
| Luxuries    | `#a855f7` | variable     | —       | gym, netflix, spotify, shopping       |
| Health      | `#ef4444` | variable     | —       | pharmeasy, medicine, doctor, apollo   |
| Travel      | `#06b6d4` | variable     | —       | ola, uber, metro, flight, fuel        |
| Investments | `#eab308` | saving       | monthly | sip, index fund, stocks, mutual fund  |
| Other       | `#64748b` | variable     | —       | (fallback bucket)                     |

### 5a. Expense tags: `expense_type` + `cadence`

Every transaction carries two independent tags, replacing the old `kind` (essential/
discretionary/saving/liquid) + `recurrence` (one-off/monthly/yearly) taxonomy entirely —
having two overlapping systems was itself a source of inconsistency, so this consolidates
into one:

- **`expense_type`** — what kind of spend this is:
  - `fixed` — locked-in, happens regardless (rent, utilities, subscriptions). You don't
    choose it month-to-month.
  - `variable` — routine day-to-day spend (food, groceries, transport) — the thing
    "cutting back" insights are actually about.
  - `one-off` — an anomaly: a trip, a big one-time purchase (a phone, a gym membership
    paid once). Not a monthly pattern, so it's **excluded entirely** from the projected
    month-end pace (see §9) rather than skewing it. Ideally paid from the Liquid
    account (§5b), since it's unplanned and shouldn't eat into the routine budget.
  - `saving` — a deliberate, usually recurring/planned contribution (a SIP, a gold
    scheme, an emergency-fund deposit).
- **`cadence`** — how often it recurs: `daily` | `weekly` | `monthly` | `annual` | none.
  Mostly meaningful for `fixed` (rent = monthly, an annual gym membership = annual) and
  `saving` (a SIP = monthly); `one-off` never has one; `variable` rarely does.

Each **category** carries its own default `expense_type`/`cadence` (set once via 🏷
Categories) so routine logging doesn't need a tag every time — "rent 21500" inherits
Bills' `fixed`/`monthly` defaults automatically. An explicit keyword in the message (or
a field edit in Recent Activity) overrides just that one transaction (see §6).

`expense_type='saving'` categories still count fully against wallet/credit/liquid like
any other expense — money that actually left the account — but are **excluded** from
the "where the money goes" donut, the Top category card, and the "largest single
hit"/"cutting X% saves Y" insights, since those are about spending habits, not saving
contributions. `compute_metrics()` exposes both `total` (everything, including saving —
used for account tracking) and `spend_total` (excludes saving — used for the
donut/top-category/largest-hit).

### 5b. Accounts: Wallet, Credit, Liquid

Three "accounts" a transaction's `payment_source` can draw from — this is the
"wallet/accounts" model, replacing the old fixed `monthly_salary` number as the source
of truth for what you actually have:

- **Wallet** — a real running balance. Money goes **in** via logged income (salary,
  bonus, freelance, a refund — `db.log_income()` / `/income` / dashboard "+ Add
  income"), and **out** via any expense with `payment_source='wallet'` or a credit
  settlement paid from it. Balance = `db.get_wallet_balance()`, all-time cumulative
  through the selected month — carries forward automatically, never resets.
- **Credit** — revolving debt, not a monthly reset. `credit_outstanding` = all-time
  charges (`payment_source='credit'`) minus all-time settlements
  (`account_transactions` where `account='credit', txn_kind='settlement'`) — an
  unsettled balance from a prior month still eats into the same `credit_limit` ceiling
  until you explicitly settle it (`db.settle_credit()` / `/settle` / "Settle credit").
  Settling records **two** ledger rows: `+amount` on `credit` (reduces what's owed) and
  `-amount` on `wallet` (the money that paid it).
- **Liquid** — a spendable cash reserve, *not* a category you log expenses "into" the
  old way. Deposits come from any `expense_type='saving'` category with
  `account_link='liquid'` set (e.g. an "Emergency Fund" category — depositing into it
  is still just a normal transaction, tagged `saving`, but the category's
  `account_link` routes it into the Liquid balance too). Withdrawals are any expense
  with `payment_source='liquid'` — this is where `one-off` anomalies should ideally be
  paid from. Balance = `db.get_liquid_balance()`: cumulative deposits minus cumulative
  liquid-paid withdrawals.

**Payday / settlement reminder:** `insights.payday_date()` computes the 25th of the
current month, or the last working day (Mon–Fri) before it if the 25th falls on a
weekend. `insights.credit_settlement_status()` flags `needs_settlement=True` once
today is on/after that date and `credit_outstanding > 0` — the dashboard shows a
prominent banner from that point (§8) and the Credit card carries an "unsettled" state.

`account_transactions` is deliberately separate from `transactions`: income and
settlements aren't "expenses" in a category, they're account-level money movements.
Liquid doesn't get its own ledger table — it's simpler to reuse the existing
category/transaction mechanism (via `account_link`) since a liquid deposit already
looks exactly like logging a `saving` transaction.

### 5c. Long-term investment vehicles (unaffected by the accounts rework)

`investment_snapshots` powers **investment tracking, per vehicle**: since there's no
way to pull real brokerage/mutual-fund values, the user manually logs each vehicle's
total value once in a while (via `/portfolio <vehicle> <amount>` or the dashboard's
per-vehicle "Update value" field) — every `expense_type='saving'` category *without* an
`account_link` (i.e. a real investment vehicle, not a Liquid-linked deposit category)
is tracked independently (its own cumulative contribution, its own value history, its
own "updated N days ago"), since a user may run several vehicles (a SIP, a gold scheme,
FDs) that shouldn't be conflated into one blended number. `server.py`'s `_is_vehicle()`
is the single predicate for "is this a real vehicle" used everywhere (the Savings
section's vehicle list, `/api/investments`, the Category Breakdown's savings-vehicle
exclusion). The dashboard also shows a combined trend: cumulative contributed vs.
summed value across vehicles, forward-filling each vehicle's last known value between
updates (`GET /api/investments`) — the gap is gain or loss. The dashboard's
`#vehicle-chart-select` dropdown re-points the same chart at a single vehicle's own
`months` series instead of the combined one, and its per-vehicle "Update value"/chart
controls only show for whichever vehicle is selected — not all of them stacked at once.

A vehicle's **gain is incremental, never since-inception**: the app can't distinguish
principal from interest inside a manually-entered value, so comparing a fresh value
against all-time contributed fabricates a huge "gain" the moment a vehicle has any
pre-existing balance the app never logged. `_vehicle_gain()` in `server.py` instead
computes `gain = new_value − (previous_snapshot_value + contributions_since_that_snapshot)`
— the real change since the *last* time this vehicle was valued, not since it was
created. A vehicle's first-ever snapshot has no previous to diff against, so `gain`/
`gain_pct` are `None` (a baseline, not a "gain") — the UI shows "first recorded value —
gain shows from next update" instead of fabricating a percentage. The combined
`total_gain_pct` divides by the *summed baselines* of vehicles with a known gain, not
by `total_contributed`, so it stays on the same scale as each vehicle's own `gain_pct`.

A vehicle can optionally nest under another `expense_type='saving'` category via
`parent_category` (e.g. a "Gold Reserve Plan" vehicle nested under "Investments") —
purely organizational, shown as an indented row in both the Categories panel and the
vehicle list; it doesn't affect that vehicle's own contribution/value/gain tracking,
which stays fully independent. A vehicle can also carry an optional `target_amount` /
`target_date` (set when adding it, or edited later via 🏷 Categories) — the vehicle list
shows a progress bar (`target_progress_pct` = effective value ÷ target, capped at 999%)
and the target date underneath.

Both the top **Savings** card and the Savings section's vehicle total use the same
"effective value" (`_savings_effective_balance()` in `server.py`): for each vehicle, the
manually-set value as of the selected month if one exists, else cumulative-contributed
as a floor estimate — so setting a vehicle's value immediately moves the top card too,
and the two never disagree.

Keyword→category aliases are seeded from `config.CATEGORY_KEYWORDS` into the `keywords`
table on first run, then live entirely in the DB from that point on — editable from the
dashboard's 🏷 Categories panel (add/remove keywords, add/edit/delete categories) without
touching code. `config.py` is only the seed data for a fresh database.

### 5d. Migrations (old `kind`/`recurrence`/`salary` DBs)

`db.init_db()` seeding uses `INSERT OR IGNORE`, which never touches a category that
already exists — so a seed value change after databases are already running needs an
explicit one-time fix, guarded by a settings flag so it never fights a user's own later
edit. Two of these run, in this exact order, before the general schema migration below
(both need the legacy `kind` column to still exist when they run):
`_migrate_investments_kind()` (Investments `essential`→`saving`, pre-dating this
taxonomy) and, after the general backfill, `_migrate_bills_to_fixed()` (`kind` alone
couldn't distinguish fixed from variable, so the generic backfill defaults every
non-saving category to `variable` — this targeted fix upgrades `Bills` specifically to
`fixed`/`monthly`, matching its keywords).

The general migration (`_migrate_transactions`/`_migrate_categories` in `db.py`) is a
**one-time, lossless replace**: it reads the legacy `kind` + `recurrence` columns one
last time to backfill `expense_type`/`cadence`, renames `payment_source='salary'` to
`'wallet'`, then drops the now-dead `kind`/`recurrence`/`period` columns (via `ALTER
TABLE ... DROP COLUMN`, SQLite 3.35+; a no-op on older builds since app code never reads
them again either way). Mapping: `kind='saving'` or `'liquid'` → `expense_type='saving'`
(`'liquid'` additionally sets `account_link='liquid'`); `recurrence='monthly'`/`'yearly'`
(non-saving) → `expense_type='fixed'`, `cadence='monthly'`/`'annual'`; anything else →
`expense_type='variable'` (the old `recurrence='one-off'` bucket mixed routine variable
spend with true anomalies — that distinction didn't exist before, so migrated data
defaults to `variable` and the user re-tags specific past rows to `one-off` going
forward, via the Recent Activity edit modal).

---

## 6. Message parsing (`parser.py`)

**Input examples the bot must handle:**
```
gym 1500
Zomato lunch 300
oyo 1500 travel
SIP index fund 5000
netflix 649
electricity bill 2200 credit        -> payment_source=credit
rent 21500 fixed                    -> expense_type=fixed, cadence=monthly (category default too)
gym membership 12000 yearly         -> expense_type=fixed, cadence=annual
flight to goa 15000 oneoff liquid   -> expense_type=one-off, payment_source=liquid
groceries 1200 weekly               -> cadence=weekly (expense_type stays the category default)
```

**Algorithm (regex-first):**
1. Extract **amount**: first number in the message (supports `1500`, `1,500`, `₹1500`).
2. Extract **category**: scan tokens against the keyword-alias dict (case-insensitive,
   loaded from the `keywords` table). If an explicit category name is present
   (`... travel`), that wins.
3. Extract **payment_source**: trailing `credit`/`card`/`cc` → `credit`; `liquid`/
   `emergency fund` → `liquid`; `wallet`/`salary`/`cash` or nothing mentioned →
   `wallet` (the default).
4. Extract an explicit **expense_type/cadence** keyword, if present (`extract_expense_type`
   in `parser.py`): `yearly`/`annual`/`annually` → `expense_type=fixed, cadence=annual`;
   `fixed`/`recurring`/`subscription` → `expense_type=fixed, cadence=monthly`;
   `oneoff`/`one-off`/`anomaly` → `expense_type=one-off`; `variable`/`saving`/
   `investment` → that `expense_type` directly; `daily`/`weekly` → cadence only,
   `expense_type` untouched.
5. Resolve the transaction's final tags: if step 4 found nothing, use the (now-resolved)
   category's own default `expense_type`/`cadence` (step 4 runs before Gemini
   category fallback if needed, so this lookup always happens *after* category is
   final). If step 4 found a bare `fixed`+cadence keyword but the category is already
   `saving` (e.g. "sip 5000 yearly"), keep `saving` — a cadence keyword only sets
   cadence, it never downgrades a SIP away from being a saving vehicle.
6. **note** = message with the amount, an explicit category name, payment keyword, and
   any expense-tag keyword stripped. A keyword-matched category word (e.g. "gym")
   is *kept* in the note.
7. `spent_on` = today unless the message contains a parseable date (keep simple: today only for v1).
8. If **no category** confidently found → **Gemini fallback** (see below).
9. If **no amount** found → reply asking user to include a number; do not write a row.

**Gemini fallback (only when step 8 triggers, and only if `GEMINI_API_KEY` set):**
- Prompt: category names + each category's example keywords (both pulled fresh from the
  DB, not `config.py`'s defaults — so categories the user added/renamed via the
  dashboard classify correctly too, including custom ones like "Gold Plan" whose name
  alone wouldn't hint at what belongs there) plus the note text; return exactly one
  category name as plain text. Low temperature. Cache identical notes in-memory.
- If no API key or the call fails → assign `Other` and flag the row (still log it).

Keep Gemini usage minimal and wrapped in try/except so the bot never crashes on API issues.

---

## 7. Telegram bot (`bot.py`)

**Security:** read `ALLOWED_TELEGRAM_USER_ID` from env. Ignore messages from any other
user id (the bot is public once created; this keeps strangers from injecting data).

**Behaviour:**
- Any normal text → parse → insert row → reply confirmation:
  `✅ ₹1,500 · Luxuries · gym · 💰 Wallet  (id 42)`
  Credit-tagged expenses show `💳 Credit`, liquid-tagged show `💧 Liquid`.
  `expense_type=fixed` appends `· 📌 Fixed`, `one-off` appends `· 🎲 One-off`, `saving`
  appends `· 📈 Saving` (each with `(cadence)` alongside if one is set; `variable` gets
  no badge, since it's the common case). If Gemini/`Other` fallback was used, append
  `⚠️ guessed category — reply /cat Food to fix`.

**Commands:**
| command             | action                                                        |
|---------------------|---------------------------------------------------------------|
| `/start`, `/help`   | short usage guide with examples                               |
| `/undo`             | delete the last transaction, confirm what was removed         |
| `/cat <Category>`   | change the category of the last transaction                   |
| `/today`            | quick text summary of today's spend                           |
| `/month`            | quick text summary of this month vs wallet & credit           |
| `/insights`         | run + send the monthly insight bullets (see §9)               |
| `/income <amount> [note]` | log a deposit into the Wallet (salary, bonus, freelance, ...)      |
| `/settle <amount>`  | pay down Credit from the Wallet (no arg = show outstanding)     |
| `/wallet`           | Wallet/Credit/Liquid balances + payday settlement status       |
| `/salary <amount>`  | set the *reference* monthly income used for %-used displays (no arg = show current value) |
| `/credit <amount>`  | set credit limit (no arg = show current value)                 |
| `/portfolio <vehicle> <amount>` | log this month's value for one investment vehicle, e.g. `/portfolio Investments 150000` (no args = list all vehicles + when each was last updated) |
| `/recap` (or `/recap refresh`) | AI day-by-day + cumulative analysis (see §9); cached once/day, `refresh` forces a new one |

---

## 8. Dashboard (`static/index.html` + `server.py` API)

Dark theme, matching the reference screenshots. Single HTML file, Tailwind + Chart.js
via CDN, vanilla JS `fetch` to the API. A month selector (default = current month)
re-fetches all sections. Header: **"Personal Cashflow Ledger — where every rupee went."**

A horizontal quick-nav pill bar under the header anchor-jumps between sections, since
the page has grown long. Headings use a serif display face (Fraunces, Google Fonts CDN)
against an Inter sans body — falls back to system serif/sans if the font CDN is
unreachable, a pure-CSS fallback with no JS failure mode (unlike Chart.js/Tailwind).

**Sections (top to bottom):**

0. **Payday / credit settlement banner** — shown only when
   `insights.credit_settlement_status().needs_settlement` is true (today is on/after
   this month's payday and credit is still outstanding). A prominent amber banner with
   a "Settle now" button that opens the settle-credit modal pre-filled with the
   outstanding amount.

1. **Four summary cards** — these are about *where money is sitting*, not spending
   patterns (which move to §2 instead):
   - *Wallet* — running balance (`GET /api/accounts`'s `wallet_balance`) + `₹X spent
     this month` + thin progress bar against the reference monthly income.
   - *Credit* — `credit_outstanding` (revolving, not reset monthly) + `X% of
     ₹CREDIT_LIMIT outstanding` + thin progress bar (red when over).
   - *Savings* — cumulative all-time effective value across all investment vehicles +
     `+₹X this month`.
   - *Liquid fund* — cumulative balance (deposits minus liquid-paid withdrawals) +
     `₹X in one-off spend this month` (a nudge, since one-off spend ideally comes from here).

2. **Where the money goes** — donut chart (Chart.js), total in center (an opaque
   backing box behind the center label so a hovering tooltip never visually blends with
   it — a real bug fixed this round), legend list of categories with amount + %, plus
   *Top category* and *Projected month-end* as inline stats below the legend. A
   pill-button toggle above the chart re-slices the same month by **All spend**
   (default — excludes `expense_type='saving'`, the usual spend-pattern view),
   **Wallet**, **Credit**, **Liquid** (by `payment_source`), or **Savings** (by
   investment-vehicle contributions) — `GET /api/summary?view=` returns a
   `breakdown`/`view_total` scoped to whichever slice is selected, so percentages are
   always against that slice's own total, not the whole month's spend.

3. **Spending insights** — bullet list from `insights.py` (see §9). Small status icon
   per bullet (✓ good / ⚠ watch / → note).

3b. **✨ Daily recap** — AI-generated (or rule-based fallback) day-by-day + cumulative
   analysis from `ai_insights.py` (see §9). Loads the cached recap for today on page
   load (zero extra Gemini calls); a "↻ Refresh" button force-regenerates. Shows which
   source produced it (`AI-generated` vs `Rule-based fallback`) and a timestamp.

4. **Daily burn** — line chart: cumulative spend per day vs a straight dashed
   even-pace reference line. A category dropdown (shared with §5's Month over month
   chart) drills into a single category's cumulative burn instead of the whole month;
   when filtered, the reference line uses that category's own `monthly_cap` (if any)
   instead of wallet+credit. The y-axis is scaled off the pace-*so-far*
   (`budget_per_day × days_elapsed`), not the full month's target — otherwise the
   reference line (which ends at the full monthly total) dwarfs the real spend line
   early in the month and it looks empty.

5. **Month over month + Savings trend** — two bar charts side by side: total outflow
   for the last 6 months (current month highlighted, filterable by the same category
   dropdown as §4), and the same for real investment-vehicle contributions
   (`expense_type='saving'`, excluding Liquid-linked categories) only, plus a "₹X saved
   this month" stat.

6. **One-off spend** — a flat list of this month's `expense_type='one-off'`
   transactions (`GET /api/oneoff`) plus a total. These are anomalies (a trip, a big
   one-time purchase) still counted fully against Wallet/Credit/Liquid, but *excluded*
   from the projected month-end pace (§9) since they're not a recurring monthly
   pattern — this section keeps them visible rather than silently vanishing from the
   normal numbers, and nudges toward paying them from Liquid.

7. **Savings** (was "Long-term investments" — renamed to stop the three-way naming
   clash with the top Savings card and the Savings trend chart above). Each real
   investment-vehicle category (`expense_type='saving'`, no `account_link`) is an
   independent "vehicle" (a SIP, a gold plan, fixed deposits, ...). A combined line
   chart shows cumulative contributed (computed automatically) vs. summed current value
   across all vehicles (forward-filled between updates); a dropdown re-points the same
   chart at a single vehicle's own history instead of the combined one. Below it, a
   per-vehicle list shows each one's own contributed/value/gain, an optional
   target-progress bar, and *"updated N days ago"* (amber if stale, >45 days) — but the
   inline "update value" field and per-vehicle chart button only act on whichever
   vehicle you've picked from the dropdown, instead of every vehicle's controls being
   stacked and visible at once (this was the "doesn't look clean" clutter fixed this
   round). "+ Add investment" opens a modal that creates a new `expense_type='saving'`
   category, optionally nested under a parent and with a target amount/date.

8. **Wallet & Accounts** — the three account balances (Wallet/Credit/Liquid) as small
   stat tiles, a "+ Add income" button (logs a Wallet deposit — salary, bonus,
   freelance), a "Settle credit" button (pays down Credit from Wallet), and a recent
   ledger of `account_transactions` (income + settlements, colored green/red for
   in/out). This is the wallet/accounts concept made visible and editable from the
   dashboard, not just via Telegram commands.

9. **Fixed & recurring expenses** — split by cadence: **Monthly fixed** shows only the
   latest logged instance of each distinct (category, note) pair tagged
   `expense_type='fixed', cadence='monthly'` (rent, subscriptions get re-logged every
   month, so this is a snapshot of current fixed costs, not a growing history);
   **Yearly cross-cutting** lists every `expense_type='fixed', cadence='annual'`
   transaction in full (rare enough to stay readable). A combined "≈₹X/month locked
   in" stat = monthly total + yearly total ÷ 12. These still count fully against
   wallet/credit and spend above; this section just keeps fixed/cross-cutting costs
   visible and flagged as non-cuttable (see §9) instead of buried in — or mistaken for
   variable spend within — one month's activity.

10. **Budgets & alerts** — per category with a `monthly_cap`: label + `spent / cap`
    progress bar. Bar is normal color when under, **red when over cap**. Show a
    `WATCH` tag near the cap, `ON TRACK` when comfortably under.

11. **Recent activity** — table: Date · Category (colored dot) · Note · Payment source
    (💰 Wallet / 💳 Credit / 💧 Liquid) · Amount · **✎ edit**. `expense_type='fixed'`
    rows get a "📌 fixed" badge, `one-off` a "🎲 one-off" badge, `saving` a "📈 saving"
    badge — each with its `cadence` alongside if one is set — next to the date.
    Subtitle: "Latest entries logged from Telegram. Tap ✎ to edit." Most recent first.
    The edit button opens a modal (category/note/amount/date/payment source/expense
    type/cadence, all editable) that persists via `PUT /api/transactions/{id}`, plus a
    Delete action via `DELETE /api/transactions/{id}` — this is the one place besides
    `/cat` and `/undo` that can change an already-logged transaction, and unlike those
    two bot commands it works on *any* past row, not just the most recent one.

12. **🏷 Categories panel** (modal) — add/edit/delete categories (color, expense_type,
    cadence, monthly cap) and their keyword aliases, backed by the
    `categories`/`keywords` tables via `/api/categories`. No code editing required to
    add a category. A `saving`-type category additionally gets a "sub-category of"
    parent picker and a "feeds Liquid fund" checkbox (sets `account_link='liquid'` —
    its deposits count toward the Liquid balance instead of being tracked as an
    investment vehicle). Keyword add has both an Enter-key handler and a visible "+
    Add" button — Android software keyboards don't reliably fire a `keydown`/Enter
    event, so the button is the dependable path; adding a keyword patches just that
    category's chip list in place (no full modal re-render) so the input keeps focus
    for adding several keywords in a row.

13. **⚙ Settings panel** (modal) — edit the reference `monthly_salary` and
    `credit_limit` via `/api/settings`. Also settable from Telegram with `/salary` and
    `/credit`. Actual Wallet income is logged separately (§8), not set here.

**⬇ Export** button in the header downloads the full-history Excel workbook (see §11).

Chart.js and Tailwind load from a CDN; if either fails (e.g. flaky wifi), the affected
chart shows a small "Chart library failed to load" message in its place but the rest of
the page — cards, insights, tables, category/settings management — keeps working off
plain JSON, since none of that depends on the chart library being present.

**API endpoints (`server.py`, all accept `?month=YYYY-MM`, default current):**
```
GET  /api/summary                        -> cards (incl. wallet/savings/liquid balances, credit outstanding) + category breakdown + pace + insights inputs; ?view=all|wallet|credit|saving|liquid re-slices the breakdown
GET  /api/insights                       -> list of insight bullet strings + status
GET  /api/recap                          -> cached AI daily recap {lines, source, generated_at}
POST /api/recap/refresh                  -> force-regenerate today's recap (bypasses cache)
GET  /api/daily-burn                     -> [{day, cumulative}], plus reference-line params; ?category= to filter
GET  /api/monthly                        -> last 6 months total outflow; ?category= to filter to one category
GET  /api/savings                        -> last 6 months total for real investment-vehicle (expense_type='saving', no account_link) categories
GET  /api/oneoff                         -> this month's expense_type='one-off' transactions + total (+ how much came from liquid)
GET  /api/accounts                       -> {wallet_balance, credit_outstanding, credit_limit, credit_available, liquid_balance, payday, needs_settlement}
GET  /api/accounts/transactions          -> income/settlement ledger; ?account=wallet|credit to filter
POST /api/accounts/income                -> log a Wallet deposit {amount, note, txn_date}
POST /api/accounts/settle                -> pay down Credit from Wallet {amount, note, txn_date}
GET  /api/investments                    -> per-vehicle {vehicles: [...], combined_months, total_contributed, total_value, total_gain}
POST /api/investments                    -> upsert {category, month, value, note} snapshot for one vehicle
GET  /api/recurring                      -> {monthly, yearly, monthly_total, yearly_total, monthly_equivalent_total}
GET  /api/budgets                        -> [{category, cap, spent, status}]
GET  /api/transactions                   -> recent rows for the activity table
PUT  /api/transactions/{id}              -> edit any field of a logged transaction (category/note/amount/payment_source/expense_type/cadence/spent_on)
DEL  /api/transactions/{id}              -> delete a logged transaction
GET  /api/categories                     -> categories with their keyword lists
POST /api/categories                     -> add a category (expense_type/cadence, optional parent_category/target_amount/target_date/account_link)
PUT  /api/categories/{name}              -> update color/expense_type/cadence/cap/parent_category/target_amount/target_date/account_link
DEL  /api/categories/{name}              -> delete (reassigns its transactions to Other)
POST /api/categories/{name}/keywords     -> add a keyword alias
DEL  /api/categories/{name}/keywords/{k} -> remove a keyword alias
GET  /api/settings                       -> monthly_salary (reference), credit_limit, currency
PUT  /api/settings                       -> update monthly_salary and/or credit_limit
GET  /api/export                         -> full-history multi-sheet Excel workbook (see §11)
```
Bind uvicorn to `0.0.0.0:8000` so the tablet's LAN IP works from the phone.

---

## 9. Insights (`insights.py`)

Compute the **metrics with plain Python** (deterministic, free, no Gemini call here at
all) and phrase them with string templates. All four expense-type splits
(`fixed_total`/`variable_total`/`oneoff_total`/`savings_total`) come straight off each
transaction's own `expense_type` — no join to `categories` needed for this, since the
tag lives on the transaction itself (a category's `expense_type` is only ever a
*default* for new transactions, not authoritative for past ones).

`projected` (and the dashboard's `pace_per_day`) only pace the **variable** portion of
spend across the month — `fixed_total + savings_total + (variable_total ÷ days_elapsed
× days_total)`. Fixed costs and a SIP contribution are lump sums already logged in full
for the month; they don't recur again before month-end, so extrapolating them by
days-elapsed would fabricate a spike (e.g. rent paid in full on day 1 previously
projected the whole month at a ₹21,500/day pace). **`oneoff_total` is excluded
entirely** — a one-time trip isn't a pattern that continues for the rest of the month,
and including it would make an otherwise-ordinary month look like a blowout. Only the
actual day-to-day variable spend is paced forward.

`credit_outstanding` (and the "close to the limit" warning) is **cumulative, not
month-scoped** — `db.get_credit_outstanding(through_month=month)` — since Credit is
revolving debt that carries an unsettled balance across months (§5b); using just this
month's own charges would understate how close to the limit an unsettled prior cycle
actually leaves you. `insights.payday_date()` / `insights.credit_settlement_status()`
(§5b) live here too, shared by `bot.py`'s `/wallet` and `server.py`'s `/api/summary` +
`/api/accounts`.

**Metrics to compute** (mirror the reference insights):
- On-pace check: projected month-end vs (reference monthly income + credit_limit) →
  "On pace for ₹X, under/over your ₹Y salary + credit."
- Credit warning: if credit_outstanding ≥ 80% of credit_limit → "Credit outstanding is
  ₹X of ₹Y — getting close to the limit."
- Payday settlement nag: if `credit_settlement_status().needs_settlement` → "₹X in
  credit is still unsettled past payday (date) — settle it to free up your limit."
- Fixed vs flexible: fixed_total (excl. saving) as % of spend_total → "Fixed costs
  (rent, subscriptions, etc.) are ₹X/month — Y% of spend. ₹Z is actually flexible."
- Category concentration (**variable spend only** — excludes fixed/one-off/saving,
  since you can't meaningfully "cut" rent, a SIP, or an anomaly): top variable category
  % of variable_total; "Cutting it 20% saves ₹X/month (₹Y/year)."
- Largest single expense (**variable only**, same exclusions): "Largest single
  (variable) hit: ₹X on <cat> (<note>)."
- One-off anomalies: if oneoff_total > 0 → "₹X in one-off spend this month — excluded
  from your pace above since these are anomalies, not a monthly pattern. Largest: ₹Y on
  <cat> (<note>)." Adds a nudge to pay from Liquid if any of it wasn't
  (`oneoff_from_liquid < oneoff_total`).
- Savings: sum(expense_type='saving') this month; "Put aside ₹X in savings/investments
  this month."

Return a list of `{text, status}` where status ∈ `good | watch | note`.

### AI daily recap (`ai_insights.py`)

The **only other** Gemini call site besides `parser.classify_with_gemini()`. Deliberately
a separate module so `insights.py` stays pure-Python — this is additive, not a
replacement for the deterministic insights above.

- **Cadence:** capped at roughly once per day. Result is cached in the `ai_recaps` table
  keyed by date; a cache hit costs zero API calls. A cache miss (first check of the day,
  or an explicit refresh) calls Gemini once and caches the result.
- **Trigger:** dashboard "✨ Daily recap" card loads the cached recap on page load; its
  "↻ Refresh" button force-regenerates. Telegram: `/recap` (cached) or `/recap refresh`
  (force). No background scheduler — generation is lazy, triggered by whichever of these
  the user hits first each day.
- **Data sent to Gemini** (`_gather_recap_data`, all pre-computed in Python — Gemini never
  sees raw transactions, only aggregates): today's total + per-category breakdown, last 7
  days' daily totals, this-week vs previous-week total, month-to-date total (incl. and
  excl. savings), fixed/variable/one-off spend split, top category and top *variable*
  category, projected month-end, the reference monthly income, credit limit, cumulative
  credit outstanding, and any categories currently over their cap.
- **Prompt:** ask for 3-5 short plain-text lines calling out only *notable* patterns
  (trends, spikes, pace vs budget, category shifts, streaks) — explicitly told not to
  restate every number or produce headers/markdown/preamble, and explicitly told both
  `fixed_total` (rent, subscriptions, a SIP) and `oneoff_total` (a trip, already excluded
  from the projection) aren't something to suggest "cutting back on" — any such
  observation must be based on `variable_total` only. Not meant to be verbose.
- **Fallback:** if `GEMINI_API_KEY` is unset or the call fails for any reason, silently
  fall back to `insights.build_insights()` (still cached, tagged `source: 'fallback'` so
  the UI can show which one it got) — the recap card and `/recap` command never error out.

---

## 10. Config (`.env.example`)

```
TELEGRAM_BOT_TOKEN=        # from @BotFather
ALLOWED_TELEGRAM_USER_ID=  # your numeric Telegram user id (bot ignores everyone else)
GEMINI_API_KEY=            # optional; leave blank to disable AI (regex-only mode)
MONTHLY_SALARY=60000       # reference monthly income for %-used displays — NOT the
                           # Wallet's real balance; log actual income via /income (§5b)
CREDIT_LIMIT=0
CURRENCY=INR
TIMEZONE=Asia/Kolkata
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8000
```

---

## 11. Excel export (`export.py`)

`GET /api/export` streams a multi-sheet `.xlsx` workbook (openpyxl) — a read-only
reporting artifact for offline analysis, not consulted by the dashboard itself.
Deliberately depends only on `db.py`, never `server.py`, so there's no import cycle with
the route that serves it. Sheets:

- **Transactions** — every transaction ever logged, oldest first, with all current
  fields (category, note, amount, payment source, expense type, cadence, source).
- **Monthly Summary** — one row per month (`db.get_all_months()`, not a fixed lookback)
  with total spend split by account (wallet/credit/liquid) and by expense type
  (fixed/variable/one-off/saving) — the highest-level view of how the picture has
  evolved over time.
- **Category Breakdown** — a wide pivot: one row per month, one column per category
  (plus a Total column), so it can be filtered/charted directly in Excel without
  reshaping first.
- **Accounts Ledger** — every `account_transactions` row (income + settlements),
  chronological.
- **Investments** — long format, one row per (vehicle, month): cumulative contributed
  and the manually-set value as of that month, for every real investment vehicle
  (mirrors the Savings section's per-vehicle history, excludes Liquid-linked categories).

Each sheet gets a bold dark header row, frozen top row, and auto-sized columns
(`_style_header`/`_autosize`/`_write_table` helpers) so it's immediately usable, not a
raw data dump.

---

## 12. Termux run (`run.sh`)

```bash
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock            # keep bot alive when screen is off
python main.py              # starts telegram bot + dashboard together
```

Setup notes to include in a short README:
```
pkg install python
pip install -r requirements.txt
cp .env.example .env        # then fill in tokens
bash run.sh
```

---

## 13. Build in phases (each must be runnable before moving on)

- **Phase 1 — Core loop:** `config.py`, `db.py`, regex-only `parser.py`, `bot.py`,
  `main.py` (bot only). Log a message, see the row + confirmation. No AI, no dashboard yet.
- **Phase 2 — Dashboard:** `server.py` + `index.html`. Cards, donut, recent activity
  reading real data. Wire `main.py` to run bot + server together.
- **Phase 3 — Full charts:** daily burn, month-over-month, budgets & alerts.
- **Phase 4 — AI layer:** Gemini fallback categorisation + `insights.py` + `/insights`.
- **Phase 5 — Polish:** `/undo`, `/cat`, month selector, guessed-category flagging.

---

## 14. Non-goals / future

- **Multi-user, auth, cloud hosting** — out of scope (single personal user).
- **WhatsApp** — future option. Because ingestion is isolated in `bot.py`, swapping to
  WhatsApp (Cloud API webhook, or an unofficial QR library) should touch nothing else.
  Do **not** build it now.
- **Bot-side transaction editing** — full edits (category, note, amount, date, payment
  source, expense type, cadence) happen via the dashboard's Recent Activity ✎ button +
  `PUT /api/transactions/{id}` (§8). `/cat` and `/undo` remain the quick bot-side
  corrections for the *last* transaction only; there's no bot command to edit an
  arbitrary past row.
- **Combined insights/recap view + a chatbot window** — deliberately **not yet built**.
  The plan is to merge §3's deterministic insights and §3b's AI daily recap into one
  view spanning daily/monthly/overall trends, plus a small Gemini-backed chat window
  that can answer free-form questions over the full ledger (via the same aggregates
  `ai_insights.py` already computes, or the Excel export). Explicitly sequenced *after*
  every other UI/data-model piece in this file is settled, so it isn't built on moving
  ground — don't start it until asked.
