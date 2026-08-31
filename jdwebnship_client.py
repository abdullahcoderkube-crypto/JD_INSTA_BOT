"""
Thin wrapper around jdwebnship's Login and Order Create APIs.

Order Create endpoint: POST {JD_ORDER_CREATE_URL}
  e.g. https://ndstage.jdwebnship.in/api/bot/direct-order
Auth header: Authorization: Bearer <auth_token>  (auth_token comes from Login)
"""
import requests
import config

TIMEOUT = 15  # seconds


def login(email: str, password: str) -> tuple[bool, dict]:
    """
    Calls jdwebnship Login API.
    Expected on success: some kind of auth token in the response, e.g.
        {"success": true, "token": "...", "retailer_name": "..."}
    Returns (success, data_or_error).
    """
    try:
        resp = requests.post(
            config.JD_LOGIN_URL,
            json={"email": email, "password": password},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        return False, {"error": f"Network error contacting jdwebnship: {exc}"}

    if resp.status_code != 200:
        return False, {"error": f"Login failed (HTTP {resp.status_code}): {resp.text[:200]}"}

    data = resp.json()
    token = data.get("token")
    if not token:
        return False, {"error": data.get("message", "Login failed — no token returned.")}

    return True, data


def create_order(auth_token: str, order: dict) -> tuple[bool, dict]:
    """
    Calls jdwebnship's POST /api/bot/direct-order.

    `order` payload shape (required fields always present, optional fields
    only included when the retailer actually provided them):
        {
            "product_name": "Blue T-Shirt",
            "product_sku": "SKU-001",            # optional
            "subcategory": "T-SHIRTS",            # optional
            "qty": 1,
            "price": 499,
            "product_weight": "0.5",              # "0.5" or "2"
            "payment_method": "cod",
            "customer_mobile": "9876543210",
            "customer_full_name": "Rahul Sharma",
            "email": "rahul@example.com",         # optional
            "customer_alt_number": "9123456780",  # optional
            "customer_address": "123 MG Road",
            "pincode": "560001",
            "state": "Karnataka",                 # optional
            "city": "Bangalore"                   # optional
        }
    Returns (success, data_or_error). Assumes a successful response includes
    some kind of order reference (e.g. "order_id") — adjust the key in
    app.py's submit_order() once you see a real response shape.
    """
    headers = {"Authorization": f"Bearer {auth_token}"}
    try:
        resp = requests.post(
            config.JD_ORDER_CREATE_URL,
            json=order,
            headers=headers,
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        return False, {"error": f"Network error contacting jdwebnship: {exc}"}

    if resp.status_code == 401:
        return False, {"error": "AUTH_EXPIRED"}

    if resp.status_code not in (200, 201):
        return False, {"error": f"Order failed (HTTP {resp.status_code}): {resp.text[:200]}"}

    return True, resp.json()