"""FastAPI app: JSON API for the dashboard + serves the static index.html.
Read-only against the SQLite ledger — never writes a transaction."""
from datetime import date

from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles

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

    breakdown = [
        {
            "category": cat,
            "amount": amount,
            "pct": (amount / m["total"] * 100) if m["total"] else 0,
            "color": categories.get(cat, {}).get("color", "#64748b"),
        }
        for cat, amount in sorted(m["by_category"].items(), key=lambda kv: -kv[1])
    ]

    days_left = max(m["days_total"] - m["days_elapsed"], 0)
    budget_left = m["budget"] - m["total"]
    per_day_left = (budget_left / days_left) if days_left else 0.0
    pace_per_day = (m["total"] / m["days_elapsed"]) if m["days_elapsed"] else 0.0

    return {
        "month": month,
        "spent": m["total"],
        "budget": m["budget"],
        "pct_used": (m["total"] / m["budget"] * 100) if m["budget"] else 0,
        "budget_left": budget_left,
        "per_day_left": per_day_left,
        "days_left": days_left,
        "top_category": m["top_category"],
        "top_amount": m["top_amount"],
        "top_pct": m["top_pct"],
        "projected": m["projected"],
        "pace_per_day": pace_per_day,
        "breakdown": breakdown,
    }


@app.get("/api/insights")
def api_insights(month: str = Query(default=None)):
    month = month or _current_month()
    return {"month": month, "insights": insights.build_insights(month)}


@app.get("/api/daily-burn")
def api_daily_burn(month: str = Query(default=None)):
    month = month or _current_month()
    txns = db.get_transactions_for_month(month)
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

    per_day_budget = (m["budget"] / m["days_total"]) if m["days_total"] else 0.0

    return {
        "month": month,
        "series": cumulative,
        "budget_per_day": per_day_budget,
        "budget": m["budget"],
    }


@app.get("/api/monthly")
def api_monthly(month: str = Query(default=None)):
    month = month or _current_month()
    months = _last_n_months(6, month)
    return {"months": db.get_monthly_totals(months)}


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


app.mount("/", StaticFiles(directory="static", html=True), name="static")
