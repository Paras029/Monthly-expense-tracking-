"""Deterministic metrics + phrased insight bullets for the monthly dashboard.

Metrics are computed in plain Python (free, exact) and phrased with string
templates — no Gemini call happens here. The only Gemini usage in the app is
parser.classify_with_gemini(), for category fallback when regex/keywords miss.
"""
from calendar import monthrange
from datetime import date, timedelta

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


def payday_date(today=None):
    """This month's payday: the 25th, or the last working day (Mon-Fri)
    before it if the 25th falls on a weekend."""
    today = today or date.today()
    payday = date(today.year, today.month, 25)
    while payday.weekday() >= 5:  # Saturday=5, Sunday=6
        payday -= timedelta(days=1)
    return payday


def credit_settlement_status():
    """Whether this cycle's credit balance should be nagged about: true once
    today is on/after this month's payday and there's still an outstanding
    balance to settle. Credit is revolving — outstanding carries forward
    across months until explicitly settled, it doesn't reset on its own."""
    today = date.today()
    payday = payday_date(today)
    outstanding = db.get_credit_outstanding()
    return {
        "payday": payday.isoformat(),
        "outstanding": outstanding,
        "needs_settlement": today >= payday and outstanding > 0,
    }


def compute_metrics(month):
    txns = db.get_transactions_for_month(month)

    monthly_salary = float(db.get_setting("monthly_salary", 0))  # a reference/expected income target, not the wallet's real balance
    credit_limit = float(db.get_setting("credit_limit", 0))
    budget = monthly_salary + credit_limit

    total = sum(t["amount"] for t in txns)
    wallet_used = sum(t["amount"] for t in txns if t["payment_source"] == "wallet")
    credit_used = sum(t["amount"] for t in txns if t["payment_source"] == "credit")
    liquid_used = sum(t["amount"] for t in txns if t["payment_source"] == "liquid")
    # Credit is revolving: an unsettled balance from a prior month still eats
    # into the same credit_limit ceiling, so "how close to the limit" has to
    # be judged against all-time outstanding, not just this month's charges.
    credit_outstanding = db.get_credit_outstanding(through_month=month)

    days_elapsed = _days_elapsed(month)
    days_total = _days_in_month(month)

    by_category = {}
    for t in txns:
        by_category.setdefault(t["category"], 0.0)
        by_category[t["category"]] += t["amount"]

    # "spend" excludes expense_type='saving' (e.g. a SIP) — money that left
    # the account still counts fully against wallet/credit/liquid above, but
    # the "where the money goes" donut and top-category card are about
    # spending habits, not saving contributions.
    spend_txns = [t for t in txns if t["expense_type"] != "saving"]
    by_category_spend = {}
    for t in spend_txns:
        by_category_spend[t["category"]] = by_category_spend.get(t["category"], 0.0) + t["amount"]
    spend_total = sum(by_category_spend.values())

    top_category = None
    top_amount = 0.0
    if by_category_spend:
        top_category, top_amount = max(by_category_spend.items(), key=lambda kv: kv[1])
    top_pct = (top_amount / spend_total * 100) if spend_total else 0.0

    # Of the three non-saving expense types: 'fixed' is locked-in (rent,
    # subscriptions) and 'one-off' is an anomaly (a trip, a big one-time
    # purchase) — neither is something you'd "cut" or that represents a
    # normal month's pace. Only 'variable' (routine day-to-day spend) is
    # what "cutting X%" / "largest single hit" insights should be based on.
    fixed_txns = [t for t in spend_txns if t["expense_type"] == "fixed"]
    variable_txns = [t for t in spend_txns if t["expense_type"] == "variable"]
    oneoff_txns = [t for t in spend_txns if t["expense_type"] == "one-off"]

    fixed_total = sum(t["amount"] for t in fixed_txns)
    variable_total = sum(t["amount"] for t in variable_txns)
    oneoff_total = sum(t["amount"] for t in oneoff_txns)
    oneoff_from_liquid = sum(t["amount"] for t in oneoff_txns if t["payment_source"] == "liquid")

    variable_by_category = {}
    for t in variable_txns:
        variable_by_category[t["category"]] = variable_by_category.get(t["category"], 0.0) + t["amount"]

    top_variable_category = None
    top_variable_amount = 0.0
    if variable_by_category:
        top_variable_category, top_variable_amount = max(variable_by_category.items(), key=lambda kv: kv[1])
    top_variable_pct = (top_variable_amount / variable_total * 100) if variable_total else 0.0

    largest = max(variable_txns, key=lambda t: t["amount"]) if variable_txns else None
    largest_oneoff = max(oneoff_txns, key=lambda t: t["amount"]) if oneoff_txns else None

    savings_total = sum(t["amount"] for t in txns if t["expense_type"] == "saving")

    # Fixed costs and savings are lump sums already logged in full for the
    # month — they don't recur again before month-end, so pacing them by
    # days-elapsed would fabricate a spike (e.g. rent paid on day 1 alone
    # would look like ₹21,500/day for the rest of the month). One-off
    # anomalies are excluded entirely: a single trip isn't a pattern that
    # continues for the rest of the month, and including it would make an
    # otherwise-ordinary month look like a blowout. Only variable_total
    # (actual day-to-day spend) is paced across the remaining days.
    variable_pace = (variable_total / days_elapsed * days_total) if days_elapsed else 0.0
    projected = fixed_total + savings_total + variable_pace

    return {
        "total": total,
        "spend_total": spend_total,
        "monthly_salary": monthly_salary,
        "credit_limit": credit_limit,
        "wallet_used": wallet_used,
        "wallet_left": monthly_salary - wallet_used,
        "credit_used": credit_used,
        "credit_left": credit_limit - credit_used,
        "credit_outstanding": credit_outstanding,
        "credit_available": credit_limit - credit_outstanding,
        "liquid_used": liquid_used,
        "budget": budget,
        "days_elapsed": days_elapsed,
        "days_total": days_total,
        "projected": projected,
        "by_category": by_category,
        "by_category_spend": by_category_spend,
        "top_category": top_category,
        "top_amount": top_amount,
        "top_pct": top_pct,
        "fixed_total": fixed_total,
        "variable_total": variable_total,
        "oneoff_total": oneoff_total,
        "oneoff_from_liquid": oneoff_from_liquid,
        "largest_oneoff": largest_oneoff,
        "top_variable_category": top_variable_category,
        "top_variable_amount": top_variable_amount,
        "top_variable_pct": top_variable_pct,
        "largest": largest,
        "savings_total": savings_total,
    }


def build_insights(month):
    m = compute_metrics(month)
    bullets = []

    if m["budget"]:
        if m["projected"] <= m["budget"]:
            bullets.append({
                "text": f"On pace for ₹{m['projected']:,.0f}, under your ₹{m['budget']:,.0f} salary + credit.",
                "status": "good",
            })
        else:
            bullets.append({
                "text": f"On pace for ₹{m['projected']:,.0f}, over your ₹{m['budget']:,.0f} salary + credit.",
                "status": "watch",
            })

    if m["credit_limit"] and m["credit_outstanding"] >= m["credit_limit"] * 0.8:
        bullets.append({
            "text": (
                f"Credit outstanding is ₹{m['credit_outstanding']:,.0f} of ₹{m['credit_limit']:,.0f} — "
                "getting close to the limit."
            ),
            "status": "watch",
        })

    settlement = credit_settlement_status()
    if settlement["needs_settlement"]:
        bullets.append({
            "text": (
                f"₹{settlement['outstanding']:,.0f} in credit is still unsettled past payday "
                f"({settlement['payday']}) — settle it to free up your limit."
            ),
            "status": "watch",
        })

    if m["fixed_total"]:
        fixed_pct = (m["fixed_total"] / m["spend_total"] * 100) if m["spend_total"] else 0
        bullets.append({
            "text": (
                f"Fixed costs (rent, subscriptions, etc.) are ₹{m['fixed_total']:,.0f}/month — "
                f"{fixed_pct:.0f}% of spend. ₹{m['variable_total']:,.0f} is actually flexible."
            ),
            "status": "note",
        })

    if m["top_variable_category"]:
        saved_month = m["top_variable_amount"] * 0.2
        bullets.append({
            "text": (
                f"{m['top_variable_category']} is your top variable-spend category at "
                f"{m['top_variable_pct']:.0f}% of what's flexible. "
                f"Cutting it 20% saves ₹{saved_month:,.0f}/month (₹{saved_month * 12:,.0f}/year)."
            ),
            "status": "note",
        })

    if m["largest"]:
        t = m["largest"]
        note = t["note"] or "no note"
        bullets.append({
            "text": f"Largest single (variable) hit: ₹{t['amount']:,.0f} on {t['category']} ({note}).",
            "status": "note",
        })

    if m["oneoff_total"]:
        t = m["largest_oneoff"]
        largest_bit = f" Largest: ₹{t['amount']:,.0f} on {t['category']} ({t['note'] or 'no note'})." if t else ""
        liquid_note = (
            "" if m["oneoff_from_liquid"] >= m["oneoff_total"]
            else " Consider paying these from your Liquid fund instead of Wallet/Credit."
        )
        bullets.append({
            "text": (
                f"₹{m['oneoff_total']:,.0f} in one-off spend this month — excluded from your pace above "
                f"since these are anomalies, not a monthly pattern.{largest_bit}{liquid_note}"
            ),
            "status": "note",
        })

    if m["savings_total"]:
        bullets.append({
            "text": f"Put aside ₹{m['savings_total']:,.0f} in savings/investments this month.",
            "status": "good",
        })

    return bullets
