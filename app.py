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
  ORDER_CUSTOMER -> ORDER_PHONE -> ORDER_ADDRESS -> ORDER_PRODUCT -> ORDER_AMOUNT -> ORDER_CONFIRM
    -> collects order fields one at a time, then confirms before calling Order Create API

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

ORDER_FIELDS = ["customer", "phone", "address", "product", "amount"]
ORDER_PROMPTS = {
    "customer": "What's the customer's name?",
    "phone": "What's the customer's phone number?",
    "address": "What's the delivery address?",
    "product": "Which product (and variant/pack size) are they ordering?",
    "amount": "What's the order amount (₹)?",
}


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
    if not _valid_signature(request):
        return "Invalid signature", 403

    body = request.get_json(force=True, silent=True) or {}
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
        state_manager.update_session(psid, state="ORDER_customer", temp_data={})
        messenger_client.send_text(psid, ORDER_PROMPTS["customer"])

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

    if state.startswith("ORDER_"):
        field = state.split("ORDER_", 1)[1]
        if field in ORDER_FIELDS:
            temp = session["temp_data"]
            temp[field] = text
            next_field = _next_order_field(field)
            if next_field:
                state_manager.update_session(psid, state=f"ORDER_{next_field}", temp_data=temp)
                messenger_client.send_text(psid, ORDER_PROMPTS[next_field])
            else:
                state_manager.update_session(psid, state="ORDER_CONFIRM", temp_data=temp)
                confirm_order(psid, temp)
            return

    # Fallback — unrecognised input for current state
    messenger_client.main_menu(psid, authenticated=bool(session["auth_token"]))


def _next_order_field(current: str):
    idx = ORDER_FIELDS.index(current)
    return ORDER_FIELDS[idx + 1] if idx + 1 < len(ORDER_FIELDS) else None


def confirm_order(psid: str, order: dict):
    summary = (
        f"Please confirm this order:\n\n"
        f"Customer: {order['customer']}\n"
        f"Phone: {order['phone']}\n"
        f"Address: {order['address']}\n"
        f"Product: {order['product']}\n"
        f"Amount: ₹{order['amount']}"
    )
    messenger_client.send_text(psid, summary)
    messenger_client.send_quick_replies(
        psid,
        "Confirm and place this order?",
        [
            {"title": "✅ Confirm", "payload": "ORDER_CONFIRM_YES"},
            {"title": "❌ Cancel", "payload": "ORDER_CONFIRM_NO"},
        ],
    )


def submit_order(psid: str, session: dict):
    order = session["temp_data"]
    success, data = jdwebnship_client.create_order(session["auth_token"], order)

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
