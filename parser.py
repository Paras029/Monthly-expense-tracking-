"""Regex-first message parsing with a Gemini fallback for categorisation.

Logging must cost zero API calls in the common case: only messages whose
category can't be confidently resolved from keywords fall through to Gemini.
"""
import re

import requests

import config
import db

AMOUNT_RE = re.compile(r"₹?\s*(\d[\d,]*(?:\.\d+)?)")

_gemini_cache = {}

GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"


def call_gemini(prompt, temperature=0.0):
    """Plain REST call to the Gemini API — deliberately not the official
    google-genai SDK. That SDK pulls in google-auth -> cryptography, a
    package with compiled Rust native code; on Termux the PyPI wheel for
    cryptography is built for glibc and fails to dlopen against Android's
    Bionic libc. We only need API-key auth (no OAuth/JWT), so a bare HTTPS
    POST via `requests` avoids that whole native-dependency chain — also
    used by ai_insights.py, the app's other Gemini call site.

    Returns the response text, or None if no API key is configured. Raises
    on any HTTP/parsing error — callers are expected to catch and fall back.
    """
    if not config.GEMINI_API_KEY:
        return None

    resp = requests.post(
        GEMINI_URL,
        headers={"x-goog-api-key": config.GEMINI_API_KEY, "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature},
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


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

    category_names = db.get_category_names()
    try:
        prompt = (
            "Classify this expense note into exactly one of these categories: "
            f"{', '.join(category_names)}.\n"
            f"Note: {note!r}\n"
            "Reply with only the category name, nothing else."
        )
        guess = call_gemini(prompt, temperature=0.0)
        category = guess.strip() if guess and guess.strip() in category_names else "Other"
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
