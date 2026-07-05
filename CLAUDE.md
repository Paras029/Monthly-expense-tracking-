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
  payment_source TEXT DEFAULT 'salary',  -- 'salary' | 'credit'
  period         TEXT DEFAULT 'monthly'  -- 'monthly' | 'yearly' (cross-cutting/recurring)
);

CREATE TABLE IF NOT EXISTS categories (
  name         TEXT PRIMARY KEY,
  color        TEXT NOT NULL,         -- hex, used by charts
  kind         TEXT NOT NULL,         -- 'essential' | 'discretionary' | 'saving'
  monthly_cap  REAL                   -- budget for Budgets & Alerts (nullable)
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

CREATE TABLE IF NOT EXISTS investment_snapshots (
  month TEXT PRIMARY KEY,   -- 'YYYY-MM'
  value REAL NOT NULL,      -- total portfolio value as of this month (manually entered)
  note  TEXT
);
```

**Default categories** (seed on first run; colors mirror the reference UI):

| name        | color     | kind          | example keywords                     |
|-------------|-----------|---------------|--------------------------------------|
| Food        | `#f59e0b` | discretionary | zomato, swiggy, lunch, dinner, cafe  |
| Groceries   | `#22c55e` | essential     | dmart, bigbasket, blinkit, grocery   |
| Bills       | `#3b82f6` | essential     | electricity, rent, wifi, recharge    |
| Luxuries    | `#a855f7` | discretionary | gym, netflix, spotify, shopping      |
| Health      | `#ef4444` | essential     | pharmeasy, medicine, doctor, apollo  |
| Travel      | `#06b6d4` | discretionary | ola, uber, metro, flight, fuel       |
| Investments | `#eab308` | saving        | sip, index fund, stocks, mutual fund |
| Other       | `#64748b` | discretionary | (fallback bucket)                    |

`kind = 'saving'` categories still count fully against salary/credit like any other
expense — they're money that actually left your account — but are *also* summed into
the dedicated Savings & Investments chart on the dashboard, so contributions don't get
lost among regular spending categories. They're **excluded** from the "where the money
goes" donut, the Top category card, and the "largest single hit" insight, since those
are about discretionary/essential spending habits, not SIP contributions — a big
Investments transaction isn't a "hit" you'd want to cut. `compute_metrics()` exposes
both `total` (all spend, including savings — used for salary/credit tracking) and
`spend_total` (excludes `kind='saving'` — used for the donut/top-category/largest-hit).

`investment_snapshots` powers **long-term investment tracking**: since there's no way
to pull real brokerage/mutual-fund values, the user manually logs their total portfolio
value once a month (via `/portfolio <amount>` or the dashboard). The dashboard then
charts that against the automatically-computed cumulative sum of `kind='saving'`
transactions ("contributed") — the gap between the two lines is gain or loss.

Keyword→category aliases are seeded from `config.CATEGORY_KEYWORDS` into the `keywords`
table on first run, then live entirely in the DB from that point on — editable from the
dashboard's 🏷 Categories panel (add/remove keywords, add/edit/delete categories) without
touching code. `config.py` is only the seed data for a fresh database.

---

## 6. Message parsing (`parser.py`)

**Input examples the bot must handle:**
```
gym 1500
Zomato lunch 300
oyo 1500 travel
SIP index fund 5000
netflix 649
electricity bill 2200 credit     -> payment_source=credit
gym membership 12000 yearly      -> period=yearly (cross-cutting/recurring)
```

**Algorithm (regex-first):**
1. Extract **amount**: first number in the message (supports `1500`, `1,500`, `₹1500`).
2. Extract **category**: scan tokens against the keyword-alias dict (case-insensitive,
   loaded from the `keywords` table). If an explicit category name is present
   (`... travel`), that wins.
3. Extract **payment_source**: trailing `credit`/`card`/`cc` → `credit`; `salary`/`cash`
   or nothing mentioned → `salary` (the default).
4. Extract **period**: trailing `yearly`/`annual`/`annually`/`recurring` → `yearly`;
   otherwise `monthly` (the default).
5. **note** = message with the amount, an explicit category name, and any payment/period
   keyword stripped. A keyword-matched category word (e.g. "gym") is *kept* in the note.
6. `spent_on` = today unless the message contains a parseable date (keep simple: today only for v1).
7. If **no category** confidently found → **Gemini fallback** (see below).
8. If **no amount** found → reply asking user to include a number; do not write a row.

**Gemini fallback (only when step 7 triggers, and only if `GEMINI_API_KEY` set):**
- Prompt: given the note text and the fixed list of category names, return exactly one
  category name as plain text. Low temperature. Cache identical notes in-memory.
- If no API key or the call fails → assign `Other` and flag the row (still log it).

Keep Gemini usage minimal and wrapped in try/except so the bot never crashes on API issues.

---

## 7. Telegram bot (`bot.py`)

**Security:** read `ALLOWED_TELEGRAM_USER_ID` from env. Ignore messages from any other
user id (the bot is public once created; this keeps strangers from injecting data).

**Behaviour:**
- Any normal text → parse → insert row → reply confirmation:
  `✅ ₹1,500 · Luxuries · gym · 💰 Salary  (id 42)`
  Credit-tagged expenses show `💳 Credit` instead; recurring ones append `· 🔁 Yearly`.
  If Gemini/`Other` fallback was used, append `⚠️ guessed category — reply /cat Food to fix`.

**Commands:**
| command             | action                                                        |
|---------------------|---------------------------------------------------------------|
| `/start`, `/help`   | short usage guide with examples                               |
| `/undo`             | delete the last transaction, confirm what was removed         |
| `/cat <Category>`   | change the category of the last transaction                   |
| `/today`            | quick text summary of today's spend                           |
| `/month`            | quick text summary of this month vs salary & credit           |
| `/insights`         | run + send the monthly insight bullets (see §9)               |
| `/salary <amount>`  | set monthly salary (no arg = show current value)               |
| `/credit <amount>`  | set credit limit (no arg = show current value)                 |
| `/portfolio <amount>` | log this month's total investment/portfolio value (no arg = show current value) |
| `/recap` (or `/recap refresh`) | AI day-by-day + cumulative analysis (see §9); cached once/day, `refresh` forces a new one |

---

## 8. Dashboard (`static/index.html` + `server.py` API)

Dark theme, matching the reference screenshots. Single HTML file, Tailwind + Chart.js
via CDN, vanilla JS `fetch` to the API. A month selector (default = current month)
re-fetches all sections. Header: **"Personal Cashflow Ledger — where every rupee went."**

**Sections (top to bottom):**

1. **Four summary cards**
   - *Salary* — used + `X% of ₹SALARY used` + thin progress bar.
   - *Credit* — used + `X% of ₹CREDIT_LIMIT used` + thin progress bar (red when over).
   - *Top category* — name + amount + `% of spend`.
   - *Projected month-end* — `spent / days_elapsed × days_in_month`, with `₹/day pace`.

2. **Where the money goes** — donut chart (Chart.js), total in center, legend list of
   categories with amount + %. Colors from `categories.color`.

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
   instead of salary+credit. The y-axis is scaled off the pace-*so-far*
   (`budget_per_day × days_elapsed`), not the full month's target — otherwise the
   reference line (which ends at the full monthly total) dwarfs the real spend line
   early in the month and it looks empty.

5. **Month over month + Savings & investments** — two bar charts side by side: total
   outflow for the last 6 months (current month highlighted, filterable by the same
   category dropdown as §4), and the same for `kind='saving'` categories only, plus a
   "₹X saved this month" stat.

6. **Long-term investments** — line chart: cumulative `kind='saving'` contributions
   (computed automatically) vs a manually-entered portfolio value per month (there's
   no way to pull real brokerage/fund data, so the user logs it once a month via
   `/portfolio <amount>` or a dashboard input). The gap between the two lines is the
   gain or loss, shown as a stat (`₹X` and `%`) above the chart.

7. **Recurring & annual expenses** — list of `period='yearly'` transactions with an
   annualised total. These still count fully against salary/credit above; this section
   just keeps cross-cutting costs (e.g. an annual gym membership) visible instead of
   buried in one month's activity.

8. **Budgets & alerts** — per category with a `monthly_cap`: label + `spent / cap`
   progress bar. Bar is normal color when under, **red when over cap**. Show a
   `WATCH` tag near the cap, `ON TRACK` when comfortably under.

9. **Recent activity** — table: Date · Category (colored dot) · Note · Payment source
   (💰 Salary / 💳 Credit) · Amount. Yearly-tagged rows get a small "yearly" badge next
   to the date. Subtitle: "Latest entries logged from Telegram." Most recent first.

10. **🏷 Categories panel** (modal) — add/edit/delete categories (color, kind, monthly
    cap) and their keyword aliases, backed by the `categories`/`keywords` tables via
    `/api/categories`. No code editing required to add a category.

11. **⚙ Settings panel** (modal) — edit `monthly_salary` and `credit_limit` via
    `/api/settings`. Also settable from Telegram with `/salary` and `/credit`.

Chart.js and Tailwind load from a CDN; if either fails (e.g. flaky wifi), the affected
chart shows a small "Chart library failed to load" message in its place but the rest of
the page — cards, insights, tables, category/settings management — keeps working off
plain JSON, since none of that depends on the chart library being present.

**API endpoints (`server.py`, all accept `?month=YYYY-MM`, default current):**
```
GET  /api/summary                        -> cards + category breakdown (excl. savings) + pace + insights inputs
GET  /api/insights                       -> list of insight bullet strings + status
GET  /api/recap                          -> cached AI daily recap {lines, source, generated_at}
POST /api/recap/refresh                  -> force-regenerate today's recap (bypasses cache)
GET  /api/daily-burn                     -> [{day, cumulative}], plus reference-line params; ?category= to filter
GET  /api/monthly                        -> last 6 months total outflow; ?category= to filter to one category
GET  /api/savings                        -> last 6 months total for kind='saving' categories
GET  /api/investments                    -> last N months' cumulative contributed vs manually-entered value + gain/loss
POST /api/investments                    -> upsert {month, value, note} portfolio snapshot
GET  /api/recurring                      -> period='yearly' transactions + annual total
GET  /api/budgets                        -> [{category, cap, spent, status}]
GET  /api/transactions                   -> recent rows for the activity table
GET  /api/categories                     -> categories with their keyword lists
POST /api/categories                     -> add a category
PUT  /api/categories/{name}              -> update color/kind/cap
DEL  /api/categories/{name}              -> delete (reassigns its transactions to Other)
POST /api/categories/{name}/keywords     -> add a keyword alias
DEL  /api/categories/{name}/keywords/{k} -> remove a keyword alias
GET  /api/settings                       -> monthly_salary, credit_limit, currency
PUT  /api/settings                       -> update monthly_salary and/or credit_limit
```
Bind uvicorn to `0.0.0.0:8000` so the tablet's LAN IP works from the phone.

---

## 9. Insights (`insights.py`)

Compute the **metrics with plain Python** (deterministic, free, no Gemini call here at
all) and phrase them with string templates. `discretionary_pct` divides by `spend_total`
(excludes `kind='saving'`), not `total` — otherwise a big SIP payment inflates the base
and understates the real ratio.

**Metrics to compute** (mirror the reference insights):
- On-pace check: projected month-end vs (salary + credit_limit) → "On pace for ₹X,
  under/over your ₹Y salary + credit."
- Credit warning: if credit_used ≥ 80% of credit_limit → "Credit usage at ₹X of ₹Y —
  getting close to the limit."
- Category concentration: top category % of spend_total; "Cutting it 20% saves
  ₹X/month (₹Y/year)."
- Largest single expense (excl. savings): "Largest single hit: ₹X on <cat> (<note>)."
- Discretionary ratio: sum(discretionary)/spend_total; "Discretionary held at N% of spend."
- Savings: sum(kind='saving') this month; "Put aside ₹X in savings/investments this month."

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
  excl. savings), top category, discretionary %, projected month-end, monthly salary,
  credit limit, and any categories currently over their cap.
- **Prompt:** ask for 3-5 short plain-text lines calling out only *notable* patterns
  (trends, spikes, pace vs budget, category shifts, streaks) — explicitly told not to
  restate every number or produce headers/markdown/preamble. Not meant to be verbose.
- **Fallback:** if `GEMINI_API_KEY` is unset or the call fails for any reason, silently
  fall back to `insights.build_insights()` (still cached, tagged `source: 'fallback'` so
  the UI can show which one it got) — the recap card and `/recap` command never error out.

---

## 10. Config (`.env.example`)

```
TELEGRAM_BOT_TOKEN=        # from @BotFather
ALLOWED_TELEGRAM_USER_ID=  # your numeric Telegram user id (bot ignores everyone else)
GEMINI_API_KEY=            # optional; leave blank to disable AI (regex-only mode)
MONTHLY_SALARY=60000
CREDIT_LIMIT=0
CURRENCY=INR
TIMEZONE=Asia/Kolkata
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8000
```

---

## 11. Termux run (`run.sh`)

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

## 12. Build in phases (each must be runnable before moving on)

- **Phase 1 — Core loop:** `config.py`, `db.py`, regex-only `parser.py`, `bot.py`,
  `main.py` (bot only). Log a message, see the row + confirmation. No AI, no dashboard yet.
- **Phase 2 — Dashboard:** `server.py` + `index.html`. Cards, donut, recent activity
  reading real data. Wire `main.py` to run bot + server together.
- **Phase 3 — Full charts:** daily burn, month-over-month, budgets & alerts.
- **Phase 4 — AI layer:** Gemini fallback categorisation + `insights.py` + `/insights`.
- **Phase 5 — Polish:** `/undo`, `/cat`, month selector, guessed-category flagging.

---

## 13. Non-goals / future

- **Multi-user, auth, cloud hosting** — out of scope (single personal user).
- **WhatsApp** — future option. Because ingestion is isolated in `bot.py`, swapping to
  WhatsApp (Cloud API webhook, or an unofficial QR library) should touch nothing else.
  Do **not** build it now.
- **Editing arbitrary past transactions in the UI** — v1 edits happen via bot commands.
