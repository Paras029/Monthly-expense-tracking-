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
    )

    reply = f"✅ {_fmt_amount(result['amount'])} · {result['category']}"
    if result["note"]:
        reply += f" · {result['note']}"
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
        "  SIP index fund 5000\n\n"
        "Commands:\n"
        "/undo — delete the last transaction\n"
        "/cat <Category> — recategorise the last transaction\n"
        "/today — today's spend\n"
        "/month — this month vs budget\n"
        "/insights — monthly insight bullets"
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
        f"Spent this month: {_fmt_amount(m['total'])} of {_fmt_amount(m['budget'])} budget\n"
        f"Projected month-end: {_fmt_amount(m['projected'])}\n"
        f"Top category: {m['top_category'] or '—'} ({_fmt_amount(m['top_amount'])})"
    )


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


def build_application() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("cat", cmd_cat))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("insights", cmd_insights))
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
