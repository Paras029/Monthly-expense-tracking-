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
  `Zomato lunch 300`, `oyo 1500 travel`.
- **Pay by credit card:** add `credit` (or `card`/`cc`), e.g.
  `electricity bill 2200 credit`. Default payment source is your salary.
- **Tag a fixed monthly cost:** add `fixed` (or `recurring`/`subscription`), e.g.
  `rent 21500 fixed`. It still counts against this month's salary/credit, but
  insights won't suggest "cutting" it the way they would a variable purchase.
- **Tag an annual cross-cutting cost:** add `yearly` (or `annual`/`annually`), e.g.
  `gym membership 12000 yearly`. Same idea, for costs paid once a year — both
  show up in the dashboard's "Fixed & recurring expenses" section instead of
  skewing one month's numbers.
- **Set your salary/credit limit:** `/salary 60000`, `/credit 20000` on the
  bot, or via the ⚙ Settings panel on the dashboard.
- **View the dashboard:** open `http://localhost:8000` on the tablet, or
  `http://<tablet-lan-ip>:8000` from a phone on the same wifi.
- **Manage categories:** use the 🏷 Categories panel on the dashboard to
  add/edit/delete categories, colors, monthly caps, and keyword aliases — no
  code editing needed. Kind `saving` = a long-term investment vehicle (SIP,
  gold plan, FDs — each tracked independently); kind `liquid` = an
  emergency/liquid cash fund (rolls up into the "Liquid fund" card at top).
- **Edit or delete a logged expense:** on the dashboard, tap ✎ next to any
  row in Recent Activity — change its category, note, amount, date, payment
  source, or fixed/yearly tag, or delete it outright.
- **Track multiple investments:** add a vehicle from the Long-term
  investments section (or 🏷 Categories, kind `saving`) and update each
  one's current value independently — the dashboard shows how long ago each
  was last updated.
- **Bot commands:** `/start`, `/undo`, `/cat <Category>`, `/today`, `/month`,
  `/insights`, `/salary <amount>`, `/credit <amount>`,
  `/portfolio <vehicle> <amount>`, `/recap`.

## Without a Telegram bot token

Leave `TELEGRAM_BOT_TOKEN` blank in `.env` and `main.py` will run the
dashboard only (useful for local development).

## Without a Gemini API key

Leave `GEMINI_API_KEY` blank — the parser falls back to regex-only
classification and tags unmatched expenses as `Other`.
