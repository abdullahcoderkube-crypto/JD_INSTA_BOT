"""
Main webhook handler + conversation state machine.

Flow:
  NEW
    -> show Login button
  AWAITING_EMAIL (after Login button tapped)
    -> retailer types email
  AWAITING_PASSWORD
    -> retailer types password -> call jdwebnship Login API
       success -> AUTHENTICATED, show Order button
       fail    -> back to AWAITING_EMAIL with error message
  AUTHENTICATED
    -> show Order / Log Out buttons
  ORDER_<field>
    -> walks through every REQUIRED_ORDER field one at a time (text prompts,
       or quick-reply buttons for constrained fields like product_weight and
       payment_method), then asks whether to add optional details, then
       walks OPTIONAL_ORDER fields (each skippable), then confirms and
       calls Order Create.

Run:
    export PAGE_ACCESS_TOKEN=... VERIFY_TOKEN=... APP_SECRET=... FERNET_KEY=...
    export JD_LOGIN_URL=... JD_ORDER_CREATE_URL=...
    python app.py
"""
import hashlib
import hmac

from flask import Flask, request

import config
import jdwebnship_client
import messenger_client
import state_manager

app = Flask(__name__)
state_manager.init_db()

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
    print(f"[webhook] Payload: {body}", flush=True)

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
                elif "text" in msg:
                    handle_text(psid, msg["text"].strip())
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


def confirm_order(psid: str, order: dict):
    lines = ["Please confirm this order:\n"]
    lines.append(f"{FIELD_LABELS['payment_method']}: Cash on Delivery (COD)")
    for field in REQUIRED_ORDER + OPTIONAL_ORDER:
        if order.get(field):
            lines.append(f"{FIELD_LABELS[field]}: {order[field]}")
    messenger_client.send_text(psid, "\n".join(lines))
    messenger_client.send_quick_replies(
        psid,
        "Confirm and place this order?",
        [
            {"title": "✅ Confirm", "payload": "ORDER_CONFIRM_YES"},
            {"title": "❌ Cancel", "payload": "ORDER_CONFIRM_NO"},
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
        submit_order(psid, session)

    elif payload == "ORDER_CONFIRM_NO":
        state_manager.update_session(psid, state="AUTHENTICATED", temp_data={})
        messenger_client.send_text(psid, "Order cancelled.")
        messenger_client.main_menu(psid, authenticated=True)


# ---------------------------------------------------------------------------
# Free-text handling (email, password, order field values)
# ---------------------------------------------------------------------------

def handle_text(psid: str, text: str):
    session = state_manager.get_session(psid)
    state = session["state"]

    if state == "AWAITING_EMAIL":
        temp = session["temp_data"]
        temp["email"] = text
        state_manager.update_session(psid, state="AWAITING_PASSWORD", temp_data=temp)
        messenger_client.send_text(psid, "Thanks. Now enter your password:")
        return

    if state == "AWAITING_PASSWORD":
        email = session["temp_data"].get("email")
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

    if state.startswith("ORDER_") and state not in ("ORDER_ASK_OPTIONAL", "ORDER_CONFIRM"):
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

    # Fallback — unrecognised input for current state
    messenger_client.main_menu(psid, authenticated=bool(session["auth_token"]))


# ---------------------------------------------------------------------------
# Order submission
# ---------------------------------------------------------------------------

def _to_number(text: str):
    return float(text) if "." in text else int(text)


def submit_order(psid: str, session: dict):
    order = session["temp_data"]

    payload = {
        "product_name": order["product_name"],
        "qty": int(order["qty"]),
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

    success, data = jdwebnship_client.create_order(session["auth_token"], payload)

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
    app.run(port=5000, debug=True)