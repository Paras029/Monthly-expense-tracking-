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
- **Tag a recurring/annual cost:** add `yearly` (or `annual`/`recurring`), e.g.
  `gym membership 12000 yearly`. It still counts against this month's
  salary/credit, but shows up in its own "Recurring & annual expenses" section
  on the dashboard instead of skewing one month's numbers.
- **Set your salary/credit limit:** `/salary 60000`, `/credit 20000` on the
  bot, or via the ⚙ Settings panel on the dashboard.
- **View the dashboard:** open `http://localhost:8000` on the tablet, or
  `http://<tablet-lan-ip>:8000` from a phone on the same wifi.
- **Manage categories:** use the 🏷 Categories panel on the dashboard to
  add/edit/delete categories, colors, monthly caps, and keyword aliases — no
  code editing needed.
- **Bot commands:** `/start`, `/undo`, `/cat <Category>`, `/today`, `/month`,
  `/insights`, `/salary <amount>`, `/credit <amount>`.

## Without a Telegram bot token

Leave `TELEGRAM_BOT_TOKEN` blank in `.env` and `main.py` will run the
dashboard only (useful for local development).

## Without a Gemini API key

Leave `GEMINI_API_KEY` blank — the parser falls back to regex-only
classification and tags unmatched expenses as `Other`.
