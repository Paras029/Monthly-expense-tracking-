# Personal Cashflow Ledger

A self-hosted, single-user expense tracker. Log expenses by chatting with a
Telegram bot; view spending analytics on a local web dashboard. Runs entirely
in Termux on an Android tablet — no cloud accounts, no auth, no PC.

See `CLAUDE.md` for the full design spec.

## Setup (Termux)

```bash
pkg install python
pip install -r requirements.txt
cp .env.example .env        # then fill in TELEGRAM_BOT_TOKEN, ALLOWED_TELEGRAM_USER_ID, etc.
bash run.sh
```

`run.sh` calls `termux-wake-lock` and then starts `main.py`, which runs the
Telegram bot (long-polling) and the FastAPI dashboard together.

## Usage

- **Log an expense:** message the bot on Telegram, e.g. `gym 1500`,
  `Zomato lunch 300`, `oyo 1500 travel`. Every expense gets two tags: a
  **type** (`fixed`/`variable`/`one-off`/`saving`) and a **cadence**
  (`daily`/`weekly`/`monthly`/`annual`) — both default to the category's own
  settings (🏷 Categories), so routine logging never needs a tag.
- **Pay by credit card or from your Liquid fund:** add `credit` (or
  `card`/`cc`) or `liquid`, e.g. `electricity bill 2200 credit`. Default
  payment source is your Wallet.
- **Tag a fixed monthly cost:** add `fixed` (or `recurring`/`subscription`), e.g.
  `rent 21500 fixed`. It still counts against your Wallet/Credit, but
  insights won't suggest "cutting" it the way they would a variable purchase.
- **Tag an annual cross-cutting cost:** add `yearly` (or `annual`/`annually`), e.g.
  `gym membership 12000 yearly`. Same idea, for costs paid once a year — both
  show up in the dashboard's "Fixed & recurring expenses" section instead of
  skewing one month's numbers.
- **Tag a one-off anomaly:** add `oneoff` (or `one-off`), e.g.
  `flight to goa 15000 oneoff liquid`. Still counted against whichever
  account paid for it, but excluded from the projected month-end pace since
  it's not a recurring pattern — ideally paid from your Liquid fund.
- **Add income to your Wallet:** `/income 60000 July salary` on the bot, or
  "+ Add income" on the dashboard's Wallet & Accounts section. Your Wallet
  balance is the real running total — salary/bonus/freelance in, expenses out.
- **Move money into your Liquid fund:** `/liquid 5000` on the bot, or "💧 Add
  to Liquid" on the dashboard — subtracts from your Wallet, adds to Liquid.
- **Settle your credit card:** `/settle 5000` on the bot, or "Settle credit"
  on the dashboard — pays down Credit from your Wallet, freeing up your
  credit limit. Credit is revolving: an unsettled balance carries into next
  month rather than resetting, and the dashboard nags you with a banner once
  your payday (the 25th, or the last working day before it) has passed with
  anything still outstanding.
- **"How much have I spent" is Wallet-only:** the donut, top category, and
  projected month-end all reflect Wallet spend specifically — Credit and
  Liquid are separate accounts with their own outstanding/limit and balance,
  tracked in their own cards instead of inflating your day-to-day spend view.
- **Set your reference income/credit limit:** `/salary 60000`, `/credit 20000` on the
  bot, or via the ⚙ Settings panel on the dashboard. `monthly_salary` is just
  a target for %-used displays — your Wallet's actual balance comes from
  logged income, not this number.
- **View the dashboard:** open `http://localhost:8000` on the tablet, or
  `http://<tablet-lan-ip>:8000` from a phone on the same wifi.
- **Log an expense from the dashboard too:** the Recent Activity section has a quick-add
  box that works exactly like messaging the bot — type `gym 1500` and hit Log.
- **Correct an account balance:** tap ✎ next to Wallet/Credit/Liquid in the Wallet &
  Accounts section (or `/correct wallet 42000` on the bot) to set the real balance
  directly — recorded as a neutral correction, not income or spend.
- **Manage categories:** use the 🏷 Categories panel on the dashboard to
  add/edit/delete categories, colors, default type/cadence, monthly caps, and
  keyword aliases — no code editing needed. A `saving`-type category can
  optionally "feed the Liquid fund" (its deposits count toward the Liquid
  balance instead of being tracked as an investment vehicle).
- **Edit or delete a logged expense:** on the dashboard, tap ✎ next to any
  row in Recent Activity — change its category, note, amount, date, payment
  source, or type/cadence tags, or delete it outright.
- **Track multiple investments:** add a vehicle from the Savings section (or
  🏷 Categories, type `saving`) and update each one's current value and
  contributed total via the ✎ Adjust popup (or click a point on the chart once
  a vehicle is selected) — a dropdown lets you view a single vehicle's own
  chart instead of the combined one. If a vehicle already held money before
  you started tracking it here, use ✎ Adjust to correct its "contributed"
  figure so gains aren't overstated.
- **Export your data:** the ⬇ Export button in the dashboard header downloads
  a full-history Excel workbook (transactions, monthly summary, category
  breakdown, accounts ledger, investment history) for offline analysis.
- **Ask the assistant:** the "💬 Ask about your finances" section can answer
  questions about your spending, summarize or restructure any part of the
  dashboard, and give basic guidance on investing/loans with pointers to
  general resources — needs `GEMINI_API_KEY` set. It uses your dashboard's
  aggregate data, never raw transactions, and costs one Gemini call per
  message you send (nothing on typing or polling).
- **Insights & Recap in one place:** today's snapshot, this month's insight
  bullets, cross-month trend commentary (once you have a few months of
  history), and the AI daily recap all live together in one dashboard
  section instead of being scattered across the page.
- **Bot commands:** `/start`, `/undo`, `/cat <Category>`, `/today`, `/month`,
  `/insights`, `/income <amount> [note]`, `/liquid <amount> [note]`,
  `/settle <amount>`, `/wallet`, `/correct <wallet|credit|liquid> <amount>`,
  `/salary <amount>`, `/credit <amount>`, `/portfolio <vehicle> <amount>`,
  `/recap`.

## Without a Telegram bot token

Leave `TELEGRAM_BOT_TOKEN` blank in `.env` and `main.py` will run the
dashboard only (useful for local development).

## Without a Gemini API key

Leave `GEMINI_API_KEY` blank — the parser falls back to regex-only
classification and tags unmatched expenses as `Other`.
