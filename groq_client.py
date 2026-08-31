"""
Groq AI order extraction client.

Uses Groq's OpenAI-compatible API to extract structured order data from
free-form retailer messages (text and/or images).

The extractor never acts as a chatbot -- it either returns structured JSON
with order fields, or signals that the message is not an order request.

Usage:
    import groq_client
    result = groq_client.extract_order(
        "Blue T-Shirt SKU-001 qty 2 price 499 "
        "Rahul 9876543210 123 MG Road Bangalore 560001 Karnataka COD"
    )
    # result["is_order_request"] == True
    # result["missing_required"]  == []   (all fields found)
"""

import base64
import json
import re
import time

import httpx

import config

# ---------------------------------------------------------------------------
# Field definitions (mirrors REQUIRED_ORDER / OPTIONAL_ORDER in app.py)
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = [
    "product_name", "qty", "price", "product_weight",
    "customer_full_name", "customer_mobile", "customer_address", "pincode",
    "payment_method",
]
OPTIONAL_FIELDS = [
    "product_sku", "subcategory", "email",
    "customer_alt_number", "state", "city",
]
ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS

FIELD_LABELS = {
    "product_name":        "Product name",
    "qty":                 "Quantity",
    "price":               "Price (Rs)",
    "product_weight":      "Weight (0.5 or 2 kg)",
    "payment_method":      "Payment method (COD/Prepaid)",
    "customer_full_name":  "Customer name",
    "customer_mobile":     "Mobile number",
    "customer_address":    "Delivery address",
    "pincode":             "Pincode",
    "product_sku":         "SKU",
    "subcategory":         "Subcategory",
    "email":               "Email",
    "customer_alt_number": "Alt number",
    "state":               "State",
    "city":                "City",
}

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a strict ecommerce order-data extractor for an Indian retailer bot.\n\n"
    "Your ONLY job is to extract order fields from the user message and return JSON.\n"
    "You MUST NOT respond conversationally, offer advice, or do anything else.\n\n"
    "Output format (valid JSON only, no markdown, no extra text):\n"
    "{\n"
    '  "language": "<detected language: English/Hindi/Hinglish/Gujarati/Marathi>",\n'
    '  "is_order_request": <true if message has order/product/customer info, false otherwise>,\n'
    '  "order": {\n'
    '    "product_name": "<string or null>",\n'
    '    "product_sku": "<string or null>",\n'
    '    "subcategory": "<string or null>",\n'
    '    "qty": <integer or null>,\n'
    '    "price": <number or null>,\n'
    '    "product_weight": "<\'0.5\' or \'2\' ONLY, or null>",\n'
    '    "payment_method": "<\'cod\' or \'prepaid\' ONLY, or null>",\n'
    '    "customer_full_name": "<string or null>",\n'
    '    "customer_mobile": "<10-digit string or null>",\n'
    '    "email": "<string or null>",\n'
    '    "customer_alt_number": "<10-digit string or null>",\n'
    '    "customer_address": "<string or null>",\n'
    '    "pincode": "<6-digit string or null>",\n'
    '    "state": "<string or null>",\n'
    '    "city": "<string or null>"\n'
    "  }\n"
    "}\n\n"
    "Rules:\n"
    "- Set is_order_request=false for greetings, jokes, or anything unrelated to orders.\n"
    "- product_weight: '500g'/'half kg'/'0.5 kg' -> '0.5'; '1 kg'/'2 kg'/'heavy' -> '2'; else null.\n"
    "- payment_method: 'cod'/'cash on delivery'/'naqad' -> 'cod'; 'prepaid'/'online'/'UPI'/'paid' -> 'prepaid'; else null.\n"
    "- qty: integer only. '2 pieces' -> 2.\n"
    "- price: number only. 'Rs499'/'499 rupees'/'499/-' -> 499.\n"
    "- customer_mobile / customer_alt_number: 10 digits, strip +91 country code.\n"
    "- pincode: exactly 6 digits.\n"
    "- Set field to null if not confidently extractable. Never guess or hallucinate values.\n"
    "- Always include ALL fields in the order object (null if not found).\n"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_order(text: str, image_url: str = None) -> dict:
    """
    Extract order fields from free-form text and/or an image URL.

    Returns a dict with:
        language         (str)  -- detected language
        is_order_request (bool) -- True if the message looks like an order
        order            (dict) -- all order fields; None where not found
        missing_required (list) -- required field keys still missing
        missing_labels   (list) -- human-readable names of missing required fields
    """
    messages = _build_messages(text, image_url)
    raw = _call_groq(messages)
    result = _parse_response(raw)

    if result.get("is_order_request"):
        order = result.get("order") or {}
        missing = [f for f in REQUIRED_FIELDS if not order.get(f)]
        result["missing_required"] = missing
        result["missing_labels"] = [FIELD_LABELS[f] for f in missing]
    else:
        result["missing_required"] = []
        result["missing_labels"] = []
        if "order" not in result:
            result["order"] = {f: None for f in ALL_FIELDS}

    return result


def localize(text: str, language: str) -> str:
    """
    Translate a bot reply into the user's language using Roman/Latin script only
    (e.g. Hinglish instead of Devanagari). Returns original text on any error.
    """
    if not language or language.lower() in ("english", "en"):
        return text
    if not config.GROQ_API_KEY:
        return text

    system = (
        "You are a translator. Translate the given text into the specified language. "
        "CRITICAL: Use ONLY Latin/Roman characters (no Devanagari, no Gujarati script, etc.). "
        "For Hindi/Marathi/Gujarati use romanized spelling (Hinglish style). "
        "Output ONLY the translated text, nothing else."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Language: {language}\nText: {text}"},
    ]
    try:
        return _call_groq(messages, json_mode=False, max_tokens=500).strip()
    except Exception as exc:
        print(f"[groq_client] localize failed: {exc}", flush=True)
        return text


def merge_into_temp(existing: dict, extracted_order: dict) -> dict:
    """Merge non-null extracted fields on top of existing temp_data dict."""
    merged = dict(existing)
    for field, value in extracted_order.items():
        if value is not None and field in ALL_FIELDS:
            merged[field] = value
    return merged


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_messages(text: str, image_url: str = None) -> list:
    user_content = []

    if image_url:
        try:
            img_resp = httpx.get(image_url, timeout=15, follow_redirects=True)
            img_resp.raise_for_status()
            ctype = img_resp.headers.get("content-type", "image/jpeg").split(";")[0]
            b64 = base64.b64encode(img_resp.content).decode()
            user_content.append({
                "type": "image_url",
                "image_url": {"url": "data:" + ctype + ";base64," + b64},
            })
        except Exception as exc:
            print(f"[groq_client] Image fetch failed: {exc}", flush=True)

    user_content.append({"type": "text", "text": text or "(no text provided)"})

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _call_groq(messages: list, json_mode: bool = True, max_tokens: int = None) -> str:
    """POST to Groq API and return the raw content string from the first choice."""
    if not config.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not configured.")

    payload = {
        "model": config.GROQ_MODEL,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens or config.GROQ_MAX_TOKENS,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": "Bearer " + config.GROQ_API_KEY,
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=config.GROQ_TIMEOUT) as client:
        resp = client.post(
            config.GROQ_BASE_URL + "/chat/completions",
            json=payload,
            headers=headers,
        )
        # Handle rate limit: wait then retry once
        if resp.status_code == 429:
            wait = int(resp.headers.get("retry-after", "5"))
            print(f"[groq_client] Rate limited -- retrying after {wait}s", flush=True)
            time.sleep(wait)
            resp = client.post(
                config.GROQ_BASE_URL + "/chat/completions",
                json=payload,
                headers=headers,
            )
        resp.raise_for_status()

    return resp.json()["choices"][0]["message"]["content"]


def _parse_response(raw: str) -> dict:
    """Parse Groq's JSON output; strips markdown code fences if present."""
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    try:
        return json.loads(clean)
    except json.JSONDecodeError as exc:
        print(f"[groq_client] JSON parse error: {exc} | raw: {raw[:300]}", flush=True)
        return {
            "language": "English",
            "is_order_request": False,
            "order": {f: None for f in ALL_FIELDS},
        }
