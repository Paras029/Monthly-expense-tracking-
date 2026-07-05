"""AI-generated daily/weekly spending recap via Gemini.

Deliberately isolated from insights.py, which must stay pure-Python and
Gemini-free. This is the one place besides parser.classify_with_gemini()
that calls the model — and it's capped to roughly once per day: the result
is cached in the ai_recaps table and only regenerated when the cache is
missing/stale or the user explicitly asks for a refresh (dashboard button
or /recap on Telegram). Falls back to the existing rule-based insights if
no API key is set or the call fails, so it never blocks or crashes.
"""
import json
from datetime import date, timedelta

import config
import db
import insights
import parser

PROMPT_TEMPLATE = """You are a terse personal-finance analyst reviewing one person's \
spending data. Write a short day-by-day and cumulative analysis of their week/month so \
far. Only call out real, notable patterns — trends, spikes, pace vs their salary+credit, \
category shifts, streaks of high spend. Skip anything unremarkable; do not restate every \
number in the data.

Output 3-5 short plain-text lines, one observation per line. No headers, no markdown, no \
bullet characters, no preamble, no sign-off. Write amounts as ₹ with comma separators.

DATA:
{data}
"""


def _gather_recap_data(day_str):
    day = date.fromisoformat(day_str)
    month = day_str[:7]

    m = insights.compute_metrics(month)

    today_txns = db.get_transactions_for_day(day_str)
    today_by_category = {}
    for t in today_txns:
        today_by_category[t["category"]] = today_by_category.get(t["category"], 0.0) + t["amount"]

    last_7_days = []
    for i in range(6, -1, -1):
        d = (day - timedelta(days=i)).strftime("%Y-%m-%d")
        txns = db.get_transactions_for_day(d)
        last_7_days.append({"date": d, "total": sum(t["amount"] for t in txns)})

    prev_week_total = 0.0
    for i in range(13, 6, -1):
        d = (day - timedelta(days=i)).strftime("%Y-%m-%d")
        txns = db.get_transactions_for_day(d)
        prev_week_total += sum(t["amount"] for t in txns)

    categories_over_cap = [
        c["name"] for c in db.get_categories()
        if c["monthly_cap"] and m["by_category"].get(c["name"], 0) > c["monthly_cap"]
    ]

    return {
        "today": {
            "date": day_str,
            "total": round(sum(today_by_category.values())),
            "by_category": {k: round(v) for k, v in today_by_category.items()},
        },
        "last_7_days_totals": [{"date": d["date"], "total": round(d["total"])} for d in last_7_days],
        "this_week_total": round(sum(d["total"] for d in last_7_days)),
        "previous_week_total": round(prev_week_total),
        "month_to_date": {
            "total_incl_savings": round(m["total"]),
            "spend_excl_savings": round(m["spend_total"]),
            "savings_contributed": round(m["savings_total"]),
            "top_category": m["top_category"],
            "top_category_amount": round(m["top_amount"]) if m["top_category"] else None,
            "discretionary_pct_of_spend": round(m["discretionary_pct"], 1),
        },
        "projected_month_end": round(m["projected"]),
        "monthly_salary": m["monthly_salary"],
        "credit_limit": m["credit_limit"],
        "categories_over_their_cap": categories_over_cap,
    }


def _fallback_lines(day_str):
    return [b["text"] for b in insights.build_insights(day_str[:7])]


def generate_recap(day_str, force=False):
    """Returns {lines, source, generated_at}. source is 'ai' or 'fallback'.

    Cached per-day so repeated dashboard loads/bot calls on the same day cost
    zero extra Gemini calls; force=True bypasses the cache for a manual
    refresh after logging more expenses.
    """
    if not force:
        cached = db.get_ai_recap(day_str)
        if cached:
            return {
                "lines": cached["text"].split("\n"),
                "source": cached["source"],
                "generated_at": cached["generated_at"],
            }

    source = "ai"
    if not config.GEMINI_API_KEY:
        lines = _fallback_lines(day_str)
        source = "fallback"
    else:
        try:
            data = _gather_recap_data(day_str)
            prompt = PROMPT_TEMPLATE.format(data=json.dumps(data))
            text = parser.call_gemini(prompt, temperature=0.3)
            lines = [
                line.strip("-• ").strip()
                for line in (text or "").strip().splitlines()
                if line.strip()
            ]
            if not lines:
                raise ValueError("empty response from Gemini")
        except Exception:
            lines = _fallback_lines(day_str)
            source = "fallback"

    generated_at = db.set_ai_recap(day_str, "\n".join(lines), source)
    return {"lines": lines, "source": source, "generated_at": generated_at}
