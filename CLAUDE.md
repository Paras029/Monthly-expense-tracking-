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
- **AI (optional):** Google Gemini free tier via the `google-genai` SDK
- **Frontend:** single `index.html` — **Tailwind (CDN)** + **Chart.js (CDN)**, dark theme
- **Process:** one entrypoint (`main.py`) runs bot **and** server together via asyncio

> ⚠️ **Verify at build time:** the Gemini SDK name/model IDs and the `python-telegram-bot`
> API surface change over time. Before coding those parts, check the current
> `google-genai` usage and current free Gemini model name, and the current PTB v21+
> async API. Don't rely on memorised snippets.

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
├── insights.py          # compute metrics + phrase insight bullets
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
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,          -- ISO datetime the row was logged
  spent_on    TEXT NOT NULL,          -- date of the expense (YYYY-MM-DD), default today
  category    TEXT NOT NULL,          -- must match a categories.name
  note        TEXT,                   -- e.g. "gym", "Zomato lunch"
  amount      REAL NOT NULL,          -- in rupees
  raw_message TEXT,                   -- original message, for debugging/undo
  source      TEXT DEFAULT 'telegram'
);

CREATE TABLE IF NOT EXISTS categories (
  name         TEXT PRIMARY KEY,
  color        TEXT NOT NULL,         -- hex, used by charts
  kind         TEXT NOT NULL,         -- 'essential' | 'discretionary'
  monthly_cap  REAL                   -- budget for Budgets & Alerts (nullable)
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);
-- seed: monthly_budget=60000, currency='INR', timezone='Asia/Kolkata'
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
| Investments | `#eab308` | essential     | sip, index fund, stocks, mutual fund |
| Other       | `#64748b` | discretionary | (fallback bucket)                    |

Keyword→category aliases live in `config.py` as a dict and drive regex categorisation.

---

## 6. Message parsing (`parser.py`)

**Input examples the bot must handle:**
```
gym 1500
Zomato lunch 300
oyo 1500 travel
SIP index fund 5000
netflix 649
```

**Algorithm (regex-first):**
1. Extract **amount**: first number in the message (supports `1500`, `1,500`, `₹1500`).
2. Extract **category**: scan tokens against the keyword-alias dict (case-insensitive).
   If an explicit category name is present (`... travel`), that wins.
3. **note** = message with the amount (and trailing explicit category word) stripped.
4. `spent_on` = today unless the message contains a parseable date (keep simple: today only for v1).
5. If **no category** confidently found → **Gemini fallback** (see below).
6. If **no amount** found → reply asking user to include a number; do not write a row.

**Gemini fallback (only when step 5 triggers, and only if `GEMINI_API_KEY` set):**
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
  `✅ ₹1,500 · Luxuries · gym  (id 42)`
  If Gemini/`Other` fallback was used, append `⚠️ guessed category — reply /cat Food to fix`.

**Commands:**
| command            | action                                                        |
|--------------------|---------------------------------------------------------------|
| `/start`, `/help`  | short usage guide with examples                               |
| `/undo`            | delete the last transaction, confirm what was removed         |
| `/cat <Category>`  | change the category of the last transaction                   |
| `/today`           | quick text summary of today's spend                           |
| `/month`           | quick text summary of this month vs budget                    |
| `/insights`        | run + send the monthly insight bullets (see §9)               |

---

## 8. Dashboard (`static/index.html` + `server.py` API)

Dark theme, matching the reference screenshots. Single HTML file, Tailwind + Chart.js
via CDN, vanilla JS `fetch` to the API. A month selector (default = current month)
re-fetches all sections. Header: **"Personal Cashflow Ledger — where every rupee went."**

**Sections (top to bottom):**

1. **Four summary cards**
   - *Spent this month* — total + `X% of ₹BUDGET used` + thin progress bar.
   - *Budget left* — `budget − spent` + `₹/day for N days left`.
   - *Top category* — name + amount + `% of spend`.
   - *Projected month-end* — `spent / days_elapsed × days_in_month`, with `₹/day pace`.

2. **Where the money goes** — donut chart (Chart.js), total in center, legend list of
   categories with amount + %. Colors from `categories.color`.

3. **Spending insights** — bullet list from `insights.py` (see §9). Small status icon
   per bullet (✓ good / ⚠ watch / → note).

4. **Daily burn** — line chart: cumulative spend per day vs a straight dashed
   even-pace budget line (`budget/days_in_month × day`).

5. **Month over month** — bar chart: total outflow for the last 6 months; current
   month highlighted.

6. **Budgets & alerts** — per category with a `monthly_cap`: label + `spent / cap`
   progress bar. Bar is normal color when under, **red when over cap**. Show a
   `WATCH` tag near the cap, `ON TRACK` when comfortably under.

7. **Recent activity** — table: Date · Category (colored dot) · Note · Amount.
   Subtitle: "Latest entries logged from Telegram." Most recent first.

**API endpoints (`server.py`, all accept `?month=YYYY-MM`, default current):**
```
GET /api/summary      -> cards + category breakdown + pace + insights inputs
GET /api/insights     -> list of insight bullet strings + status
GET /api/daily-burn   -> [{day, cumulative}], plus budget line params
GET /api/monthly      -> last 6 months total outflow
GET /api/budgets      -> [{category, cap, spent, status}]
GET /api/transactions -> recent rows for the activity table
```
Bind uvicorn to `0.0.0.0:8000` so the tablet's LAN IP works from the phone.

---

## 9. Insights (`insights.py`)

Compute the **metrics with plain Python** (deterministic, free), then optionally use
Gemini only to phrase them into natural sentences. If no API key, use string templates.

**Metrics to compute** (mirror the reference insights):
- On-pace check: projected month-end vs budget → "On pace for ₹X, under/over your ₹BUDGET budget."
- Category concentration: top category %; "Cutting it 20% saves ₹X/month (₹Y/year)."
- Largest single expense: "Largest single hit: ₹X on <cat> (<note>)."
- Discretionary ratio: sum(discretionary)/total; "Discretionary held at N%."

Return a list of `{text, status}` where status ∈ `good | watch | note`.

---

## 10. Config (`.env.example`)

```
TELEGRAM_BOT_TOKEN=        # from @BotFather
ALLOWED_TELEGRAM_USER_ID=  # your numeric Telegram user id (bot ignores everyone else)
GEMINI_API_KEY=            # optional; leave blank to disable AI (regex-only mode)
MONTHLY_BUDGET=60000
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
