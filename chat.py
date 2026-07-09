"""Gemini-backed chat assistant: expense Q&A, dashboard summaries, and basic
financial literacy guidance (investing/loan basics, general resources).

Deliberately efficient with API calls — exactly one Gemini call per user-
submitted message (never on typing/polling), a compact pre-computed data
snapshot (never raw transactions, keeping the payload small), and a capped
conversation history so token usage per call doesn't grow unbounded over a
long chat session.
"""
import json
import logging
from datetime import date

import config
import db
import insights
import parser

logger = logging.getLogger(__name__)

MAX_HISTORY_TURNS = 8  # user+model pairs kept per request

SYSTEM_CONTEXT_TEMPLATE = """You are a helpful, concise financial assistant embedded \
in the user's personal expense-tracking dashboard (Personal Cashflow Ledger, India, \
currency INR/₹). You have access to their aggregated spending data below — never raw \
transaction-level detail beyond what's summarized here.

You can:
- Answer questions about their spending, budget, savings, and accounts using the data below.
- Summarize or explain any part of their dashboard in plain language, or give a more \
structured view of it if asked.
- Give basic, general financial literacy guidance — e.g. how someone might start \
investing, what to weigh before taking a loan for a big purchase — clearly framed as \
general education, not individualized professional financial/legal/tax advice. When \
relevant, point to well-known general resources (e.g. SEBI/RBI investor education, \
Zerodha Varsity) rather than recommending specific stocks or products.

Keep replies short and conversational — a few sentences or a short list, not an essay. \
Never fabricate numbers not present in the data below; say so plainly if something \
isn't tracked in the app.

DATA:
{data}
"""

UNAVAILABLE_REPLY = (
    "Chat needs a Gemini API key to work — set GEMINI_API_KEY in your .env to enable it."
)


def _gather_financial_context(month):
    """Compact aggregates only — mirrors ai_insights.py's approach of never
    sending raw transactions to Gemini, just pre-computed summaries."""
    m = insights.compute_metrics(month)
    months = db.get_all_months()[-6:]
    monthly_trend = db.get_monthly_totals(months)

    categories = [
        {"name": c["name"], "type": c["expense_type"], "cap": c["monthly_cap"]}
        for c in db.get_categories()
    ]
    budgets_over_cap = [
        c["name"] for c in db.get_categories()
        if c["monthly_cap"] and m["by_category"].get(c["name"], 0) > c["monthly_cap"]
    ]

    vehicles = []
    for c in db.get_categories():
        if c["expense_type"] == "saving" and not c["account_link"]:
            latest = db.get_latest_investment_snapshot(c["name"])
            vehicles.append({"name": c["name"], "latest_value": latest["value"] if latest else None})

    return {
        "current_month": month,
        "this_month": {
            "wallet_spend": round(m["spend_total"]),
            "fixed_costs": round(m["fixed_total"]),
            "variable_spend": round(m["variable_total"]),
            "oneoff_spend": round(m["oneoff_total"]),
            "savings_contributed": round(m["savings_total"]),
            "top_spend_category": m["top_category"],
            "projected_month_end_wallet_spend": round(m["projected"]),
            "reference_monthly_income": m["monthly_salary"],
        },
        "accounts": {
            "wallet_balance": round(db.get_wallet_balance()),
            "credit_outstanding": round(db.get_credit_outstanding()),
            "credit_limit": float(db.get_setting("credit_limit", 0)),
            "liquid_balance": round(db.get_liquid_balance()),
        },
        "last_6_months_total_outflow": monthly_trend,
        "categories": categories,
        "categories_over_their_monthly_cap": budgets_over_cap,
        "investment_vehicles": vehicles,
    }


def _build_contents(history, user_message, data_context):
    """history: list of {role: 'user'|'model', text: str} from prior turns
    (already capped). The data snapshot is injected once as part of the
    first turn so it's always in context without re-describing it (and
    re-spending tokens on it) every single message."""
    intro = SYSTEM_CONTEXT_TEMPLATE.format(data=json.dumps(data_context))
    contents = []
    if not history:
        contents.append({"role": "user", "parts": [{"text": f"{intro}\n\nUser: {user_message}"}]})
        return contents

    contents.append({"role": "user", "parts": [{"text": intro}]})
    contents.append({
        "role": "model",
        "parts": [{"text": "Got it — I can see your spending data. What would you like to know?"}],
    })
    for turn in history[-MAX_HISTORY_TURNS * 2:]:
        contents.append({"role": turn["role"], "parts": [{"text": turn["text"]}]})
    contents.append({"role": "user", "parts": [{"text": user_message}]})
    return contents


def chat_reply(history, user_message, month=None):
    """Returns {reply, source} — source is 'ai' | 'unavailable' | 'error'."""
    if not config.GEMINI_API_KEY:
        return {"reply": UNAVAILABLE_REPLY, "source": "unavailable"}

    try:
        month = month or date.today().strftime("%Y-%m")
        data_context = _gather_financial_context(month)
        contents = _build_contents(history[-MAX_HISTORY_TURNS * 2:], user_message, data_context)
        reply = parser.call_gemini(None, temperature=0.4, contents=contents)
        if not reply or not reply.strip():
            raise ValueError("empty response from Gemini")
        return {"reply": reply.strip(), "source": "ai"}
    except Exception:
        logger.exception("chat_reply failed")
        return {
            "reply": "Sorry, I couldn't reach the assistant right now — try again in a moment.",
            "source": "error",
        }
