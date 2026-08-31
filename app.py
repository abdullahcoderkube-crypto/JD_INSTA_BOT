"""
Main webhook handler + conversation state machine.

Flow:
  NEW
    -> show Login button
  AWAITING_EMAIL (after Login button tapped)
    -> retailer types email
  AWAITING_PASSWORD
    -> retailer types password -> call jdwebnship Login API
       success -> AUTHENTICATED, show Order buttons
       fail    -> back to AWAITING_EMAIL with error message
  AUTHENTICATED
    -> show Place Order (AI) / Fill Manually / Log Out buttons

  === AI flow (ORDER_AI_COLLECT) ===
  ORDER_AI_COLLECT
    -> retailer sends free-form text or a photo with order details
    -> Groq extracts all fields; merges onto existing temp_data
    -> if all required fields present -> ORDER_CONFIRM
    -> if fields still missing -> stay in ORDER_AI_COLLECT, tell retailer what's needed

  === Manual wizard flow (ORDER_<field>) ===
  ORDER_<field>
    -> walks through every REQUIRED_ORDER field one at a time, then optional fields,
       then confirms and calls Order Create.

  ORDER_CONFIRM
    -> retailer confirms or cancels

Run:
    set PAGE_ACCESS_TOKEN=... VERIFY_TOKEN=... APP_SECRET=... FERNET_KEY=...
    set JD_LOGIN_URL=... JD_ORDER_CREATE_URL=... GROQ_API_KEY=...
    python app.py
"""
import hashlib
import hmac
import os

import requests as _requests
from flask import Flask, request

import config
import groq_client
import jdwebnship_client
import messenger_client
import state_manager

app = Flask(__name__)
state_manager.init_db()

# ---------------------------------------------------------------------------
# ngrok tunnel -- started at process boot so we always have a public URL
# ---------------------------------------------------------------------------

NGROK_AUTHTOKEN = os.environ.get("NGROK_AUTHTOKEN", "3Ifp6tJa3CtcgzXN9MGF0h93o2e_41Yrcv1nCrpJacYGbBdVX")

def _start_ngrok(port: int = 5000):
    try:
        from pyngrok import ngrok, conf as ngrok_conf
        ngrok_conf.get_default().auth_token = NGROK_AUTHTOKEN
        tunnel = ngrok.connect(port, "http")
        public_url = tunnel.public_url.replace("http://", "https://")
        print(f"\n{'='*60}", flush=True)
        print(f"  ngrok tunnel active!", flush=True)
        print(f"  Public URL : {public_url}", flush=True)
        print(f"  Webhook URL: {public_url}/webhook", flush=True)
        print(f"{'='*60}\n", flush=True)
        return public_url
    except Exception as exc:
        print(f"[ngrok] Could not start tunnel: {exc}", flush=True)
        return None

# ---------------------------------------------------------------------------
# Order field definitions — matches jdwebnship's /api/bot/direct-order payload
# ---------------------------------------------------------------------------

FIELD_DEFS = {
    "product_name": {"prompt": "What's the product name?", "type": "text"},
    "qty": {"prompt": "How many units?", "type": "text"},
    "price": {"prompt": "What's the price (in ₹)?", "type": "text"},
    "product_weight": {
        "prompt": "What's the product weight category?",
        "type": "choice",
        "options": [("0.5 kg", "0.5"), ("2 kg", "2")],
    },
    "customer_full_name": {"prompt": "What's the customer's full name?", "type": "text"},
    "customer_mobile": {"prompt": "What's the customer's mobile number?", "type": "text"},
    "customer_address": {"prompt": "What's the delivery address?", "type": "text"},
    "pincode": {"prompt": "What's the delivery pincode?", "type": "text"},
    "product_sku": {"prompt": "Product SKU?", "type": "text"},
    "subcategory": {"prompt": "Product subcategory?", "type": "text"},
    "email": {"prompt": "Customer's email?", "type": "text"},
    "customer_alt_number": {"prompt": "Customer's alternate phone number?", "type": "text"},
    "state": {"prompt": "Customer's state?", "type": "text"},
    "city": {"prompt": "Customer's city?", "type": "text"},
}

FIELD_LABELS = {
    "product_name": "Product",
    "qty": "Quantity",
    "price": "Price (₹)",
    "product_weight": "Weight (kg)",
    "payment_method": "Payment method",
    "customer_full_name": "Customer name",
    "customer_mobile": "Mobile",
    "customer_address": "Address",
    "pincode": "Pincode",
    "product_sku": "SKU",
    "subcategory": "Subcategory",
    "email": "Email",
    "customer_alt_number": "Alternate number",
    "state": "State",
    "city": "City",
}

REQUIRED_ORDER = [
    "product_name", "qty", "price", "product_weight",
    "customer_full_name", "customer_mobile", "customer_address", "pincode",
]
OPTIONAL_ORDER = [
    "product_sku", "subcategory", "email", "customer_alt_number", "state", "city",
]


# ---------------------------------------------------------------------------
# Webhook verification (GET) + signature check helper
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == config.VERIFY_TOKEN:
        return challenge, 200
    return "Verification failed", 403


def _valid_signature(req) -> bool:
    if not config.APP_SECRET:
        return True  # skip check only if not configured (dev mode)
    signature = req.headers.get("X-Hub-Signature-256", "")
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(
        config.APP_SECRET.encode(), req.get_data(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature.split("sha256=")[1], expected)


# ---------------------------------------------------------------------------
# Webhook receiver (POST)
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def receive_webhook():
    print("[webhook] Incoming POST received", flush=True)

    if not _valid_signature(request):
        print("[webhook] Rejected: invalid signature", flush=True)
        return "Invalid signature", 403

    body = request.get_json(force=True, silent=True) or {}
    safe_body = str(body).encode("ascii", errors="replace").decode("ascii")
    print(f"[webhook] Payload: {safe_body}", flush=True)

    for entry in body.get("entry", []):
        for event in entry.get("messaging", []):
            psid = event["sender"]["id"]
            if "postback" in event:
                handle_postback(psid, event["postback"].get("payload", ""))
            elif "message" in event:
                msg = event["message"]
                if msg.get("is_echo"):
                    continue  # ignore messages the bot itself sent
                if "quick_reply" in msg:
                    handle_postback(psid, msg["quick_reply"].get("payload", ""))
                else:
                    # Extract image URL if present (works for both Instagram and Messenger)
                    image_url = None
                    for att in msg.get("attachments", []):
                        if att.get("type") == "image":
                            image_url = att.get("payload", {}).get("url")
                            break
                    text = msg.get("text", "").strip()
                    if text or image_url:
                        handle_text(psid, text, image_url=image_url)
    return "OK", 200


# ---------------------------------------------------------------------------
# Order flow helpers
# ---------------------------------------------------------------------------

def send_field_prompt(psid: str, field_key: str):
    field = FIELD_DEFS[field_key]
    if field["type"] == "choice":
        messenger_client.send_quick_replies(
            psid, field["prompt"],
            [{"title": label, "payload": f"ORDER_SET::{field_key}::{value}"}
             for label, value in field["options"]],
        )
    elif field_key in OPTIONAL_ORDER:
        messenger_client.send_quick_replies(
            psid, field["prompt"],
            [{"title": "Skip", "payload": f"ORDER_SKIP::{field_key}"}],
        )
    else:
        messenger_client.send_text(psid, field["prompt"])


def advance_order(psid: str, temp_data: dict, current_field: str):
    if current_field in REQUIRED_ORDER:
        idx = REQUIRED_ORDER.index(current_field)
        if idx + 1 < len(REQUIRED_ORDER):
            next_field = REQUIRED_ORDER[idx + 1]
            state_manager.update_session(psid, state=f"ORDER_{next_field}", temp_data=temp_data)
            send_field_prompt(psid, next_field)
        else:
            state_manager.update_session(psid, state="ORDER_ASK_OPTIONAL", temp_data=temp_data)
            messenger_client.send_quick_replies(
                psid,
                "Want to add optional details (SKU, subcategory, email, "
                "alternate number, state, city)?",
                [
                    {"title": "Yes, add more", "payload": "ORDER_OPTIONAL_YES"},
                    {"title": "No, place order", "payload": "ORDER_OPTIONAL_NO"},
                ],
            )
    elif current_field in OPTIONAL_ORDER:
        idx = OPTIONAL_ORDER.index(current_field)
        if idx + 1 < len(OPTIONAL_ORDER):
            next_field = OPTIONAL_ORDER[idx + 1]
            state_manager.update_session(psid, state=f"ORDER_{next_field}", temp_data=temp_data)
            send_field_prompt(psid, next_field)
        else:
            state_manager.update_session(psid, state="ORDER_CONFIRM", temp_data=temp_data)
            confirm_order(psid, temp_data)


def validate_field(field_key: str, text: str):
    """Returns (is_valid, cleaned_value_or_error_message)."""
    text = text.strip()
    if field_key == "qty":
        if not text.isdigit() or int(text) <= 0:
            return False, "Please enter a valid quantity (a positive whole number)."
        return True, text
    if field_key == "price":
        try:
            float(text)
        except ValueError:
            return False, "Please enter a valid price (numbers only)."
        return True, text
    if field_key == "pincode":
        if not (text.isdigit() and len(text) == 6):
            return False, "Please enter a valid 6-digit pincode."
        return True, text
    if field_key in ("customer_mobile", "customer_alt_number"):
        digits = text.replace(" ", "").replace("-", "")
        if not (digits.isdigit() and len(digits) == 10):
            return False, "Please enter a valid 10-digit mobile number."
        return True, digits
    return True, text


def confirm_order(psid: str, order: dict, language: str = "English"):
    pm = order.get("payment_method", "cod")
    pm_label = "Prepaid" if pm == "prepaid" else "Cash on Delivery (COD)"
    lines = ["Please confirm this order:\n"]
    lines.append(f"{FIELD_LABELS['payment_method']}: {pm_label}")
    for field in REQUIRED_ORDER + OPTIONAL_ORDER:
        if order.get(field):
            lines.append(f"{FIELD_LABELS[field]}: {order[field]}")
    summary = groq_client.localize("\n".join(lines), language)
    messenger_client.send_text(psid, summary)
    messenger_client.send_quick_replies(
        psid,
        "Confirm and place this order?",
        [
            {"title": "Confirm", "payload": "ORDER_CONFIRM_YES"},
            {"title": "Cancel", "payload": "ORDER_CONFIRM_NO"},
        ],
    )


# ---------------------------------------------------------------------------
# Postback (button tap) handling
# ---------------------------------------------------------------------------

def handle_postback(psid: str, payload: str):
    session = state_manager.get_session(psid)

    if payload in ("GET_STARTED",):
        messenger_client.main_menu(psid, authenticated=bool(session["auth_token"]))

    elif payload == "LOGIN":
        state_manager.update_session(psid, state="AWAITING_EMAIL", temp_data={})
        messenger_client.send_text(psid, "Please enter your jdwebnship registered email address:")

    elif payload == "LOGOUT":
        state_manager.clear_auth(psid)
        messenger_client.send_text(psid, "You've been logged out.")
        messenger_client.main_menu(psid, authenticated=False)

    elif payload == "ORDER":
        # AI-assisted free-form order collection
        if not session["auth_token"]:
            messenger_client.send_text(psid, "Please log in first.")
            messenger_client.main_menu(psid, authenticated=False)
            return
        state_manager.update_session(psid, state="ORDER_AI_COLLECT", temp_data={})
        messenger_client.send_text(
            psid,
            "Send me the order details in any language -- product name, price, quantity, "
            "customer name, mobile, address, pincode and payment method (COD/Prepaid). "
            "You can send it all in one message or even send a photo of the order sheet!"
        )

    elif payload == "ORDER_MANUAL":
        # Classic step-by-step wizard
        if not session["auth_token"]:
            messenger_client.send_text(psid, "Please log in first.")
            messenger_client.main_menu(psid, authenticated=False)
            return
        first_field = REQUIRED_ORDER[0]
        state_manager.update_session(
            psid, state=f"ORDER_{first_field}", temp_data={"payment_method": "cod"}
        )
        send_field_prompt(psid, first_field)

    elif payload.startswith("ORDER_SET::"):
        _, field, value = payload.split("::", 2)
        temp = session["temp_data"]
        temp[field] = value
        advance_order(psid, temp, field)

    elif payload.startswith("ORDER_SKIP::"):
        _, field = payload.split("::", 1)
        advance_order(psid, session["temp_data"], field)

    elif payload == "ORDER_OPTIONAL_YES":
        first_optional = OPTIONAL_ORDER[0]
        state_manager.update_session(psid, state=f"ORDER_{first_optional}", temp_data=session["temp_data"])
        send_field_prompt(psid, first_optional)

    elif payload == "ORDER_OPTIONAL_NO":
        state_manager.update_session(psid, state="ORDER_CONFIRM", temp_data=session["temp_data"])
        confirm_order(psid, session["temp_data"])

    elif payload == "ORDER_CONFIRM_YES":
        # Guard against stale replayed messages from Meta retries.
        # Only submit if the session is actually in ORDER_CONFIRM state.
        if session["state"] != "ORDER_CONFIRM":
            print(f"[postback] Ignoring stale ORDER_CONFIRM_YES (state={session['state']})", flush=True)
            return
        submit_order(psid, session)

    elif payload == "ORDER_CONFIRM_NO":
        state_manager.update_session(psid, state="AUTHENTICATED", temp_data={})
        messenger_client.send_text(psid, "Order cancelled.")
        messenger_client.main_menu(psid, authenticated=True)


# ---------------------------------------------------------------------------
# Free-text handling (email, password, order field values)
# ---------------------------------------------------------------------------

def handle_text(psid: str, text: str, image_url: str = None):
    session = state_manager.get_session(psid)
    state = session["state"]

    # ------------------------------------------------------------------
    # Login flow
    # ------------------------------------------------------------------
    if state == "AWAITING_EMAIL":
        temp = session["temp_data"]
        temp["login_email"] = text
        state_manager.update_session(psid, state="AWAITING_PASSWORD", temp_data=temp)
        messenger_client.send_text(psid, "Thanks. Now enter your password:")
        return

    if state == "AWAITING_PASSWORD":
        email = session["temp_data"].get("login_email")
        password = text  # used once below, never stored
        success, data = jdwebnship_client.login(email, password)
        password = None  # drop reference immediately after use

        if success:
            state_manager.update_session(
                psid, state="AUTHENTICATED", auth_token=data["token"], temp_data={}
            )
            messenger_client.send_text(psid, "You're logged in. ✅")
            messenger_client.main_menu(psid, authenticated=True)
        else:
            state_manager.update_session(psid, state="AWAITING_EMAIL", temp_data={})
            messenger_client.send_text(
                psid, f"Login failed: {data.get('error', 'please check your details.')}"
            )
            messenger_client.send_text(psid, "Please re-enter your email address:")
        return

    # ------------------------------------------------------------------
    # AI order collection state
    # ------------------------------------------------------------------
    if state == "ORDER_AI_COLLECT":
        handle_ai_order_input(psid, session, text, image_url)
        return

    # ------------------------------------------------------------------
    # Manual wizard: one field at a time
    # ------------------------------------------------------------------
    if state.startswith("ORDER_") and state not in ("ORDER_ASK_OPTIONAL", "ORDER_CONFIRM", "ORDER_AI_COLLECT"):
        field = state.split("ORDER_", 1)[1]
        if field in FIELD_DEFS:
            if FIELD_DEFS[field]["type"] == "choice":
                messenger_client.send_text(psid, "Please tap one of the buttons above to choose an option.")
                send_field_prompt(psid, field)
                return
            valid, result = validate_field(field, text)
            if not valid:
                messenger_client.send_text(psid, result)
                return
            temp = session["temp_data"]
            temp[field] = result
            advance_order(psid, temp, field)
            return

    # Fallback -- unrecognised input for current state
    messenger_client.main_menu(psid, authenticated=bool(session["auth_token"]))


# ---------------------------------------------------------------------------
# AI order input handler
# ---------------------------------------------------------------------------

def handle_ai_order_input(psid: str, session: dict, text: str, image_url: str = None):
    """Process a free-form message via Groq and update the order draft."""
    messenger_client.send_text(psid, "Got it! Extracting order details...")

    try:
        result = groq_client.extract_order(text, image_url)
    except Exception as exc:
        print(f"[groq] extract_order failed: {exc}", flush=True)
        messenger_client.send_text(
            psid,
            "Sorry, I couldn't process that right now. "
            "Please try again or use 'Fill Manually' from the menu."
        )
        return

    language = result.get("language", "English")

    if not result.get("is_order_request"):
        reply = groq_client.localize(
            "That doesn't look like an order. Please send the product name, price, "
            "customer details and address, or tap 'Fill Manually' to use the step-by-step form.",
            language,
        )
        messenger_client.send_text(psid, reply)
        return

    # Merge newly extracted fields onto the existing draft
    existing = session["temp_data"]
    merged = groq_client.merge_into_temp(existing, result.get("order") or {})
    # Ensure payment_method defaults to cod if Groq couldn't find it
    if not merged.get("payment_method"):
        merged["payment_method"] = "cod"

    missing = result.get("missing_required", [])
    # Re-check missing after merge (Groq might have filled some from earlier messages)
    missing = [f for f in groq_client.REQUIRED_FIELDS if not merged.get(f)]

    print(f"[groq] language={language} merged={merged} missing={missing}", flush=True)

    if missing:
        missing_labels = [groq_client.FIELD_LABELS[f] for f in missing]
        state_manager.update_session(psid, state="ORDER_AI_COLLECT", temp_data=merged)
        needed_str = ", ".join(missing_labels)
        reply = groq_client.localize(
            f"Thanks! I still need the following details:\n{needed_str}\n\nPlease send them.",
            language,
        )
        messenger_client.send_text(psid, reply)
    else:
        # All required fields present -- go straight to confirmation
        state_manager.update_session(psid, state="ORDER_CONFIRM", temp_data=merged)
        confirm_order(psid, merged, language=language)


# ---------------------------------------------------------------------------
# Order submission
# ---------------------------------------------------------------------------

def _to_number(val):
    """Convert a value to int or float. Handles both string and native numeric types
    (Groq returns price/qty as actual numbers, the manual wizard stores them as strings)."""
    if isinstance(val, (int, float)):
        return val
    val = str(val).strip()
    return float(val) if "." in val else int(val)


def submit_order(psid: str, session: dict):
    order = session["temp_data"]

    # Sanity-check: required fields must all be present.
    # Protects against stale sessions or corrupt temp_data.
    REQUIRED_SUBMIT = [
        "product_name", "qty", "price", "product_weight",
        "payment_method", "customer_mobile", "customer_full_name",
        "customer_address", "pincode",
    ]
    missing = [f for f in REQUIRED_SUBMIT if not order.get(f)]
    if missing:
        print(f"[submit_order] Missing fields: {missing} — aborting", flush=True)
        messenger_client.send_text(
            psid,
            "Something went wrong — some order details are missing. "
            "Please start a new order."
        )
        state_manager.update_session(psid, state="AUTHENTICATED", temp_data={})
        messenger_client.main_menu(psid, authenticated=True)
        return

    payload = {
        "product_name": order["product_name"],
        "qty": int(_to_number(order["qty"])),
        "price": _to_number(order["price"]),
        "product_weight": order["product_weight"],
        "payment_method": order["payment_method"],
        "customer_mobile": order["customer_mobile"],
        "customer_full_name": order["customer_full_name"],
        "customer_address": order["customer_address"],
        "pincode": order["pincode"],
    }
    # Optional fields only included if the retailer actually provided them
    for field in OPTIONAL_ORDER:
        if order.get(field):
            payload[field] = order[field]

    print(f"[submit_order] Sending payload: {payload}", flush=True)
    success, data = jdwebnship_client.create_order(session["auth_token"], payload)
    print(f"[submit_order] Result: success={success} data={data}", flush=True)

    if success:
        order_ref = data.get("order_id", "N/A")
        messenger_client.send_text(psid, f"Order placed successfully! Reference: {order_ref} 🎉")
        state_manager.update_session(psid, state="AUTHENTICATED", temp_data={})
        messenger_client.main_menu(psid, authenticated=True)

    elif data.get("error") == "AUTH_EXPIRED":
        messenger_client.send_text(psid, "Your session expired. Please log in again.")
        state_manager.clear_auth(psid)
        messenger_client.main_menu(psid, authenticated=False)

    else:
        messenger_client.send_text(psid, f"Order failed: {data.get('error')}")
        state_manager.update_session(psid, state="AUTHENTICATED", temp_data={})
        messenger_client.main_menu(psid, authenticated=True)


if __name__ == "__main__":
    
    app.run(port=80, debug=False, use_reloader=False)