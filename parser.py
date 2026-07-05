"""Regex-first message parsing with a Gemini fallback for categorisation.

Logging must cost zero API calls in the common case: only messages whose
category can't be confidently resolved from keywords fall through to Gemini.
"""
import re

import config
import db

AMOUNT_RE = re.compile(r"₹?\s*(\d[\d,]*(?:\.\d+)?)")

_gemini_cache = {}
_gemini_client = None


def get_gemini_client():
    """Shared lazily-created client — also used by ai_insights.py so both
    Gemini call sites (category fallback, daily recap) reuse one instance."""
    global _gemini_client
    if _gemini_client is None and config.GEMINI_API_KEY:
        from google import genai
        _gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _gemini_client


def extract_amount(message):
    match = AMOUNT_RE.search(message)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def extract_category(message):
    """Returns (category, strip_span) or (None, None) if nothing matched.

    An explicit category name mentioned in the message (e.g. "oyo 1500
    travel") always wins over a keyword alias match, and its span is
    stripped from the note since it's redundant with the category field.
    A keyword match (e.g. "gym") is kept in the note — only its span is
    returned as None so strip_note leaves it alone.
    """
    lower = message.lower()

    for name in db.get_category_names():
        match = re.search(rf"\b{re.escape(name.lower())}\b", lower)
        if match:
            return name, match.span()

    for keyword, category in db.get_keywords().items():
        match = re.search(rf"\b{re.escape(keyword)}\b", lower)
        if match:
            return category, None

    return None, None


def extract_payment_source(message):
    """Returns (payment_source, strip_span). Defaults to 'salary' with no
    span when nothing is mentioned explicitly."""
    lower = message.lower()
    for keyword, source in config.PAYMENT_SOURCE_KEYWORDS.items():
        match = re.search(rf"\b{re.escape(keyword)}\b", lower)
        if match:
            return source, match.span()
    return "salary", None


def extract_period(message):
    """Returns (period, strip_span). Defaults to 'monthly' with no span."""
    lower = message.lower()
    for keyword in config.RECURRING_KEYWORDS:
        match = re.search(rf"\b{re.escape(keyword)}\b", lower)
        if match:
            return "yearly", match.span()
    return "monthly", None


def strip_note(message, spans):
    """Remove the given spans (amount, explicit category, payment source,
    recurring tag) from the message to build the free-text note."""
    text = message
    for start, end in sorted((s for s in spans if s), reverse=True):
        text = text[:start] + text[end:]
    text = re.sub(r"₹", "", text)
    return re.sub(r"\s+", " ", text).strip(" -,.")


def classify_with_gemini(note):
    """Ask Gemini for exactly one category name; falls back to 'Other' on
    any error so a flaky API never blocks logging a transaction."""
    if note in _gemini_cache:
        return _gemini_cache[note]

    client = get_gemini_client()
    if client is None:
        return "Other"

    try:
        from google.genai import types

        category_names = db.get_category_names()
        prompt = (
            "Classify this expense note into exactly one of these categories: "
            f"{', '.join(category_names)}.\n"
            f"Note: {note!r}\n"
            "Reply with only the category name, nothing else."
        )
        response = client.models.generate_content(
            model="gemini-3-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0),
        )
        guess = response.text.strip()
        category = guess if guess in category_names else "Other"
    except Exception:
        category = "Other"

    _gemini_cache[note] = category
    return category


def parse_message(message):
    """Returns a dict: {amount, category, note, guessed, payment_source,
    period} or {error: str} if the message has no parseable amount.
    """
    amount = extract_amount(message)
    if amount is None:
        return {"error": "Couldn't find an amount — include a number, e.g. `gym 1500`."}

    amount_match = AMOUNT_RE.search(message)
    category, category_span = extract_category(message)
    payment_source, payment_span = extract_payment_source(message)
    period, period_span = extract_period(message)

    note = strip_note(message, [amount_match.span(), category_span, payment_span, period_span])

    guessed = False
    if category is None:
        category = classify_with_gemini(note or message)
        guessed = True

    return {
        "amount": amount,
        "category": category,
        "note": note,
        "guessed": guessed,
        "payment_source": payment_source,
        "period": period,
    }
