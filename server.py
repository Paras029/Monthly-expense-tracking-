"""FastAPI app: JSON API for the dashboard + serves the static index.html.
Only the category/keyword/settings/transaction-edit endpoints write to the
DB — new transactions are still only ever created by the Telegram bot."""
from datetime import date, datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ai_insights
import db
import insights

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


@app.get("/api/summary")
def api_summary(month: str = Query(default=None)):
    month = month or _current_month()
    m = insights.compute_metrics(month)
    categories = {c["name"]: c for c in db.get_categories()}

    # breakdown feeds the "where the money goes" donut — it excludes
    # kind='saving' categories (e.g. Investments) so contributions don't
    # dominate a chart about spending habits; they get their own Savings chart.
    breakdown = [
        {
            "category": cat,
            "amount": amount,
            "pct": (amount / m["spend_total"] * 100) if m["spend_total"] else 0,
            "color": categories.get(cat, {}).get("color", "#64748b"),
            "kind": categories.get(cat, {}).get("kind", "discretionary"),
        }
        for cat, amount in sorted(m["by_category_spend"].items(), key=lambda kv: -kv[1])
    ]

    days_left = max(m["days_total"] - m["days_elapsed"], 0)
    combined_left = m["salary_left"] + m["credit_left"]
    per_day_left = (combined_left / days_left) if days_left else 0.0
    pace_per_day = (m["total"] / m["days_elapsed"]) if m["days_elapsed"] else 0.0

    # cumulative, all-time balances (through the end of the selected month) —
    # these power the top Savings/Liquid Fund cards, distinct from
    # savings_total/liquid_total which are just *this month's* contribution.
    savings_balance = db.get_cumulative_balance_by_kind("saving", through_month=month)
    liquid_balance = db.get_cumulative_balance_by_kind("liquid", through_month=month)

    return {
        "month": month,
        "spent": m["total"],
        "spend_total": m["spend_total"],
        "projected": m["projected"],
        "pace_per_day": pace_per_day,
        "days_left": days_left,
        "per_day_left": per_day_left,
        "monthly_salary": m["monthly_salary"],
        "salary_used": m["salary_used"],
        "salary_left": m["salary_left"],
        "salary_pct_used": (m["salary_used"] / m["monthly_salary"] * 100) if m["monthly_salary"] else 0,
        "credit_limit": m["credit_limit"],
        "credit_used": m["credit_used"],
        "credit_left": m["credit_left"],
        "credit_pct_used": (m["credit_used"] / m["credit_limit"] * 100) if m["credit_limit"] else 0,
        "top_category": m["top_category"],
        "top_amount": m["top_amount"],
        "top_pct": m["top_pct"],
        "savings_total": m["savings_total"],
        "savings_balance": savings_balance,
        "liquid_total": m["liquid_total"],
        "liquid_balance": liquid_balance,
        "breakdown": breakdown,
    }


@app.get("/api/insights")
def api_insights(month: str = Query(default=None)):
    month = month or _current_month()
    return {"month": month, "insights": insights.build_insights(month)}


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
    if category:
        txns = [t for t in txns if t["category"] == category]
    m = insights.compute_metrics(month)

    daily_totals = {}
    for t in txns:
        day = int(t["spent_on"].split("-")[2])
        daily_totals[day] = daily_totals.get(day, 0.0) + t["amount"]

    cumulative = []
    running = 0.0
    for day in range(1, m["days_total"] + 1):
        running += daily_totals.get(day, 0.0)
        cumulative.append({"day": day, "cumulative": running})

    if category:
        # a single category has no salary/credit-wide target — fall back to
        # its own monthly cap (if one is set) as the pace reference instead.
        cat_row = next((c for c in db.get_categories() if c["name"] == category), None)
        cap = cat_row["monthly_cap"] if cat_row else None
        per_day_budget = (cap / m["days_total"]) if cap and m["days_total"] else None
        budget = cap
    else:
        per_day_budget = (m["budget"] / m["days_total"]) if m["days_total"] else 0.0
        budget = m["budget"]

    return {
        "month": month,
        "category": category,
        "series": cumulative,
        "budget_per_day": per_day_budget,
        "budget": budget,
    }


@app.get("/api/monthly")
def api_monthly(month: str = Query(default=None), category: str = Query(default=None)):
    month = month or _current_month()
    months = _last_n_months(6, month)
    return {"months": db.get_monthly_totals(months, category=category)}


@app.get("/api/savings")
def api_savings(month: str = Query(default=None)):
    month = month or _current_month()
    months = _last_n_months(6, month)
    return {"months": db.get_monthly_totals(months, kind="saving")}


@app.get("/api/recurring")
def api_recurring():
    """Fixed & recurring costs — split by cadence since a monthly-fixed cost
    (rent, a subscription) is re-logged every month (we show only the latest
    instance of each), while yearly cross-cutting ones are rare enough to
    list in full. monthly_equivalent_total lets the dashboard show one
    combined "≈₹X/month locked in" figure."""
    monthly = db.get_fixed_monthly_costs()
    yearly = db.get_yearly_costs()
    monthly_total = sum(t["amount"] for t in monthly)
    yearly_total = sum(t["amount"] for t in yearly)
    return {
        "monthly": monthly,
        "yearly": yearly,
        "monthly_total": monthly_total,
        "yearly_total": yearly_total,
        "monthly_equivalent_total": monthly_total + yearly_total / 12,
    }


@app.get("/api/investments")
def api_investments(month: str = Query(default=None), n: int = Query(default=12)):
    """Long-term investment tracking, per vehicle: each kind='saving' category
    (SIP, a gold plan, FDs, ...) is tracked independently — its own
    automatically-computed cumulative contribution, its own manually-updated
    current value, and how long ago that value was last updated. Also
    returns a combined trend (contributed vs value summed across vehicles,
    forward-filling each vehicle's last known value between updates) for a
    single overview chart."""
    month = month or _current_month()
    months = _last_n_months(n, month)
    saving_categories = [c for c in db.get_categories() if c["kind"] == "saving"]

    combined_contributed = {m: 0.0 for m in months}
    combined_value = {m: 0.0 for m in months}
    combined_value_known = {m: False for m in months}

    vehicles = []
    for cat in saving_categories:
        name = cat["name"]
        contributed_by_month = {
            row["month"]: row["contributed"]
            for row in db.get_cumulative_savings_contributions(months, category=name)
        }
        snapshots = db.get_investment_snapshots(name)
        latest_snapshot = db.get_latest_investment_snapshot(name)

        carry = None
        for m in months:
            if m in snapshots:
                carry = snapshots[m]["value"]
            combined_contributed[m] += contributed_by_month.get(m, 0.0)
            if carry is not None:
                combined_value[m] += carry
                combined_value_known[m] = True

        latest_contributed = contributed_by_month.get(months[-1], 0.0) if months else 0.0
        latest_value = latest_snapshot["value"] if latest_snapshot else None
        updated_at = latest_snapshot["updated_at"] if latest_snapshot else None
        updated_days_ago = None
        if updated_at:
            updated_days_ago = (date.today() - datetime.fromisoformat(updated_at).date()).days
        gain = (latest_value - latest_contributed) if latest_value is not None else None
        gain_pct = (gain / latest_contributed * 100) if gain is not None and latest_contributed else None

        vehicles.append({
            "category": name,
            "color": cat["color"],
            "contributed": latest_contributed,
            "latest_value": latest_value,
            "updated_at": updated_at,
            "updated_days_ago": updated_days_ago,
            "gain": gain,
            "gain_pct": gain_pct,
        })

    vehicles.sort(key=lambda v: -v["contributed"])

    combined_months = [
        {
            "month": m,
            "contributed": combined_contributed[m],
            "value": combined_value[m] if combined_value_known[m] else None,
        }
        for m in months
    ]
    total_contributed = combined_contributed[months[-1]] if months else 0.0
    total_value = combined_value[months[-1]] if months and combined_value_known[months[-1]] else None
    total_gain = (total_value - total_contributed) if total_value is not None else None
    total_gain_pct = (total_gain / total_contributed * 100) if total_gain is not None and total_contributed else None

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


@app.post("/api/investments")
def api_add_investment_snapshot(body: InvestmentSnapshotIn):
    categories = {c["name"]: c for c in db.get_categories()}
    if body.category not in categories:
        raise HTTPException(404, f"Category {body.category!r} not found")
    if categories[body.category]["kind"] != "saving":
        raise HTTPException(400, f"{body.category!r} is not a 'saving' category")
    updated_at = db.set_investment_snapshot(body.category, body.month, body.value, body.note)
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


class TransactionUpdate(BaseModel):
    category: Optional[str] = None
    note: Optional[str] = None
    amount: Optional[float] = None
    payment_source: Optional[str] = None
    recurrence: Optional[str] = None
    spent_on: Optional[str] = None


@app.put("/api/transactions/{txn_id}")
def api_update_transaction(txn_id: int, body: TransactionUpdate):
    if not db.get_transaction(txn_id):
        raise HTTPException(404, f"Transaction {txn_id} not found")
    if body.category is not None and body.category not in db.get_category_names():
        raise HTTPException(400, f"Category {body.category!r} not found")
    if body.payment_source is not None and body.payment_source not in {"salary", "credit"}:
        raise HTTPException(400, "payment_source must be 'salary' or 'credit'")
    if body.recurrence is not None and body.recurrence not in {"one-off", "monthly", "yearly"}:
        raise HTTPException(400, "recurrence must be 'one-off', 'monthly', or 'yearly'")
    db.update_transaction(
        txn_id,
        category=body.category,
        note=body.note,
        amount=body.amount,
        payment_source=body.payment_source,
        recurrence=body.recurrence,
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
    kind: str
    monthly_cap: Optional[float] = None


class CategoryUpdate(BaseModel):
    color: Optional[str] = None
    kind: Optional[str] = None
    monthly_cap: Optional[float] = None
    clear_cap: bool = False


class KeywordIn(BaseModel):
    keyword: str


VALID_KINDS = {"essential", "discretionary", "saving", "liquid"}


@app.get("/api/categories")
def api_get_categories():
    categories = db.get_categories()
    keywords = db.get_keywords_by_category()
    for cat in categories:
        cat["keywords"] = keywords.get(cat["name"], [])
    return {"categories": categories}


@app.post("/api/categories")
def api_add_category(body: CategoryIn):
    if body.kind not in VALID_KINDS:
        raise HTTPException(400, f"kind must be one of {sorted(VALID_KINDS)}")
    if body.name in db.get_category_names():
        raise HTTPException(409, f"Category {body.name!r} already exists")
    db.add_category(body.name, body.color, body.kind, body.monthly_cap)
    return {"ok": True}


@app.put("/api/categories/{name}")
def api_update_category(name: str, body: CategoryUpdate):
    if name not in db.get_category_names():
        raise HTTPException(404, f"Category {name!r} not found")
    if body.kind is not None and body.kind not in VALID_KINDS:
        raise HTTPException(400, f"kind must be one of {sorted(VALID_KINDS)}")
    cap = None if body.clear_cap else (body.monthly_cap if body.monthly_cap is not None else -1)
    db.update_category(name, color=body.color, kind=body.kind, monthly_cap=cap)
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


app.mount("/", StaticFiles(directory="static", html=True), name="static")
