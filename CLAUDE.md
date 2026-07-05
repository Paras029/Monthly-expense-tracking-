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
  recurrence     TEXT DEFAULT 'one-off'  -- 'one-off' | 'monthly' | 'yearly' (fixed/cross-cutting)
);

CREATE TABLE IF NOT EXISTS categories (
  name         TEXT PRIMARY KEY,
  color        TEXT NOT NULL,         -- hex, used by charts
  kind         TEXT NOT NULL,         -- 'essential' | 'discretionary' | 'saving' | 'liquid'
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
  category   TEXT NOT NULL,  -- a kind='saving' category name — each is its own "vehicle"
  month      TEXT NOT NULL,  -- 'YYYY-MM'
  value      REAL NOT NULL,  -- total value of this vehicle as of this month (manually entered)
  note       TEXT,
  updated_at TEXT,           -- when last set, for "updated N days ago"
  PRIMARY KEY (category, month)
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

`kind = 'saving'` (a long-term investment vehicle — SIP, a gold plan, fixed deposits)
and `kind = 'liquid'` (an emergency/liquid cash fund) categories still count fully
against salary/credit like any other expense — they're money that actually left your
account — but are **excluded** from the "where the money goes" donut, the Top category
card, and the "largest single hit"/"cutting X% saves Y" insights (`NON_SPEND_KINDS` in
`insights.py`), since those are about discretionary/essential spending habits, not SIP
contributions or cash you deliberately set aside — neither is a "hit" you'd cut.
`compute_metrics()` exposes both `total` (all spend, including saving/liquid — used for
salary/credit tracking) and `spend_total` (excludes both — used for the
donut/top-category/largest-hit). Each `kind='saving'` category also gets its own
cumulative all-time balance card and its own row in Long-term investments; each
`kind='liquid'` category rolls up into the single "Liquid fund" balance card at the top
of the dashboard (§8).

`investment_snapshots` powers **long-term investment tracking, per vehicle**: since
there's no way to pull real brokerage/mutual-fund values, the user manually logs each
vehicle's total value once in a while (via `/portfolio <vehicle> <amount>` or the
dashboard's per-vehicle "Update value" field) — every `kind='saving'` category is
tracked independently (its own cumulative contribution, its own value history, its own
"updated N days ago"), since a user may run several vehicles (a SIP, a gold scheme, FDs)
that shouldn't be conflated into one blended number. The dashboard also shows a combined
trend: cumulative contributed vs. summed value across vehicles, forward-filling each
vehicle's last known value between updates (`GET /api/investments`) — the gap is gain
or loss.

Keyword→category aliases are seeded from `config.CATEGORY_KEYWORDS` into the `keywords`
table on first run, then live entirely in the DB from that point on — editable from the
dashboard's 🏷 Categories panel (add/remove keywords, add/edit/delete categories) without
touching code. `config.py` is only the seed data for a fresh database.

`db.init_db()` seeding uses `INSERT OR IGNORE`, which never touches a category that
already exists — so if a category's seed `kind` changes after some databases are
already running (as happened when Investments moved essential → saving), those existing
installs are stuck on the old value forever unless explicitly fixed. `_migrate_investments_kind()`
does that one-time fix, guarded by a `migrated_investments_kind` settings flag so it
never fights a user's own later edit back to something else via the Categories panel.
Any future re-seed of a default category's `kind` needs the same treatment.

**`recurrence`** ('one-off' default | 'monthly' | 'yearly') flags a transaction as a
fixed, locked-in cost rather than a variable purchase — orthogonal to `kind`. 'yearly'
is an annual cross-cutting cost (an annual gym membership); 'monthly' is a cost that
recurs every month at roughly the same amount (rent, a SIP contribution, a Netflix
subscription) re-logged each time it's paid. Both still count fully against
salary/credit and `spend_total` like any transaction, but `compute_metrics()` also
excludes `recurrence != 'one-off'` transactions when computing `top_variable_category`
and `largest` (the "cutting X% saves Y" and "largest single hit" insights) — you can't
meaningfully "cut" rent the way you can a discretionary purchase, so those insights are
based on `variable_total` (spend minus fixed costs), not `spend_total`.

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
rent 21500 fixed                 -> recurrence=monthly (fixed cost, re-logged every month)
gym membership 12000 yearly      -> recurrence=yearly (annual cross-cutting)
```

**Algorithm (regex-first):**
1. Extract **amount**: first number in the message (supports `1500`, `1,500`, `₹1500`).
2. Extract **category**: scan tokens against the keyword-alias dict (case-insensitive,
   loaded from the `keywords` table). If an explicit category name is present
   (`... travel`), that wins.
3. Extract **payment_source**: trailing `credit`/`card`/`cc` → `credit`; `salary`/`cash`
   or nothing mentioned → `salary` (the default).
4. Extract **recurrence**: trailing `yearly`/`annual`/`annually` → `yearly`; trailing
   `recurring`/`fixed`/`subscription` → `monthly` (a fixed cost re-logged every month —
   rent, a SIP, a subscription); otherwise `one-off` (the default, an ordinary variable
   purchase).
5. **note** = message with the amount, an explicit category name, and any
   payment/recurrence keyword stripped. A keyword-matched category word (e.g. "gym")
   is *kept* in the note.
6. `spent_on` = today unless the message contains a parseable date (keep simple: today only for v1).
7. If **no category** confidently found → **Gemini fallback** (see below).
8. If **no amount** found → reply asking user to include a number; do not write a row.

**Gemini fallback (only when step 7 triggers, and only if `GEMINI_API_KEY` set):**
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
  `✅ ₹1,500 · Luxuries · gym · 💰 Salary  (id 42)`
  Credit-tagged expenses show `💳 Credit` instead; `recurrence=monthly` appends
  `· 📌 Fixed`, `recurrence=yearly` appends `· 🔁 Yearly`.
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

1. **Four summary cards** — these are about *where money is sitting*, not spending
   patterns (which move to §2 instead):
   - *Salary* — used + `X% of ₹SALARY used` + thin progress bar.
   - *Credit* — used + `X% of ₹CREDIT_LIMIT used` + thin progress bar (red when over).
   - *Savings* — cumulative all-time balance across all `kind='saving'` categories +
     `+₹X this month`.
   - *Liquid fund* — cumulative all-time balance across all `kind='liquid'` categories +
     `+₹X this month` — a deliberately-set-aside emergency/liquid cash reserve, tracked
     separately from long-term investments since it's meant to stay accessible, not grow.

2. **Where the money goes** — donut chart (Chart.js), total in center, legend list of
   categories with amount + %, plus *Top category* and *Projected month-end* as inline
   stats below the legend (moved here from the old top-card row — they're about
   spending patterns, which this section already covers).

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

6. **Long-term investments** — each `kind='saving'` category is an independent
   "vehicle" (a SIP, a gold plan, fixed deposits, ...). A combined line chart shows
   cumulative contributed (computed automatically) vs. summed current value across all
   vehicles (forward-filled between updates — a vehicle not updated this month keeps
   its last known value rather than dropping to zero); below it, a per-vehicle list
   shows each one's own contributed/value/gain and *"updated N days ago"* (amber if
   stale, >45 days), with an inline field to update that vehicle's value. "+ Add
   investment" opens a lightweight modal that just creates a new `kind='saving'`
   category (equivalent to doing it from 🏷 Categories, but one tap closer since it's a
   very expected action from this section).

7. **Fixed & recurring expenses** — split by cadence: **Monthly fixed** shows only the
   latest logged instance of each distinct (category, note) pair tagged
   `recurrence='monthly'` (rent, subscriptions get re-logged every month, so this is a
   snapshot of current fixed costs, not a growing history); **Yearly cross-cutting**
   lists every `recurrence='yearly'` transaction in full (rare enough to stay
   readable). A combined "≈₹X/month locked in" stat = monthly total + yearly total ÷ 12.
   These still count fully against salary/credit and spend above; this section just
   keeps fixed/cross-cutting costs visible and flagged as non-cuttable (see §9) instead
   of buried in — or mistaken for variable spend within — one month's activity.

8. **Budgets & alerts** — per category with a `monthly_cap`: label + `spent / cap`
   progress bar. Bar is normal color when under, **red when over cap**. Show a
   `WATCH` tag near the cap, `ON TRACK` when comfortably under.

9. **Recent activity** — table: Date · Category (colored dot) · Note · Payment source
   (💰 Salary / 💳 Credit) · Amount · **✎ edit**. `recurrence='monthly'` rows get a small
   "fixed" badge, `recurrence='yearly'` rows get a "yearly" badge, next to the date.
   Subtitle: "Latest entries logged from Telegram. Tap ✎ to edit." Most recent first.
   The edit button opens a modal (category/note/amount/date/payment
   source/recurrence, all editable) that persists via `PUT /api/transactions/{id}`, plus
   a Delete action via `DELETE /api/transactions/{id}` — this is the one place besides
   `/cat` and `/undo` that can change an already-logged transaction, and unlike those two
   bot commands it works on *any* past row, not just the most recent one.

10. **🏷 Categories panel** (modal) — add/edit/delete categories (color, kind, monthly
    cap) and their keyword aliases, backed by the `categories`/`keywords` tables via
    `/api/categories`. No code editing required to add a category. Kind is one of
    `essential`/`discretionary`/`saving`/`liquid`. Keyword add has both an Enter-key
    handler and a visible "+ Add" button — Android software keyboards don't reliably
    fire a `keydown`/Enter event, so the button is the dependable path; adding a keyword
    patches just that category's chip list in place (no full modal re-render) so the
    input keeps focus for adding several keywords in a row.

11. **⚙ Settings panel** (modal) — edit `monthly_salary` and `credit_limit` via
    `/api/settings`. Also settable from Telegram with `/salary` and `/credit`.

Chart.js and Tailwind load from a CDN; if either fails (e.g. flaky wifi), the affected
chart shows a small "Chart library failed to load" message in its place but the rest of
the page — cards, insights, tables, category/settings management — keeps working off
plain JSON, since none of that depends on the chart library being present.

**API endpoints (`server.py`, all accept `?month=YYYY-MM`, default current):**
```
GET  /api/summary                        -> cards (incl. savings/liquid balances) + category breakdown (excl. saving/liquid) + pace + insights inputs
GET  /api/insights                       -> list of insight bullet strings + status
GET  /api/recap                          -> cached AI daily recap {lines, source, generated_at}
POST /api/recap/refresh                  -> force-regenerate today's recap (bypasses cache)
GET  /api/daily-burn                     -> [{day, cumulative}], plus reference-line params; ?category= to filter
GET  /api/monthly                        -> last 6 months total outflow; ?category= to filter to one category
GET  /api/savings                        -> last 6 months total for kind='saving' categories
GET  /api/investments                    -> per-vehicle {vehicles: [...], combined_months, total_contributed, total_value, total_gain}
POST /api/investments                    -> upsert {category, month, value, note} snapshot for one vehicle
GET  /api/recurring                      -> {monthly, yearly, monthly_total, yearly_total, monthly_equivalent_total}
GET  /api/budgets                        -> [{category, cap, spent, status}]
GET  /api/transactions                   -> recent rows for the activity table
PUT  /api/transactions/{id}              -> edit any field of a logged transaction
DEL  /api/transactions/{id}              -> delete a logged transaction
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
(excludes `NON_SPEND_KINDS = {'saving', 'liquid'}`), not `total` — otherwise a big SIP
payment or a liquid-fund transfer inflates the base and understates the real ratio.

**Metrics to compute** (mirror the reference insights):
- On-pace check: projected month-end vs (salary + credit_limit) → "On pace for ₹X,
  under/over your ₹Y salary + credit."
- Credit warning: if credit_used ≥ 80% of credit_limit → "Credit usage at ₹X of ₹Y —
  getting close to the limit."
- Fixed vs flexible: fixed_total (`recurrence != 'one-off'`, excl. savings) as % of
  spend_total → "Fixed costs (rent, subscriptions, etc.) are ₹X/month — Y% of spend.
  ₹Z is actually flexible."
- Category concentration (**variable spend only** — excludes both savings and
  `recurrence != 'one-off'`, since you can't meaningfully "cut" rent or a SIP): top
  variable category % of variable_total; "Cutting it 20% saves ₹X/month (₹Y/year)."
- Largest single expense (**variable only**, same exclusions): "Largest single
  (variable) hit: ₹X on <cat> (<note>)."
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
  excl. savings), fixed vs variable spend split, top category and top *variable* category,
  discretionary %, projected month-end, monthly salary, credit limit, and any categories
  currently over their cap.
- **Prompt:** ask for 3-5 short plain-text lines calling out only *notable* patterns
  (trends, spikes, pace vs budget, category shifts, streaks) — explicitly told not to
  restate every number or produce headers/markdown/preamble, and explicitly told
  `fixed_total` (rent, subscriptions, a SIP) isn't something the user chose this month
  and can't be meaningfully cut, so any "cut back on X" observation must be based on
  `variable_total`, never fixed costs. Not meant to be verbose.
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
- **Bot-side transaction editing** — full edits (category, note, amount, date, payment
  source, recurrence) happen via the dashboard's Recent Activity ✎ button + `PUT
  /api/transactions/{id}` (§8). `/cat` and `/undo` remain the quick bot-side corrections
  for the *last* transaction only; there's no bot command to edit an arbitrary past row.
