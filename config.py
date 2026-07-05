"""Environment config, default categories, and keyword aliases."""
import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_TELEGRAM_USER_ID = os.getenv("ALLOWED_TELEGRAM_USER_ID", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

MONTHLY_SALARY = float(os.getenv("MONTHLY_SALARY", "60000"))
CREDIT_LIMIT = float(os.getenv("CREDIT_LIMIT", "0"))
CURRENCY = os.getenv("CURRENCY", "INR")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")

DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8000"))

DB_PATH = os.getenv("DB_PATH", "ledger.db")

# name -> (color, kind, monthly_cap)
DEFAULT_CATEGORIES = {
    "Food":        {"color": "#f59e0b", "kind": "discretionary", "monthly_cap": None},
    "Groceries":   {"color": "#22c55e", "kind": "essential",     "monthly_cap": None},
    "Bills":       {"color": "#3b82f6", "kind": "essential",     "monthly_cap": None},
    "Luxuries":    {"color": "#a855f7", "kind": "discretionary", "monthly_cap": None},
    "Health":      {"color": "#ef4444", "kind": "essential",     "monthly_cap": None},
    "Travel":      {"color": "#06b6d4", "kind": "discretionary", "monthly_cap": None},
    "Investments": {"color": "#eab308", "kind": "saving",        "monthly_cap": None},
    "Other":       {"color": "#64748b", "kind": "discretionary", "monthly_cap": None},
}

# category -> keyword list, used for regex-first classification
CATEGORY_KEYWORDS = {
    "Food": [
        "zomato", "swiggy", "lunch", "dinner", "breakfast", "cafe", "coffee",
        "restaurant", "food", "snack", "dominos", "pizza", "starbucks",
    ],
    "Groceries": [
        "dmart", "bigbasket", "blinkit", "grocery", "groceries", "zepto",
        "vegetables", "milk", "kirana",
    ],
    "Bills": [
        "electricity", "rent", "wifi", "recharge", "broadband", "gas bill",
        "water bill", "phone bill", "bill", "emi",
    ],
    "Luxuries": [
        "gym", "netflix", "spotify", "shopping", "amazon", "myntra",
        "prime video", "hotstar", "movie", "concert",
    ],
    "Health": [
        "pharmeasy", "medicine", "doctor", "apollo", "hospital", "pharmacy",
        "clinic", "medical",
    ],
    "Travel": [
        "ola", "uber", "metro", "flight", "fuel", "petrol", "diesel",
        "cab", "train", "irctc", "oyo", "hotel",
    ],
    "Investments": [
        "sip", "index fund", "stocks", "mutual fund", "mf", "nps", "ppf",
    ],
    "Other": [],
}

CATEGORY_NAMES = list(DEFAULT_CATEGORIES.keys())

# trailing keyword in a message -> payment_source. Default (no match) is 'salary'.
PAYMENT_SOURCE_KEYWORDS = {
    "credit": "credit",
    "card": "credit",
    "cc": "credit",
    "salary": "salary",
    "cash": "salary",
}

# trailing keyword in a message -> period. Default (no match) is 'monthly'.
RECURRING_KEYWORDS = ["yearly", "annual", "annually", "recurring"]
