"""FastAPI app: JSON API for the dashboard + serves the static index.html.
Only the category/keyword/settings management endpoints write to the DB —
transactions themselves are still only ever created by the Telegram bot."""
from datetime import date
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import insights

app = FastAPI(title="Personal Cashflow Ledger")


def _current_month() -> str:
    return date.today().strftime("%Y-%m")


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
        "breakdown": breakdown,
    }


@app.get("/api/insights")
def api_insights(month: str = Query(default=None)):
    month = month or _current_month()
    return {"month": month, "insights": insights.build_insights(month)}


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
    txns = db.get_recurring_transactions()
    total = sum(t["amount"] for t in txns)
    return {"transactions": txns, "total": total}


@app.get("/api/investments")
def api_investments(month: str = Query(default=None), n: int = Query(default=12)):
    """Long-term investment tracking: cumulative SIP/stocks/etc contributions
    (computed automatically from kind='saving' transactions) vs the actual
    portfolio value (manually entered, since we can't fetch real market data)."""
    month = month or _current_month()
    months = _last_n_months(n, month)
    contributed_by_month = {
        c["month"]: c["contributed"] for c in db.get_cumulative_savings_contributions(months)
    }
    snapshots = db.get_investment_snapshots()

    out = [
        {
            "month": m,
            "contributed": contributed_by_month.get(m, 0.0),
            "value": snapshots.get(m, {}).get("value"),
        }
        for m in months
    ]

    latest_value = next((row["value"] for row in reversed(out) if row["value"] is not None), None)
    latest_contributed = out[-1]["contributed"] if out else 0.0
    gain = (latest_value - latest_contributed) if latest_value is not None else None
    gain_pct = (gain / latest_contributed * 100) if gain is not None and latest_contributed else None

    return {
        "months": out,
        "latest_value": latest_value,
        "latest_contributed": latest_contributed,
        "gain": gain,
        "gain_pct": gain_pct,
    }


class InvestmentSnapshotIn(BaseModel):
    month: str
    value: float
    note: Optional[str] = None


@app.post("/api/investments")
def api_add_investment_snapshot(body: InvestmentSnapshotIn):
    db.set_investment_snapshot(body.month, body.value, body.note)
    return {"ok": True}


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


VALID_KINDS = {"essential", "discretionary", "saving"}


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
