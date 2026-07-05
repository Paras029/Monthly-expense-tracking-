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
    # Credit and Liquid are separate accounts with their own ceilings/balances
    # (credit_limit/credit_outstanding, liquid_balance) — the budget you're
    # actually spending against day to day is your salary/wallet alone, so
    # credit_limit is deliberately not added in here.
    budget = monthly_salary

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

    # "spend" is scoped to the Wallet only, excluding expense_type='saving'
    # (e.g. a SIP): Credit and Liquid are separate accounts with their own
    # balances/ceilings (credit_outstanding vs credit_limit, liquid_balance),
    # so blending their spend into "how much have I spent" would overstate
    # what's actually coming out of your salary/wallet this month — the
    # "where the money goes" donut, top-category card, and projected pace
    # are all about wallet spending habits specifically.
    spend_txns = [t for t in txns if t["expense_type"] != "saving" and t["payment_source"] == "wallet"]
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

    fixed_total = sum(t["amount"] for t in fixed_txns)
    variable_total = sum(t["amount"] for t in variable_txns)

    # One-off anomaly tracking deliberately looks at *all* payment sources,
    # not just wallet — the whole point is to flag anomalous spend and nudge
    # toward paying it from Liquid, regardless of which account it actually
    # hit; scoping this to wallet-only would hide the well-behaved case
    # (already paid from Liquid) as well as any paid by Credit.
    all_oneoff_txns = [t for t in txns if t["expense_type"] == "one-off"]
    oneoff_total = sum(t["amount"] for t in all_oneoff_txns)
    oneoff_from_liquid = sum(t["amount"] for t in all_oneoff_txns if t["payment_source"] == "liquid")

    variable_by_category = {}
    for t in variable_txns:
        variable_by_category[t["category"]] = variable_by_category.get(t["category"], 0.0) + t["amount"]

    top_variable_category = None
    top_variable_amount = 0.0
    if variable_by_category:
        top_variable_category, top_variable_amount = max(variable_by_category.items(), key=lambda kv: kv[1])
    top_variable_pct = (top_variable_amount / variable_total * 100) if variable_total else 0.0

    largest = max(variable_txns, key=lambda t: t["amount"]) if variable_txns else None
    largest_oneoff = max(all_oneoff_txns, key=lambda t: t["amount"]) if all_oneoff_txns else None

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
                "text": f"On pace for ₹{m['projected']:,.0f}, under your ₹{m['budget']:,.0f} salary/wallet budget.",
                "status": "good",
            })
        else:
            bullets.append({
                "text": f"On pace for ₹{m['projected']:,.0f}, over your ₹{m['budget']:,.0f} salary/wallet budget.",
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


def _shift_month(month, delta):
    year, mon = (int(p) for p in month.split("-"))
    mon += delta
    while mon <= 0:
        mon += 12
        year -= 1
    while mon > 12:
        mon -= 12
        year += 1
    return f"{year:04d}-{mon:02d}"


def build_trend_insights(month, lookback=3):
    """Cross-month pattern commentary — deterministic, zero Gemini calls,
    reusing compute_metrics() for each prior month. Only surfaces bullets
    once there's enough history to compare against; returns [] on a fresh
    install or a month with no prior data, rather than fabricating a trend
    out of nothing."""
    prior_months = [_shift_month(month, -i) for i in range(1, lookback + 1)]
    prior_metrics = [
        compute_metrics(m) for m in prior_months if db.get_transactions_for_month(m)
    ]
    if len(prior_metrics) < 2:
        return []

    current = compute_metrics(month)
    bullets = []

    avg_spend = sum(pm["spend_total"] for pm in prior_metrics) / len(prior_metrics)
    if avg_spend and current["days_elapsed"] >= 5:
        # only worth comparing once a handful of days have actually elapsed —
        # day 1-2 of a new month will always look "down" vs a full prior month
        projected_full_month = current["projected"]
        diff_pct = (projected_full_month - avg_spend) / avg_spend * 100
        if abs(diff_pct) >= 10:
            direction = "higher" if diff_pct > 0 else "lower"
            status = "watch" if diff_pct > 0 else "good"
            bullets.append({
                "text": (
                    f"On pace for {abs(diff_pct):.0f}% {direction} wallet spend than your "
                    f"{len(prior_metrics)}-month average (₹{avg_spend:,.0f})."
                ),
                "status": status,
            })

    # Longest streak (ending this month) of the same category being the top
    # spend category — a persistent pattern worth naming, not just a one-off.
    streak_months = [current] + list(reversed(prior_metrics))
    streak_category = current["top_category"]
    streak_len = 0
    if streak_category:
        for pm in streak_months:
            if pm["top_category"] == streak_category:
                streak_len += 1
            else:
                break
    if streak_len >= 3:
        bullets.append({
            "text": f"{streak_category} has been your top spend category for {streak_len} months running.",
            "status": "note",
        })

    # Longest streak (ending this month) of staying on/off pace against the
    # salary/wallet budget.
    if current["budget"]:
        over_streak = 0
        under_streak = 0
        for pm in streak_months:
            if not pm["budget"]:
                break
            if pm["projected"] > pm["budget"]:
                over_streak += 1
                if under_streak:
                    break
            else:
                under_streak += 1
                if over_streak:
                    break
        if over_streak >= 3:
            bullets.append({
                "text": f"Projected spend has run over your salary/wallet budget for {over_streak} months in a row.",
                "status": "watch",
            })
        elif under_streak >= 3:
            bullets.append({
                "text": f"On pace to stay under your salary/wallet budget for {under_streak} months in a row.",
                "status": "good",
            })

    return bullets
