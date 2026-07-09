"""FastAPI app: JSON API for the dashboard + serves the static index.html.
New transactions can be created either by the Telegram bot or, via the
quick-add endpoint below, directly from the dashboard — both go through the
same parser.parse_message() so a dashboard-logged expense is parsed
identically to a Telegram one."""
import io
from datetime import date, datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ai_insights
import db
import export
import insights
import parser

app = FastAPI(title="Personal Cashflow Ledger")


def _current_month() -> str:
    return date.today().strftime("%Y-%m")


def _today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def _last_n_months(n: int, month: str) -> list[str]:
    year, mon = (int(p) for p in month.split("-"))
    months = []
    for i in range(n - 1, -1, -1):
        m = mon - i
        y = year
        while m <= 0:
            m += 12
            y -= 1
        months.append(f"{y:04d}-{m:02d}")
    return months


def _is_vehicle(cat):
    """A real investment vehicle: expense_type='saving' and not linked to
    the Liquid account (account_link='liquid' categories are cash deposits,
    not something with a fluctuating value/gain to track)."""
    return cat["expense_type"] == "saving" and not cat["account_link"]


def _savings_effective_balance(month):
    """Sum across all investment vehicles of (manually-set value as of this
    month if one exists, else cumulative contributed-to-date as a floor
    estimate) — a "real" savings figure that updates the moment a vehicle's
    value is set, not just when a new transaction is logged. Shared by the
    top Savings card and the Long-term investments total so they always
    agree."""
    total = 0.0
    for cat in db.get_categories():
        if not _is_vehicle(cat):
            continue
        snap = db.get_snapshot_as_of(cat["name"], month)
        if snap:
            total += snap["value"]
        else:
            contributed = db.get_cumulative_savings_contributions([month], category=cat["name"])
            total += contributed[0]["contributed"] if contributed else 0.0
    return total


@app.get("/api/summary")
def api_summary(month: str = Query(default=None), view: str = Query(default="wallet")):
    month = month or _current_month()
    m = insights.compute_metrics(month)
    categories = {c["name"]: c for c in db.get_categories()}

    # "view" re-slices the same month's breakdown for the "where the money
    # goes" toggle: 'wallet' (the default) is the usual spend-pattern donut —
    # money that actually left your salary/wallet, excluding saving so
    # contributions don't dominate a chart about spending habits. 'credit'/
    # 'liquid' slice by that account instead — they're separate pools with
    # their own ceiling/balance, not blended into the wallet spend view.
    # 'saving' shows real investment-vehicle contributions.
    if view == "wallet":
        by_view = m["by_category_spend"]
    elif view == "credit":
        by_view = {}
        for t in db.get_transactions_for_month(month):
            if t["payment_source"] == "credit":
                by_view[t["category"]] = by_view.get(t["category"], 0.0) + t["amount"]
    elif view == "liquid":
        by_view = {}
        for t in db.get_transactions_for_month(month):
            if t["payment_source"] == "liquid":
                by_view[t["category"]] = by_view.get(t["category"], 0.0) + t["amount"]
    elif view == "saving":
        by_view = {
            cat: amount for cat, amount in m["by_category"].items()
            if _is_vehicle(categories.get(cat, {"expense_type": None, "account_link": None}))
        }
    else:
        raise HTTPException(400, f"Unknown view {view!r}")

    view_total = sum(by_view.values())
    breakdown = [
        {
            "category": cat,
            "amount": amount,
            "pct": (amount / view_total * 100) if view_total else 0,
            "color": categories.get(cat, {}).get("color", "#64748b"),
            "expense_type": categories.get(cat, {}).get("expense_type", "variable"),
        }
        for cat, amount in sorted(by_view.items(), key=lambda kv: -kv[1])
    ]

    days_left = max(m["days_total"] - m["days_elapsed"], 0)
    # Wallet-only, matching the rest of the "expenditure" framing above —
    # Credit's own available limit is a separate ceiling, not additional
    # room in your day-to-day salary/wallet budget.
    per_day_left = (m["wallet_left"] / days_left) if days_left else 0.0
    # Pace reflects actual day-to-day (variable) spend, not total — a fixed
    # lump sum (rent, a SIP) logged once on day 1 shouldn't read as a
    # ₹21,500/day pace for the rest of the month. One-off anomalies (a trip)
    # are excluded entirely, same as in insights.compute_metrics.
    pace_per_day = (m["variable_total"] / m["days_elapsed"]) if m["days_elapsed"] else 0.0

    # savings_balance is the "effective" value (manually-set vehicle values,
    # falling back to contributed-to-date for ones never valued) — distinct
    # from savings_total, which is just *this month's* new contribution.
    # wallet_balance/liquid_balance are cumulative running balances that
    # already carry forward correctly month to month; credit is revolving
    # debt that only shrinks via an explicit settlement.
    savings_balance = _savings_effective_balance(month)
    wallet_balance = db.get_wallet_balance(through_month=month)
    liquid_balance = db.get_liquid_balance(through_month=month)
    settlement = insights.credit_settlement_status()

    return {
        "month": month,
        "spent": m["total"],
        "spend_total": m["spend_total"],
        "projected": m["projected"],
        "pace_per_day": pace_per_day,
        "days_left": days_left,
        "per_day_left": per_day_left,
        "monthly_salary": m["monthly_salary"],
        "wallet_used": m["wallet_used"],
        "wallet_left": m["wallet_left"],
        "wallet_balance": wallet_balance,
        "wallet_pct_used": (m["wallet_used"] / m["monthly_salary"] * 100) if m["monthly_salary"] else 0,
        "credit_limit": m["credit_limit"],
        "credit_used": m["credit_used"],
        "credit_outstanding": m["credit_outstanding"],
        "credit_available": m["credit_available"],
        "credit_pct_used": (m["credit_outstanding"] / m["credit_limit"] * 100) if m["credit_limit"] else 0,
        "credit_needs_settlement": settlement["needs_settlement"],
        "credit_payday": settlement["payday"],
        "top_category": m["top_category"],
        "top_amount": m["top_amount"],
        "top_pct": m["top_pct"],
        "oneoff_total": m["oneoff_total"],
        "savings_total": m["savings_total"],
        "savings_balance": savings_balance,
        "liquid_balance": liquid_balance,
        "view": view,
        "view_total": view_total,
        "breakdown": breakdown,
    }


@app.get("/api/insights")
def api_insights(month: str = Query(default=None)):
    month = month or _current_month()
    today_txns = db.get_transactions_for_day(_today_str())
    today_total = sum(t["amount"] for t in today_txns)
    today_by_category = {}
    for t in today_txns:
        today_by_category[t["category"]] = today_by_category.get(t["category"], 0.0) + t["amount"]
    today_top = max(today_by_category.items(), key=lambda kv: kv[1])[0] if today_by_category else None
    return {
        "month": month,
        "today": {"total": today_total, "count": len(today_txns), "top_category": today_top},
        "insights": insights.build_insights(month),
        "trends": insights.build_trend_insights(month),
    }


@app.get("/api/recap")
def api_recap(date_: str = Query(default=None, alias="date")):
    """Cached AI daily recap — at most one Gemini call per day unless refreshed."""
    day = date_ or _today_str()
    return {"date": day, **ai_insights.generate_recap(day, force=False)}


@app.post("/api/recap/refresh")
def api_recap_refresh(date_: str = Query(default=None, alias="date")):
    day = date_ or _today_str()
    return {"date": day, **ai_insights.generate_recap(day, force=True)}


@app.get("/api/daily-burn")
def api_daily_burn(month: str = Query(default=None), category: str = Query(default=None)):
    month = month or _current_month()
    txns = db.get_transactions_for_month(month)
    variable_only = category == "__variable__"
    if variable_only:
        txns = [t for t in txns if t["expense_type"] == "variable"]
    elif category:
        txns = [t for t in txns if t["category"] == category]
    m = insights.compute_metrics(month)

    daily_totals = {}
    for t in txns:
        day = int(t["spent_on"].split("-")[2])
        daily_totals[day] = daily_totals.get(day, 0.0) + t["amount"]

    series = []
    running = 0.0
    for day in range(1, m["days_total"] + 1):
        amount = daily_totals.get(day, 0.0)
        running += amount
        series.append({"day": day, "amount": amount, "cumulative": running})

    return {
        "month": month,
        "category": category,
        "series": series,
    }


@app.get("/api/monthly")
def api_monthly(month: str = Query(default=None), category: str = Query(default=None)):
    month = month or _current_month()
    months = _last_n_months(6, month)
    if category == "__variable__":
        return {"months": db.get_monthly_totals(months, expense_type="variable")}
    return {"months": db.get_monthly_totals(months, category=category)}


@app.get("/api/savings")
def api_savings(month: str = Query(default=None)):
    month = month or _current_month()
    months = _last_n_months(6, month)
    return {"months": db.get_monthly_totals(months, expense_type="saving")}


@app.get("/api/recurring")
def api_recurring():
    """Fixed & recurring costs — split by cadence since a monthly-fixed cost
    (rent, a subscription) is re-logged every month (we show only the latest
    instance of each), while annual cross-cutting ones are rare enough to
    list in full. monthly_equivalent_total lets the dashboard show one
    combined "≈₹X/month locked in" figure."""
    monthly = db.get_fixed_monthly_costs()
    yearly = db.get_annual_costs()
    monthly_total = sum(t["amount"] for t in monthly)
    yearly_total = sum(t["amount"] for t in yearly)
    return {
        "monthly": monthly,
        "yearly": yearly,
        "monthly_total": monthly_total,
        "yearly_total": yearly_total,
        "monthly_equivalent_total": monthly_total + yearly_total / 12,
    }


@app.get("/api/oneoff")
def api_oneoff(month: str = Query(default=None)):
    """One-off/anomalous spend for the month — a trip, a big one-time
    purchase. Excluded from the projected-month-end pace (see insights.py)
    since it's not a recurring pattern; surfaced here so it stays visible
    rather than silently vanishing from the numbers."""
    month = month or _current_month()
    txns = db.get_oneoff_transactions(months=[month])
    total = sum(t["amount"] for t in txns)
    from_liquid = sum(t["amount"] for t in txns if t["payment_source"] == "liquid")
    return {"month": month, "transactions": txns, "total": total, "from_liquid": from_liquid}


# ---- accounts: wallet (income) / credit (settlement) / liquid ------------

@app.get("/api/accounts")
def api_accounts():
    """Wallet/Credit/Liquid balances + payday settlement status — all-time,
    not scoped to a month, since these are running account balances."""
    settlement = insights.credit_settlement_status()
    credit_limit = float(db.get_setting("credit_limit", 0))
    return {
        "wallet_balance": db.get_wallet_balance(),
        "credit_outstanding": settlement["outstanding"],
        "credit_limit": credit_limit,
        "credit_available": credit_limit - settlement["outstanding"],
        "liquid_balance": db.get_liquid_balance(),
        "payday": settlement["payday"],
        "needs_settlement": settlement["needs_settlement"],
    }


@app.get("/api/accounts/transactions")
def api_account_transactions(account: str = Query(default=None), limit: int = Query(default=30)):
    if account and account not in ("wallet", "credit", "liquid"):
        raise HTTPException(400, "account must be 'wallet', 'credit', or 'liquid'")
    return {"transactions": db.get_account_transactions(account=account, limit=limit)}


class IncomeIn(BaseModel):
    amount: float
    note: Optional[str] = None
    txn_date: Optional[str] = None


@app.post("/api/accounts/income")
def api_log_income(body: IncomeIn):
    if body.amount <= 0:
        raise HTTPException(400, "amount must be positive")
    txn_id = db.log_income(body.amount, note=body.note, txn_date=body.txn_date, source="dashboard")
    return {"ok": True, "id": txn_id, "wallet_balance": db.get_wallet_balance()}


class SettleIn(BaseModel):
    amount: float
    note: Optional[str] = None
    txn_date: Optional[str] = None


@app.post("/api/accounts/settle")
def api_settle_credit(body: SettleIn):
    if body.amount <= 0:
        raise HTTPException(400, "amount must be positive")
    db.settle_credit(body.amount, note=body.note, txn_date=body.txn_date, source="dashboard")
    return {
        "ok": True,
        "credit_outstanding": db.get_credit_outstanding(),
        "wallet_balance": db.get_wallet_balance(),
    }


class LiquidDepositIn(BaseModel):
    amount: float
    note: Optional[str] = None
    txn_date: Optional[str] = None


@app.post("/api/accounts/liquid-deposit")
def api_liquid_deposit(body: LiquidDepositIn):
    """Move money from the Wallet into the Liquid reserve — subtracts from
    the Wallet balance, adds to Liquid. Auto-creates a default liquid-linked
    category on first use so this never requires prior category setup."""
    if body.amount <= 0:
        raise HTTPException(400, "amount must be positive")
    txn_id = db.deposit_to_liquid(body.amount, note=body.note, spent_on=body.txn_date, source="dashboard")
    return {"ok": True, "id": txn_id, "wallet_balance": db.get_wallet_balance(), "liquid_balance": db.get_liquid_balance()}


class AccountCorrectionIn(BaseModel):
    account: str  # 'wallet' | 'credit' | 'liquid'
    new_balance: float
    note: Optional[str] = None
    txn_date: Optional[str] = None


@app.post("/api/accounts/correct")
def api_correct_account_balance(body: AccountCorrectionIn):
    """Directly set Wallet/Credit/Liquid to a known-correct balance — a
    neutral correction, not an income/settlement/deposit, for reconciling
    drift (e.g. the account already had a balance before this app was set
    up to track it). Handy any time, not just once at setup."""
    if body.account == "wallet":
        db.correct_wallet_balance(body.new_balance, note=body.note, txn_date=body.txn_date, source="dashboard")
    elif body.account == "credit":
        db.correct_credit_outstanding(body.new_balance, note=body.note, txn_date=body.txn_date, source="dashboard")
    elif body.account == "liquid":
        db.correct_liquid_balance(body.new_balance, note=body.note, txn_date=body.txn_date, source="dashboard")
    else:
        raise HTTPException(400, "account must be 'wallet', 'credit', or 'liquid'")
    return {
        "ok": True,
        "wallet_balance": db.get_wallet_balance(),
        "credit_outstanding": db.get_credit_outstanding(),
        "liquid_balance": db.get_liquid_balance(),
    }


def _vehicle_gain(latest_value, contributed):
    """Gain is a plain value-vs-contributed comparison: how much more (or
    less) a vehicle is worth than the total that's actually gone into it.
    `contributed` already honors any contributed_override (see §5c in
    CLAUDE.md), which is what makes this comparison meaningful even for a
    vehicle that held money before the app started tracking it — without
    that correction this same math would previously have fabricated a huge
    gain. Returns (gain, gain_pct) — both None if no value has been set yet."""
    if latest_value is None:
        return None, None
    gain = latest_value - contributed
    gain_pct = (gain / contributed * 100) if contributed else None
    return gain, gain_pct


@app.get("/api/investments")
def api_investments(month: str = Query(default=None), n: int = Query(default=12)):
    """Savings/investment tracking, per vehicle: each real investment vehicle
    category (SIP, a gold plan, FDs — expense_type='saving' and not linked
    to the Liquid account) is tracked independently — its own automatically-
    computed cumulative contribution, its own manually-updated current
    value, and how long ago that value was last updated. Gain is current
    value vs. contributed (see _vehicle_gain). "Effective value" (the
    manually-set value if one exists as of this month, else contributed-to-
    date as a floor estimate) feeds both the combined chart and the top
    Savings card, so updating a vehicle's value immediately updates the
    top-level figure too."""
    month = month or _current_month()
    months = _last_n_months(n, month)
    saving_categories = [c for c in db.get_categories() if _is_vehicle(c)]

    combined_contributed = {m: 0.0 for m in months}
    combined_effective_value = {m: 0.0 for m in months}

    vehicles = []
    for cat in saving_categories:
        name = cat["name"]
        contributed_by_month = {
            row["month"]: row["contributed"]
            for row in db.get_cumulative_savings_contributions(months, category=name)
        }
        vehicle_months = []
        for m in months:
            snap = db.get_snapshot_as_of(name, m)
            contributed = contributed_by_month.get(m, 0.0)
            value = snap["value"] if snap else None
            effective = value if value is not None else contributed
            vehicle_months.append({"month": m, "contributed": contributed, "value": value})
            combined_contributed[m] += contributed
            combined_effective_value[m] += effective

        latest_contributed = contributed_by_month.get(months[-1], 0.0) if months else 0.0
        latest_snapshot = db.get_latest_investment_snapshot(name)
        latest_value = latest_snapshot["value"] if latest_snapshot else None
        updated_at = latest_snapshot["updated_at"] if latest_snapshot else None
        updated_days_ago = None
        if updated_at:
            updated_days_ago = (date.today() - datetime.fromisoformat(updated_at).date()).days

        gain, gain_pct = _vehicle_gain(latest_value, latest_contributed)

        target_progress = None
        if cat["target_amount"]:
            effective_now = latest_value if latest_value is not None else latest_contributed
            target_progress = min(effective_now / cat["target_amount"] * 100, 999)

        vehicles.append({
            "category": name,
            "color": cat["color"],
            "parent_category": cat["parent_category"],
            "contributed": latest_contributed,
            "latest_value": latest_value,
            "updated_at": updated_at,
            "updated_days_ago": updated_days_ago,
            "gain": gain,
            "gain_pct": gain_pct,
            "target_amount": cat["target_amount"],
            "target_date": cat["target_date"],
            "target_progress_pct": target_progress,
            "months": vehicle_months,
        })

    vehicles.sort(key=lambda v: -v["contributed"])

    combined_months = [
        {"month": m, "contributed": combined_contributed[m], "value": combined_effective_value[m]}
        for m in months
    ]
    total_contributed = combined_contributed[months[-1]] if months else 0.0
    total_value = combined_effective_value[months[-1]] if months else 0.0
    # Sum each vehicle's own gain directly, rather than diffing the combined
    # totals above — those blend in contributed-as-floor for any vehicle
    # that's never had a value set, which would understate the real ratio.
    known_gains = [v["gain"] for v in vehicles if v["gain"] is not None]
    total_gain = sum(known_gains) if known_gains else None
    known_contributed = sum(v["contributed"] for v in vehicles if v["gain"] is not None)
    total_gain_pct = (total_gain / known_contributed * 100) if total_gain is not None and known_contributed else None

    return {
        "vehicles": vehicles,
        "combined_months": combined_months,
        "total_contributed": total_contributed,
        "total_value": total_value,
        "total_gain": total_gain,
        "total_gain_pct": total_gain_pct,
    }


class InvestmentSnapshotIn(BaseModel):
    category: str
    month: str
    value: float
    note: Optional[str] = None
    # Manual correction to the running "contributed" total as of `month` —
    # lets a user back-fill money a vehicle already held before they started
    # tracking it in this app (see db.set_investment_snapshot). Omit to leave
    # any existing correction untouched.
    contributed_override: Optional[float] = None


@app.post("/api/investments")
def api_add_investment_snapshot(body: InvestmentSnapshotIn):
    categories = {c["name"]: c for c in db.get_categories()}
    if body.category not in categories:
        raise HTTPException(404, f"Category {body.category!r} not found")
    if not _is_vehicle(categories[body.category]):
        raise HTTPException(400, f"{body.category!r} is not an investment vehicle")
    updated_at = db.set_investment_snapshot(
        body.category, body.month, body.value, body.note, body.contributed_override
    )
    return {"ok": True, "updated_at": updated_at}


@app.get("/api/budgets")
def api_budgets(month: str = Query(default=None)):
    month = month or _current_month()
    m = insights.compute_metrics(month)
    categories = db.get_categories()

    out = []
    for cat in categories:
        if cat["monthly_cap"] is None:
            continue
        spent = m["by_category"].get(cat["name"], 0.0)
        cap = cat["monthly_cap"]
        pct = (spent / cap * 100) if cap else 0.0
        if pct >= 100:
            status = "OVER"
        elif pct >= 80:
            status = "WATCH"
        else:
            status = "ON TRACK"
        out.append({
            "category": cat["name"],
            "cap": cap,
            "spent": spent,
            "pct": pct,
            "status": status,
            "color": cat["color"],
        })
    return {"month": month, "budgets": out}


@app.get("/api/transactions")
def api_transactions(month: str = Query(default=None), limit: int = Query(default=50)):
    month = month or _current_month()
    txns = db.get_transactions_for_month(month)
    return {"month": month, "transactions": txns[:limit]}


class QuickAddIn(BaseModel):
    message: str


@app.post("/api/transactions/quick-add")
def api_quick_add_transaction(body: QuickAddIn):
    """Log a new transaction straight from the dashboard, parsed exactly the
    same way a Telegram message would be — same regex-first parser, same
    Gemini fallback, just a different source label."""
    message = body.message.strip()
    if not message:
        raise HTTPException(400, "message cannot be empty")
    result = parser.parse_message(message)
    if "error" in result:
        raise HTTPException(400, result["error"])
    txn_id = db.insert_transaction(
        category=result["category"],
        note=result["note"],
        amount=result["amount"],
        raw_message=message,
        source="dashboard",
        guessed=result["guessed"],
        payment_source=result["payment_source"],
        expense_type=result["expense_type"],
        cadence=result["cadence"],
    )
    return {"ok": True, "id": txn_id, **result}


VALID_PAYMENT_SOURCES = {"wallet", "credit", "liquid"}
VALID_EXPENSE_TYPES = {"fixed", "variable", "one-off", "saving"}
VALID_CADENCES = {"daily", "weekly", "monthly", "annual"}


class TransactionUpdate(BaseModel):
    category: Optional[str] = None
    note: Optional[str] = None
    amount: Optional[float] = None
    payment_source: Optional[str] = None
    expense_type: Optional[str] = None
    cadence: Optional[str] = None
    clear_cadence: bool = False
    spent_on: Optional[str] = None


@app.put("/api/transactions/{txn_id}")
def api_update_transaction(txn_id: int, body: TransactionUpdate):
    if not db.get_transaction(txn_id):
        raise HTTPException(404, f"Transaction {txn_id} not found")
    if body.category is not None and body.category not in db.get_category_names():
        raise HTTPException(400, f"Category {body.category!r} not found")
    if body.payment_source is not None and body.payment_source not in VALID_PAYMENT_SOURCES:
        raise HTTPException(400, f"payment_source must be one of {sorted(VALID_PAYMENT_SOURCES)}")
    if body.expense_type is not None and body.expense_type not in VALID_EXPENSE_TYPES:
        raise HTTPException(400, f"expense_type must be one of {sorted(VALID_EXPENSE_TYPES)}")
    if body.cadence is not None and body.cadence not in VALID_CADENCES:
        raise HTTPException(400, f"cadence must be one of {sorted(VALID_CADENCES)}")
    if body.clear_cadence:
        cadence = None
    elif body.cadence is not None:
        cadence = body.cadence
    else:
        cadence = -1  # sentinel: leave unchanged
    db.update_transaction(
        txn_id,
        category=body.category,
        note=body.note,
        amount=body.amount,
        payment_source=body.payment_source,
        expense_type=body.expense_type,
        cadence=cadence,
        spent_on=body.spent_on,
    )
    return {"ok": True}


@app.delete("/api/transactions/{txn_id}")
def api_delete_transaction(txn_id: int):
    if not db.get_transaction(txn_id):
        raise HTTPException(404, f"Transaction {txn_id} not found")
    db.delete_transaction(txn_id)
    return {"ok": True}


# ---- category & keyword management ---------------------------------------

class CategoryIn(BaseModel):
    name: str
    color: str
    expense_type: str
    cadence: Optional[str] = None
    monthly_cap: Optional[float] = None
    parent_category: Optional[str] = None
    target_amount: Optional[float] = None
    target_date: Optional[str] = None
    account_link: Optional[str] = None


class CategoryUpdate(BaseModel):
    color: Optional[str] = None
    expense_type: Optional[str] = None
    cadence: Optional[str] = None
    clear_cadence: bool = False
    monthly_cap: Optional[float] = None
    clear_cap: bool = False
    parent_category: Optional[str] = None
    clear_parent: bool = False
    target_amount: Optional[float] = None
    clear_target_amount: bool = False
    target_date: Optional[str] = None
    clear_target_date: bool = False
    account_link: Optional[str] = None
    clear_account_link: bool = False


class KeywordIn(BaseModel):
    keyword: str


@app.get("/api/categories")
def api_get_categories():
    categories = db.get_categories()
    keywords = db.get_keywords_by_category()
    for cat in categories:
        cat["keywords"] = keywords.get(cat["name"], [])
    return {"categories": categories}


VALID_ACCOUNT_LINKS = {"liquid"}


@app.post("/api/categories")
def api_add_category(body: CategoryIn):
    if body.expense_type not in VALID_EXPENSE_TYPES:
        raise HTTPException(400, f"expense_type must be one of {sorted(VALID_EXPENSE_TYPES)}")
    if body.cadence is not None and body.cadence not in VALID_CADENCES:
        raise HTTPException(400, f"cadence must be one of {sorted(VALID_CADENCES)}")
    if body.name in db.get_category_names():
        raise HTTPException(409, f"Category {body.name!r} already exists")
    if body.parent_category and body.parent_category not in db.get_category_names():
        raise HTTPException(404, f"Parent category {body.parent_category!r} not found")
    if body.account_link is not None:
        if body.account_link not in VALID_ACCOUNT_LINKS:
            raise HTTPException(400, f"account_link must be one of {sorted(VALID_ACCOUNT_LINKS)}")
        if body.expense_type != "saving":
            raise HTTPException(400, "account_link is only valid on a 'saving' category")
    db.add_category(
        body.name, body.color, body.expense_type, cadence=body.cadence, monthly_cap=body.monthly_cap,
        parent_category=body.parent_category,
        target_amount=body.target_amount,
        target_date=body.target_date,
        account_link=body.account_link,
    )
    return {"ok": True}


@app.put("/api/categories/{name}")
def api_update_category(name: str, body: CategoryUpdate):
    if name not in db.get_category_names():
        raise HTTPException(404, f"Category {name!r} not found")
    if body.expense_type is not None and body.expense_type not in VALID_EXPENSE_TYPES:
        raise HTTPException(400, f"expense_type must be one of {sorted(VALID_EXPENSE_TYPES)}")
    if body.cadence is not None and body.cadence not in VALID_CADENCES:
        raise HTTPException(400, f"cadence must be one of {sorted(VALID_CADENCES)}")
    if body.parent_category and body.parent_category not in db.get_category_names():
        raise HTTPException(404, f"Parent category {body.parent_category!r} not found")
    if body.account_link is not None and body.account_link not in VALID_ACCOUNT_LINKS:
        raise HTTPException(400, f"account_link must be one of {sorted(VALID_ACCOUNT_LINKS)}")
    cap = None if body.clear_cap else (body.monthly_cap if body.monthly_cap is not None else -1)
    cadence = None if body.clear_cadence else (body.cadence if body.cadence is not None else -1)
    parent = None if body.clear_parent else (body.parent_category if body.parent_category is not None else -1)
    target_amount = None if body.clear_target_amount else (body.target_amount if body.target_amount is not None else -1)
    target_date = None if body.clear_target_date else (body.target_date if body.target_date is not None else -1)
    account_link = None if body.clear_account_link else (body.account_link if body.account_link is not None else -1)
    db.update_category(
        name, color=body.color, expense_type=body.expense_type, cadence=cadence, monthly_cap=cap,
        parent_category=parent, target_amount=target_amount, target_date=target_date,
        account_link=account_link,
    )
    return {"ok": True}


@app.delete("/api/categories/{name}")
def api_delete_category(name: str):
    if name == "Other":
        raise HTTPException(400, "Can't delete the 'Other' fallback category")
    if name not in db.get_category_names():
        raise HTTPException(404, f"Category {name!r} not found")
    db.delete_category(name)
    return {"ok": True}


@app.post("/api/categories/{name}/keywords")
def api_add_keyword(name: str, body: KeywordIn):
    if name not in db.get_category_names():
        raise HTTPException(404, f"Category {name!r} not found")
    db.add_keyword(body.keyword, name)
    return {"ok": True}


@app.delete("/api/categories/{name}/keywords/{keyword}")
def api_delete_keyword(name: str, keyword: str):
    db.delete_keyword(keyword)
    return {"ok": True}


# ---- settings ---------------------------------------------------------

class SettingsUpdate(BaseModel):
    monthly_salary: Optional[float] = None
    credit_limit: Optional[float] = None


@app.get("/api/settings")
def api_get_settings():
    return {
        "monthly_salary": float(db.get_setting("monthly_salary", 0)),
        "credit_limit": float(db.get_setting("credit_limit", 0)),
        "currency": db.get_setting("currency", "INR"),
    }


@app.put("/api/settings")
def api_update_settings(body: SettingsUpdate):
    if body.monthly_salary is not None:
        db.set_setting("monthly_salary", str(body.monthly_salary))
    if body.credit_limit is not None:
        db.set_setting("credit_limit", str(body.credit_limit))
    return {"ok": True}


@app.get("/api/export")
def api_export():
    """Full-history multi-sheet Excel export (see export.py) — a read-only
    reporting artifact for offline analysis, not used by the dashboard itself."""
    wb = export.build_workbook()
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"cashflow-ledger-{date.today().isoformat()}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


app.mount("/", StaticFiles(directory="static", html=True), name="static")
