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

# name -> (color, expense_type, cadence, monthly_cap). expense_type is one of
# 'fixed' | 'variable' | 'one-off' | 'saving' — the default tag for new
# transactions logged into this category (each transaction can still
# override it explicitly, e.g. tagging one Travel expense 'one-off').
DEFAULT_CATEGORIES = {
    "Food":        {"color": "#f59e0b", "expense_type": "variable", "cadence": None,      "monthly_cap": None},
    "Groceries":   {"color": "#22c55e", "expense_type": "variable", "cadence": None,      "monthly_cap": None},
    "Bills":       {"color": "#3b82f6", "expense_type": "fixed",    "cadence": "monthly", "monthly_cap": None},
    "Luxuries":    {"color": "#a855f7", "expense_type": "variable", "cadence": None,      "monthly_cap": None},
    "Health":      {"color": "#ef4444", "expense_type": "variable", "cadence": None,      "monthly_cap": None},
    "Travel":      {"color": "#06b6d4", "expense_type": "variable", "cadence": None,      "monthly_cap": None},
    "Other":       {"color": "#64748b", "expense_type": "variable", "cadence": None,      "monthly_cap": None},
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
    "Other": [],
}

CATEGORY_NAMES = list(DEFAULT_CATEGORIES.keys())

# trailing keyword in a message -> payment_source. Default (no match) is 'wallet'.
PAYMENT_SOURCE_KEYWORDS = {
    "credit": "credit",
    "card": "credit",
    "cc": "credit",
    "wallet": "wallet",
    "salary": "wallet",
    "cash": "wallet",
    "liquid": "liquid",
    "emergency fund": "liquid",
}

# trailing keyword in a message -> expense_type. Explicit keywords override
# whatever the category's own default expense_type is (e.g. tagging one
# Travel expense 'oneoff' even though Travel usually defaults to 'variable').
# No match -> falls back to the category's default expense_type.
EXPENSE_TYPE_KEYWORDS = {
    "oneoff": "one-off",
    "one-off": "one-off",
    "one off": "one-off",
    "anomaly": "one-off",
    "variable": "variable",
    "saving": "saving",
    "investment": "saving",
    # 'fixed'/'recurring'/'subscription' are handled together with cadence
    # below, since old behaviour tied them to an implied monthly cadence.
}

# trailing keyword in a message -> (expense_type, cadence) — these set BOTH
# together, mirroring the old recurrence semantics: 'fixed'/'recurring'/
# 'subscription' meant "a locked-in cost, recurring monthly"; 'yearly'/
# 'annual'/'annually' meant the same but annually. A category that's already
# expense_type='saving' (e.g. a SIP) keeps its type — the keyword here only
# ever sets the cadence for it, never downgrades it away from 'saving'.
FIXED_CADENCE_KEYWORDS = {
    "fixed": ("fixed", "monthly"),
    "recurring": ("fixed", "monthly"),
    "subscription": ("fixed", "monthly"),
}
YEARLY_KEYWORDS = ["yearly", "annual", "annually"]

# trailing keyword in a message -> cadence only (expense_type untouched).
CADENCE_ONLY_KEYWORDS = {
    "daily": "daily",
    "weekly": "weekly",
}
