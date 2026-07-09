"""Regex-first message parsing with a Gemini fallback for categorisation.

Logging must cost zero API calls in the common case: only messages whose
category can't be confidently resolved from keywords fall through to Gemini.
"""
import re
import time

import requests

import config
import db

AMOUNT_RE = re.compile(r"₹?\s*(\d[\d,]*(?:\.\d+)?)")

_gemini_cache = {}

GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# HTTP statuses worth a short retry: transient overload/rate-limit on
# Google's side, not something wrong with the request itself.
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_RETRY_DELAYS = (1, 2)  # seconds, between the 3 attempts


def call_gemini(prompt, temperature=0.0, contents=None, system_instruction=None):
    """Plain REST call to the Gemini API — deliberately not the official
    google-genai SDK. That SDK pulls in google-auth -> cryptography, a
    package with compiled Rust native code; on Termux the PyPI wheel for
    cryptography is built for glibc and fails to dlopen against Android's
    Bionic libc. We only need API-key auth (no OAuth/JWT), so a bare HTTPS
    POST via `requests` avoids that whole native-dependency chain — also
    used by ai_insights.py and chat.py, the app's other Gemini call sites.

    Pass a plain string `prompt` for the common single-turn case (wrapped
    automatically), or a pre-built multi-turn `contents` list (`[{"role":
    "user"|"model", "parts": [{"text": ...}]}, ...]`) for chat.py's
    conversation history — `prompt` is ignored when `contents` is given.
    `system_instruction` (plain string, optional) goes in the dedicated
    `systemInstruction` request field rather than being embedded as a fake
    first conversation turn — the API's documented mechanism for this, and
    lighter than duplicating a data blob inside `contents`.

    Retries up to twice more (3 attempts total, short backoff) on a
    transient 429/5xx from Google's side, or on a network-level timeout/
    connection error — chat's larger multi-turn payload takes longer to
    generate than a short classification/recap prompt, so it's more likely
    to land during a brief overload window or just run past the read
    timeout; neither should surface as a hard failure on the first try.

    Returns the response text, or None if no API key is configured. Raises
    on any HTTP/parsing error — callers are expected to catch and fall back.
    """
    if not config.GEMINI_API_KEY:
        return None

    payload_contents = contents if contents is not None else [{"parts": [{"text": prompt}]}]
    payload = {
        "contents": payload_contents,
        "generationConfig": {
            "temperature": temperature,
            # Gemini 3.x models default to 'medium' thinking effort, which adds
            # meaningful latency for no benefit on these simple classification/
            # chat/recap tasks — 'minimal' (the lowest level gemini-3.5-flash
            # supports) keeps calls as fast as possible. Ignored harmlessly by
            # older (2.x) model families that don't support it.
            "thinkingConfig": {"thinkingLevel": "minimal"},
        },
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
    headers = {"x-goog-api-key": config.GEMINI_API_KEY, "Content-Type": "application/json"}

    resp = None
    last_exc = None
    for delay in (*_RETRY_DELAYS, None):
        is_last_attempt = delay is None
        try:
            # 60s read timeout: chat's larger multi-turn payload (system
            # prompt + data snapshot + history) takes noticeably longer to
            # generate than the short single-turn classify/recap prompts,
            # and 30s wasn't always enough even at thinkingLevel=low.
            resp = requests.post(GEMINI_URL, headers=headers, json=payload, timeout=60)
        except requests.exceptions.RequestException as e:
            last_exc = e
            resp = None
            if is_last_attempt:
                break
            time.sleep(delay)
            continue

        if resp.status_code in _RETRYABLE_STATUSES and not is_last_attempt:
            time.sleep(delay)
            continue
        break

    if resp is None:
        raise last_exc
    resp.raise_for_status()

    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        # e.g. blocked by a safety filter — data still has a
        # promptFeedback/finishReason worth surfacing instead of a bare KeyError
        raise ValueError(f"Gemini returned no candidates: {data.get('promptFeedback', data)}")
    return candidates[0]["content"]["parts"][0]["text"]


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
    """Returns (payment_source, strip_span). Defaults to 'wallet' with no
    span when nothing is mentioned explicitly."""
    lower = message.lower()
    for keyword, source in config.PAYMENT_SOURCE_KEYWORDS.items():
        match = re.search(rf"\b{re.escape(keyword)}\b", lower)
        if match:
            return source, match.span()
    return "wallet", None


def extract_expense_type(message):
    """Returns (expense_type, cadence, strip_span) from an explicit keyword
    in the message, or (None, None, None) if nothing matched — the caller
    then falls back to the resolved category's own default expense_type/
    cadence (see parse_message). 'yearly'/'annual'/'annually' and 'fixed'/
    'recurring'/'subscription' set both expense_type='fixed' and a cadence
    together, mirroring the old recurrence keyword semantics."""
    lower = message.lower()
    for keyword in config.YEARLY_KEYWORDS:
        match = re.search(rf"\b{re.escape(keyword)}\b", lower)
        if match:
            return "fixed", "annual", match.span()
    for keyword, (expense_type, cadence) in config.FIXED_CADENCE_KEYWORDS.items():
        match = re.search(rf"\b{re.escape(keyword)}\b", lower)
        if match:
            return expense_type, cadence, match.span()
    for keyword, expense_type in config.EXPENSE_TYPE_KEYWORDS.items():
        match = re.search(rf"\b{re.escape(keyword)}\b", lower)
        if match:
            return expense_type, None, match.span()
    for keyword, cadence in config.CADENCE_ONLY_KEYWORDS.items():
        match = re.search(rf"\b{re.escape(keyword)}\b", lower)
        if match:
            return None, cadence, match.span()
    return None, None, None


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
    any error so a flaky API never blocks logging a transaction.

    The category list and their example keywords are pulled fresh from the
    DB on every call (not the config.py defaults), so categories the user
    added or renamed via the dashboard's Categories panel are classified
    correctly too — including custom ones (e.g. "Gold Plan") whose name
    alone wouldn't hint at what belongs there without its keywords.
    """
    if note in _gemini_cache:
        return _gemini_cache[note]

    category_names = db.get_category_names()
    keywords_by_category = db.get_keywords_by_category()
    try:
        category_hints = "\n".join(
            f"- {name}" + (
                f" (examples: {', '.join(keywords_by_category[name][:6])})"
                if keywords_by_category.get(name) else ""
            )
            for name in category_names
        )
        prompt = (
            "Classify this personal expense note into exactly one of the categories "
            "below. These categories were defined by the user and may include custom "
            "ones beyond common names — use each category's example keywords as a "
            "hint for what belongs in it.\n\n"
            f"{category_hints}\n\n"
            f"Note: {note!r}\n"
            "Reply with only the category name from the list above, nothing else."
        )
        guess = call_gemini(prompt, temperature=0.0)
        category = guess.strip() if guess and guess.strip() in category_names else "Other"
    except Exception:
        category = "Other"

    _gemini_cache[note] = category
    return category


def parse_message(message):
    """Returns a dict: {amount, category, note, guessed, payment_source,
    expense_type, cadence} or {error: str} if the message has no parseable
    amount.
    """
    amount = extract_amount(message)
    if amount is None:
        return {"error": "Couldn't find an amount — include a number, e.g. `gym 1500`."}

    amount_match = AMOUNT_RE.search(message)
    category, category_span = extract_category(message)
    payment_source, payment_span = extract_payment_source(message)
    tag_expense_type, tag_cadence, tag_span = extract_expense_type(message)

    note = strip_note(message, [amount_match.span(), category_span, payment_span, tag_span])

    guessed = False
    if category is None:
        category = classify_with_gemini(note or message)
        guessed = True

    cat_row = db.get_category(category) or {}
    cat_expense_type = cat_row.get("expense_type") or "variable"
    cat_cadence = cat_row.get("cadence")

    if tag_expense_type is None:
        # no explicit expense_type keyword in the message -> use the
        # category's own default (set once via the Categories panel).
        expense_type = cat_expense_type
    elif tag_expense_type == "fixed" and cat_expense_type == "saving":
        # a bare cadence keyword ("sip 5000 yearly") on an already-saving
        # category sets cadence only — it shouldn't downgrade a SIP away
        # from being a saving vehicle just because it also recurs annually.
        expense_type = "saving"
    else:
        expense_type = tag_expense_type

    cadence = tag_cadence if tag_cadence is not None else cat_cadence

    return {
        "amount": amount,
        "category": category,
        "note": note,
        "guessed": guessed,
        "payment_source": payment_source,
        "expense_type": expense_type,
        "cadence": cadence,
    }
