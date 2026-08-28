"""
Thin wrapper around jdwebnship's Login and Order Create APIs.

NOTE: The exact request/response shape below is a placeholder based on
typical login/order APIs. Swap in the real field names once you have the
jdwebnship API docs — everything else in the bot stays the same as long
as these two functions keep returning (success: bool, data: dict).
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
    Calls jdwebnship Order Create API.
    `order` example payload (matches the format given):
        {
            "customer": "Sadashiva",
            "phone": "9109066064",
            "address": "Sadashiva Nagar, Nelamangala, Karnataka – 562123, Near Tejaswini Transport",
            "product": "Jamalgota Oil + Powder 100gm Combo Pack",
            "amount": 899
        }
    Returns (success, data_or_error).
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
