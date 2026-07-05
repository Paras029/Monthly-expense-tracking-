"""Deterministic metrics + phrased insight bullets for the monthly dashboard.

Metrics are always computed in plain Python (free, exact). Gemini is only
used, optionally, to rephrase them into more natural sentences.
"""
from calendar import monthrange
from datetime import date

import config
import db


def _days_in_month(month):
    year, mon = (int(p) for p in month.split("-"))
    return monthrange(year, mon)[1]


def _days_elapsed(month):
    today = date.today()
    year, mon = (int(p) for p in month.split("-"))
    if (year, mon) == (today.year, today.month):
        return today.day
    return _days_in_month(month)


def compute_metrics(month):
    txns = db.get_transactions_for_month(month)
    categories = {c["name"]: c for c in db.get_categories()}
    budget = float(db.get_setting("monthly_budget", config.MONTHLY_BUDGET))

    total = sum(t["amount"] for t in txns)
    days_elapsed = _days_elapsed(month)
    days_total = _days_in_month(month)
    projected = (total / days_elapsed * days_total) if days_elapsed else 0.0

    by_category = {}
    for t in txns:
        by_category.setdefault(t["category"], 0.0)
        by_category[t["category"]] += t["amount"]

    top_category = None
    top_amount = 0.0
    if by_category:
        top_category, top_amount = max(by_category.items(), key=lambda kv: kv[1])
    top_pct = (top_amount / total * 100) if total else 0.0

    largest = max(txns, key=lambda t: t["amount"]) if txns else None

    discretionary_total = sum(
        amount for cat, amount in by_category.items()
        if categories.get(cat, {}).get("kind") == "discretionary"
    )
    discretionary_pct = (discretionary_total / total * 100) if total else 0.0

    return {
        "total": total,
        "budget": budget,
        "days_elapsed": days_elapsed,
        "days_total": days_total,
        "projected": projected,
        "by_category": by_category,
        "top_category": top_category,
        "top_amount": top_amount,
        "top_pct": top_pct,
        "largest": largest,
        "discretionary_total": discretionary_total,
        "discretionary_pct": discretionary_pct,
    }


def build_insights(month):
    m = compute_metrics(month)
    bullets = []

    if m["projected"] <= m["budget"]:
        bullets.append({
            "text": f"On pace for ₹{m['projected']:,.0f}, under your ₹{m['budget']:,.0f} budget.",
            "status": "good",
        })
    else:
        bullets.append({
            "text": f"On pace for ₹{m['projected']:,.0f}, over your ₹{m['budget']:,.0f} budget.",
            "status": "watch",
        })

    if m["top_category"]:
        saved_month = m["top_amount"] * 0.2
        bullets.append({
            "text": (
                f"{m['top_category']} is your top category at {m['top_pct']:.0f}% of spend. "
                f"Cutting it 20% saves ₹{saved_month:,.0f}/month (₹{saved_month * 12:,.0f}/year)."
            ),
            "status": "note",
        })

    if m["largest"]:
        t = m["largest"]
        note = t["note"] or "no note"
        bullets.append({
            "text": f"Largest single hit: ₹{t['amount']:,.0f} on {t['category']} ({note}).",
            "status": "note",
        })

    if m["total"]:
        status = "good" if m["discretionary_pct"] <= 40 else "watch"
        bullets.append({
            "text": f"Discretionary spend held at {m['discretionary_pct']:.0f}% of total.",
            "status": status,
        })

    return bullets
