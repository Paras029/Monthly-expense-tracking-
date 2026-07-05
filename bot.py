"""Telegram ingestion layer — the ONLY file to touch when swapping the
messaging channel (e.g. to WhatsApp). It parses a message, writes a row via
db.py, and replies. Nothing else in the app knows Telegram exists.
"""
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import config
import db
import insights
import parser

logger = logging.getLogger(__name__)


def _is_allowed(update: Update) -> bool:
    if not config.ALLOWED_TELEGRAM_USER_ID:
        return True
    return str(update.effective_user.id) == str(config.ALLOWED_TELEGRAM_USER_ID)


def _fmt_amount(amount: float) -> str:
    return f"₹{amount:,.0f}"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return

    message = update.message.text
    result = parser.parse_message(message)

    if "error" in result:
        await update.message.reply_text(result["error"])
        return

    txn_id = db.insert_transaction(
        category=result["category"],
        note=result["note"],
        amount=result["amount"],
        raw_message=message,
        guessed=result["guessed"],
        payment_source=result["payment_source"],
        expense_type=result["expense_type"],
        cadence=result["cadence"],
    )

    source_icon = {"credit": "💳", "liquid": "💧"}.get(result["payment_source"], "💰")
    type_badge = {
        "fixed": " · 📌 Fixed",
        "one-off": " · 🎲 One-off",
        "saving": " · 📈 Saving",
    }.get(result["expense_type"], "")
    cadence_badge = f" ({result['cadence']})" if result["cadence"] and result["expense_type"] != "variable" else ""

    reply = f"✅ {_fmt_amount(result['amount'])} · {result['category']}"
    if result["note"]:
        reply += f" · {result['note']}"
    reply += f" · {source_icon} {result['payment_source'].title()}"
    reply += type_badge + cadence_badge
    reply += f"  (id {txn_id})"
    if result["guessed"]:
        reply += "\n⚠️ guessed category — reply /cat Food to fix"

    await update.message.reply_text(reply)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    await update.message.reply_text(
        "Personal Cashflow Ledger\n\n"
        "Just send an expense as text, e.g.:\n"
        "  gym 1500\n"
        "  Zomato lunch 300\n"
        "  oyo 1500 travel\n"
        "  SIP index fund 5000\n"
        "  electricity bill 2200 credit    → paid by credit card\n"
        "  rent 21500 fixed                → fixed monthly cost\n"
        "  gym membership 12000 yearly     → fixed, annual cadence\n"
        "  flight to goa 15000 oneoff liquid → one-off anomaly, paid from Liquid fund\n\n"
        "Every expense gets two tags:\n"
        "  type: fixed (locked-in, e.g. rent) | variable (routine, e.g. food) | "
        "one-off (an anomaly, e.g. a trip — excluded from your monthly pace) | saving\n"
        "  cadence: daily/weekly/monthly/annual — how often it recurs\n"
        "New transactions default to their category's own tags (set once via 🏷 "
        "Categories); add a keyword ('fixed', 'oneoff', 'weekly', 'yearly', ...) to "
        "override just that message.\n\n"
        "Payment defaults to your Wallet unless you add 'credit'/'card' or 'liquid'.\n\n"
        "Commands:\n"
        "/undo — delete the last transaction\n"
        "/cat <Category> — recategorise the last transaction\n"
        "/today — today's spend\n"
        "/month — this month vs wallet & credit\n"
        "/insights — monthly insight bullets\n"
        "/income <amount> [note] — log money into your Wallet (salary, bonus, ...)\n"
        "/liquid <amount> [note] — move money from Wallet into your Liquid fund\n"
        "/settle <amount> — pay down Credit from your Wallet\n"
        "/wallet — wallet/credit/liquid balances + payday status\n"
        "/salary <amount> — set your reference monthly income (for %-used displays)\n"
        "/credit <amount> — set your credit limit\n"
        "/portfolio <vehicle> <amount> — log this month's value for one investment "
        "vehicle (e.g. /portfolio Investments 150000); no args lists all vehicles\n"
        "/recap — AI day-by-day + cumulative spending analysis (cached once/day; "
        "/recap refresh to force a new one)"
    )


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    last = db.get_last_transaction()
    if not last:
        await update.message.reply_text("Nothing to undo.")
        return
    db.delete_transaction(last["id"])
    await update.message.reply_text(
        f"🗑️ Removed {_fmt_amount(last['amount'])} · {last['category']} · {last['note'] or ''}"
    )


async def cmd_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /cat <Category>")
        return

    category = context.args[0]
    valid_names = {name.lower(): name for name in db.get_category_names()}
    if category.lower() not in valid_names:
        await update.message.reply_text(
            f"Unknown category. Choose one of: {', '.join(db.get_category_names())}"
        )
        return

    last = db.get_last_transaction()
    if not last:
        await update.message.reply_text("Nothing to recategorise.")
        return

    resolved = valid_names[category.lower()]
    db.update_transaction_category(last["id"], resolved)
    await update.message.reply_text(f"Updated to {resolved}.")


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    from datetime import date

    today = date.today().strftime("%Y-%m-%d")
    txns = db.get_transactions_for_day(today)
    total = sum(t["amount"] for t in txns)
    if not txns:
        await update.message.reply_text("No spend logged today.")
        return
    lines = [f"Today: {_fmt_amount(total)} across {len(txns)} transaction(s)"]
    for t in txns:
        lines.append(f"  · {_fmt_amount(t['amount'])} {t['category']} — {t['note'] or ''}")
    await update.message.reply_text("\n".join(lines))


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    from datetime import date

    month = date.today().strftime("%Y-%m")
    m = insights.compute_metrics(month)
    await update.message.reply_text(
        f"💰 Wallet: {_fmt_amount(m['wallet_used'])} spent this month "
        f"(vs {_fmt_amount(m['monthly_salary'])} reference income)\n"
        f"💳 Credit: {_fmt_amount(m['credit_outstanding'])} outstanding of "
        f"{_fmt_amount(m['credit_limit'])} limit ({_fmt_amount(m['credit_available'])} available)\n"
        f"Projected month-end: {_fmt_amount(m['projected'])}\n"
        f"Top category: {m['top_category'] or '—'} ({_fmt_amount(m['top_amount'])})\n"
        f"Saved/invested: {_fmt_amount(m['savings_total'])}"
    )


async def cmd_salary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    if not context.args:
        current = db.get_setting("monthly_salary", "0")
        await update.message.reply_text(f"Monthly salary is {_fmt_amount(float(current))}. Usage: /salary <amount>")
        return
    try:
        amount = float(context.args[0].replace(",", ""))
    except ValueError:
        await update.message.reply_text("Usage: /salary <amount>, e.g. /salary 60000")
        return
    db.set_setting("monthly_salary", str(amount))
    await update.message.reply_text(f"Monthly salary set to {_fmt_amount(amount)}.")


async def cmd_credit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    if not context.args:
        current = db.get_setting("credit_limit", "0")
        await update.message.reply_text(f"Credit limit is {_fmt_amount(float(current))}. Usage: /credit <amount>")
        return
    try:
        amount = float(context.args[0].replace(",", ""))
    except ValueError:
        await update.message.reply_text("Usage: /credit <amount>, e.g. /credit 20000")
        return
    db.set_setting("credit_limit", str(amount))
    await update.message.reply_text(f"Credit limit set to {_fmt_amount(amount)}.")


async def cmd_income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /income <amount> [note], e.g. /income 60000 July salary")
        return
    try:
        amount = float(context.args[0].replace(",", ""))
    except ValueError:
        await update.message.reply_text("Usage: /income <amount> [note], e.g. /income 60000 July salary")
        return
    note = " ".join(context.args[1:]) or None
    db.log_income(amount, note=note, raw_message=update.message.text)
    balance = db.get_wallet_balance()
    await update.message.reply_text(
        f"💰 +{_fmt_amount(amount)} added to Wallet{f' ({note})' if note else ''}. "
        f"New balance: {_fmt_amount(balance)}."
    )


async def cmd_liquid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /liquid <amount> [note], e.g. /liquid 5000 topping up emergency fund")
        return
    try:
        amount = float(context.args[0].replace(",", ""))
    except ValueError:
        await update.message.reply_text("Usage: /liquid <amount> [note], e.g. /liquid 5000 topping up emergency fund")
        return
    note = " ".join(context.args[1:]) or None
    db.deposit_to_liquid(amount, note=note, raw_message=update.message.text)
    wallet_balance = db.get_wallet_balance()
    liquid_balance = db.get_liquid_balance()
    await update.message.reply_text(
        f"💧 +{_fmt_amount(amount)} moved from Wallet to Liquid{f' ({note})' if note else ''}.\n"
        f"Wallet: {_fmt_amount(wallet_balance)} · Liquid: {_fmt_amount(liquid_balance)}."
    )


async def cmd_settle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    outstanding = db.get_credit_outstanding()
    if not context.args:
        if outstanding <= 0:
            await update.message.reply_text("No outstanding credit to settle.")
        else:
            await update.message.reply_text(
                f"Outstanding credit: {_fmt_amount(outstanding)}. Usage: /settle <amount>, "
                f"e.g. /settle {outstanding:.0f} to pay it off in full."
            )
        return
    try:
        amount = float(context.args[0].replace(",", ""))
    except ValueError:
        await update.message.reply_text("Usage: /settle <amount>, e.g. /settle 5000")
        return
    db.settle_credit(amount, raw_message=update.message.text)
    new_outstanding = db.get_credit_outstanding()
    await update.message.reply_text(
        f"💳 Settled {_fmt_amount(amount)} from Wallet. "
        f"Credit outstanding is now {_fmt_amount(new_outstanding)}."
    )


async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    wallet_balance = db.get_wallet_balance()
    credit_outstanding = db.get_credit_outstanding()
    liquid_balance = db.get_liquid_balance()
    settlement = insights.credit_settlement_status()
    lines = [
        f"💰 Wallet: {_fmt_amount(wallet_balance)}",
        f"💳 Credit outstanding: {_fmt_amount(credit_outstanding)}",
        f"💧 Liquid fund: {_fmt_amount(liquid_balance)}",
    ]
    if settlement["needs_settlement"]:
        lines.append(
            f"⚠️ Unsettled credit past payday ({settlement['payday']}) — /settle to pay it down."
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    from datetime import date

    month = date.today().strftime("%Y-%m")
    saving_categories = {
        c["name"]: c["name"] for c in db.get_categories()
        if c["expense_type"] == "saving" and not c["account_link"]
    }
    saving_by_lower = {name.lower(): name for name in saving_categories}

    if not context.args:
        # list every vehicle's latest value + how stale it is
        if not saving_categories:
            await update.message.reply_text(
                "No investment vehicles yet — add a category with expense_type 'saving' "
                "(e.g. Investments, Gold Plan, Fixed Deposits) in the dashboard's 🏷 Categories panel."
            )
            return
        lines = ["Investment vehicles:"]
        for name in saving_categories:
            latest = db.get_latest_investment_snapshot(name)
            if latest:
                days_ago = (date.today() - date.fromisoformat(latest["updated_at"][:10])).days
                lines.append(f"  {name}: {_fmt_amount(latest['value'])} (updated {days_ago}d ago)")
            else:
                lines.append(f"  {name}: no value logged yet")
        lines.append("\nUsage: /portfolio <vehicle> <amount>, e.g. /portfolio Investments 150000")
        await update.message.reply_text("\n".join(lines))
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /portfolio <vehicle> <amount>, e.g. /portfolio Investments 150000\n"
            "Run /portfolio with no arguments to see your vehicle names."
        )
        return

    *name_parts, amount_str = context.args
    vehicle_input = " ".join(name_parts)
    vehicle = saving_by_lower.get(vehicle_input.lower())
    if not vehicle:
        if saving_categories:
            await update.message.reply_text(
                f"Unknown vehicle {vehicle_input!r}. Choose one of: {', '.join(saving_categories)}"
            )
        else:
            await update.message.reply_text(
                "No investment vehicles yet — add a category with expense_type 'saving' first."
            )
        return
    try:
        amount = float(amount_str.replace(",", ""))
    except ValueError:
        await update.message.reply_text("Usage: /portfolio <vehicle> <amount>, e.g. /portfolio Investments 150000")
        return
    db.set_investment_snapshot(vehicle, month, amount)
    await update.message.reply_text(f"{vehicle} value for {month} set to {_fmt_amount(amount)}.")


async def cmd_insights(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    from datetime import date

    month = date.today().strftime("%Y-%m")
    bullets = insights.build_insights(month)
    if not bullets:
        await update.message.reply_text("Not enough data yet for insights.")
        return
    icons = {"good": "✓", "watch": "⚠", "note": "→"}
    lines = [f"{icons.get(b['status'], '•')} {b['text']}" for b in bullets]
    await update.message.reply_text("\n".join(lines))


async def cmd_recap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    from datetime import date

    import ai_insights

    force = bool(context.args and context.args[0].lower() in ("refresh", "force"))
    today = date.today().strftime("%Y-%m-%d")
    result = ai_insights.generate_recap(today, force=force)
    prefix = "✨" if result["source"] == "ai" else "→"
    lines = [f"{prefix} {l}" for l in result["lines"]]
    if result["source"] == "fallback":
        lines.append("(rule-based fallback — set GEMINI_API_KEY for AI analysis)")
    await update.message.reply_text("\n".join(lines))


def build_application() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("cat", cmd_cat))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("insights", cmd_insights))
    app.add_handler(CommandHandler("salary", cmd_salary))
    app.add_handler(CommandHandler("credit", cmd_credit))
    app.add_handler(CommandHandler("income", cmd_income))
    app.add_handler(CommandHandler("liquid", cmd_liquid))
    app.add_handler(CommandHandler("settle", cmd_settle))
    app.add_handler(CommandHandler("wallet", cmd_wallet))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("recap", cmd_recap))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return app


async def run_bot(app: Application):
    """Starts polling and blocks until cancelled. Meant to be run as one
    task in asyncio.gather alongside the dashboard server."""
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    try:
        import asyncio
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
