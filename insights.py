"""Deterministic metrics + phrased insight bullets for the monthly dashboard.

Metrics are computed in plain Python (free, exact) and phrased with string
templates — no Gemini call happens here. The only Gemini usage in the app is
parser.classify_with_gemini(), for category fallback when regex/keywords miss.
"""
from calendar import monthrange
from datetime import date

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

    monthly_salary = float(db.get_setting("monthly_salary", 0))
    credit_limit = float(db.get_setting("credit_limit", 0))
    budget = monthly_salary + credit_limit

    total = sum(t["amount"] for t in txns)
    salary_used = sum(t["amount"] for t in txns if t["payment_source"] == "salary")
    credit_used = sum(t["amount"] for t in txns if t["payment_source"] == "credit")

    days_elapsed = _days_elapsed(month)
    days_total = _days_in_month(month)
    projected = (total / days_elapsed * days_total) if days_elapsed else 0.0

    by_category = {}
    for t in txns:
        by_category.setdefault(t["category"], 0.0)
        by_category[t["category"]] += t["amount"]

    # "spend" excludes kind='saving' categories (e.g. Investments) — money that
    # left the account still counts fully against salary/credit above, but the
    # "where the money goes" pie and top-category card are about
    # discretionary/essential spending habits, not SIP contributions.
    by_category_spend = {
        cat: amount for cat, amount in by_category.items()
        if categories.get(cat, {}).get("kind") != "saving"
    }
    spend_total = sum(by_category_spend.values())
    spend_txns = [t for t in txns if categories.get(t["category"], {}).get("kind") != "saving"]

    top_category = None
    top_amount = 0.0
    if by_category_spend:
        top_category, top_amount = max(by_category_spend.items(), key=lambda kv: kv[1])
    top_pct = (top_amount / spend_total * 100) if spend_total else 0.0

    # "variable" additionally excludes recurrence != 'one-off' (rent, a SIP,
    # a subscription) — fixed costs you don't really choose month-to-month,
    # so "cutting your top category 20%" and "largest single hit" should be
    # computed from what's actually adjustable, not a locked-in fixed cost.
    variable_txns = [t for t in spend_txns if t["recurrence"] == "one-off"]
    fixed_txns = [t for t in spend_txns if t["recurrence"] != "one-off"]
    fixed_total = sum(t["amount"] for t in fixed_txns)
    variable_total = spend_total - fixed_total

    variable_by_category = {}
    for t in variable_txns:
        variable_by_category[t["category"]] = variable_by_category.get(t["category"], 0.0) + t["amount"]

    top_variable_category = None
    top_variable_amount = 0.0
    if variable_by_category:
        top_variable_category, top_variable_amount = max(variable_by_category.items(), key=lambda kv: kv[1])
    top_variable_pct = (top_variable_amount / variable_total * 100) if variable_total else 0.0

    largest = max(variable_txns, key=lambda t: t["amount"]) if variable_txns else None

    discretionary_total = sum(
        amount for cat, amount in by_category.items()
        if categories.get(cat, {}).get("kind") == "discretionary"
    )
    # denominator is spend_total (excludes savings), not total — otherwise a
    # big SIP payment inflates the base and understates your discretionary %
    discretionary_pct = (discretionary_total / spend_total * 100) if spend_total else 0.0

    savings_total = sum(
        amount for cat, amount in by_category.items()
        if categories.get(cat, {}).get("kind") == "saving"
    )

    return {
        "total": total,
        "spend_total": spend_total,
        "monthly_salary": monthly_salary,
        "credit_limit": credit_limit,
        "salary_used": salary_used,
        "salary_left": monthly_salary - salary_used,
        "credit_used": credit_used,
        "credit_left": credit_limit - credit_used,
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
        "top_variable_category": top_variable_category,
        "top_variable_amount": top_variable_amount,
        "top_variable_pct": top_variable_pct,
        "largest": largest,
        "discretionary_total": discretionary_total,
        "discretionary_pct": discretionary_pct,
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

    if m["credit_limit"] and m["credit_used"] >= m["credit_limit"] * 0.8:
        bullets.append({
            "text": f"Credit usage at ₹{m['credit_used']:,.0f} of ₹{m['credit_limit']:,.0f} — getting close to the limit.",
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

    if m["spend_total"]:
        status = "good" if m["discretionary_pct"] <= 40 else "watch"
        bullets.append({
            "text": f"Discretionary spend held at {m['discretionary_pct']:.0f}% of spend.",
            "status": status,
        })

    if m["savings_total"]:
        bullets.append({
            "text": f"Put aside ₹{m['savings_total']:,.0f} in savings/investments this month.",
            "status": "good",
        })

    return bullets
