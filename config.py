"""
Central configuration. All secrets come from environment variables —
never hardcode tokens/keys in source.

Required env vars:
  PAGE_ACCESS_TOKEN   -> Long-lived Page Access Token from Meta App (Messenger/Instagram product)
  VERIFY_TOKEN        -> Any string you choose; used during webhook verification handshake
  APP_SECRET          -> Meta App Secret; used to verify X-Hub-Signature-256 on incoming webhooks
  JD_LOGIN_URL        -> Full URL of jdwebnship Login API
  JD_ORDER_CREATE_URL -> Full URL of jdwebnship Order Create API
  FERNET_KEY          -> Key used to encrypt auth tokens at rest (generate once, keep secret)
  SUPABASE_URL        -> Your Supabase project URL (Project Settings -> API)
  SUPABASE_SERVICE_KEY -> Supabase *service_role* key (server-side only, never expose to a client)
"""
import os
from dotenv import load_dotenv

load_dotenv()  # loads variables from .env into os.environ

PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
APP_SECRET = os.environ.get("APP_SECRET", "")

JD_LOGIN_URL = os.environ.get("JD_LOGIN_URL", "https://api.jdwebnship.example.com/login")
JD_ORDER_CREATE_URL = os.environ.get("JD_ORDER_CREATE_URL", "https://api.jdwebnship.example.com/order/create")

FERNET_KEY = os.environ.get("FERNET_KEY", "")  # generate with Fernet.generate_key()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

GRAPH_API_VERSION = "v20.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
