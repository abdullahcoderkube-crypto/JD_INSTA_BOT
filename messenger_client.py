"""
Wrapper around Meta's Send API for Instagram/Messenger business messaging.
https://developers.facebook.com/docs/messenger-platform/reference/send-api
"""
import requests
import config

SEND_URL = f"{config.GRAPH_API_BASE}/me/messages"


def _post(payload: dict):
    params = {"access_token": config.PAGE_ACCESS_TOKEN}
    resp = requests.post(SEND_URL, params=params, json=payload, timeout=15)
    if resp.status_code != 200:
        print(f"[messenger_client] send failed: {resp.status_code} {resp.text}")
    return resp


def send_text(psid: str, text: str):
    return _post({"recipient": {"id": psid}, "message": {"text": text}})


def send_button_template(psid: str, text: str, buttons: list):
    """
    buttons: list of {"title": str, "payload": str} — rendered as postback buttons.
    Max 3 buttons per Meta's limits.
    """
    return _post({
        "recipient": {"id": psid},
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": text,
                    "buttons": [
                        {"type": "postback", "title": b["title"], "payload": b["payload"]}
                        for b in buttons[:3]
                    ],
                },
            }
        },
    })


def send_quick_replies(psid: str, text: str, options: list):
    """
    options: list of {"title": str, "payload": str}
    """
    return _post({
        "recipient": {"id": psid},
        "message": {
            "text": text,
            "quick_replies": [
                {"content_type": "text", "title": o["title"], "payload": o["payload"]}
                for o in options[:13]
            ],
        },
    })


def main_menu(psid: str, authenticated: bool):
    if authenticated:
        send_button_template(
            psid,
            "What would you like to do?",
            [
                {"title": "Place Order", "payload": "ORDER"},
                {"title": "Log Out", "payload": "LOGOUT"},
            ],
        )
    else:
        send_button_template(
            psid,
            "Welcome! Please log in to your jdwebnship account to place orders.",
            [{"title": "Login", "payload": "LOGIN"}],
        )
