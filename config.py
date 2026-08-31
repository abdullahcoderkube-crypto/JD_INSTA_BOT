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

Optional env vars:
  GROQ_API_KEY        -> Groq API key; enables AI free-form order extraction
  GROQ_MODEL          -> Groq model name (default: moonshotai/kimi-k2-instruct)
  GROQ_BASE_URL       -> Groq API base URL (default: https://api.groq.com/openai/v1)
  GROQ_TIMEOUT        -> HTTP timeout in seconds for Groq calls (default: 45)
  GROQ_MAX_TOKENS     -> Max completion tokens for Groq responses (default: 4500)

For local development, put these in a `.env` file (see `.env.example`) —
this file auto-loads it. In production (Render, Railway, etc.) set these
as actual environment variables in the host's dashboard instead; .env
files are for local dev only and should never be committed to git.
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()  # loads .env into os.environ if present; no-op if it doesn't exist

PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
APP_SECRET = os.environ.get("APP_SECRET", "")

JD_LOGIN_URL = os.environ.get("JD_LOGIN_URL", "https://ndstage.jdwebnship.in/api/bot/login")
JD_ORDER_CREATE_URL = os.environ.get("JD_ORDER_CREATE_URL", "https://ndstage.jdwebnship.in/api/bot/direct-order")

FERNET_KEY = os.environ.get("FERNET_KEY", "")  # generate with Fernet.generate_key()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# Groq AI — free-form order extraction. Optional; bot falls back to manual wizard if absent.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "qwen/qwen3.8-27b")
GROQ_BASE_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_TIMEOUT = int(os.environ.get("GROQ_TIMEOUT", "45"))
GROQ_MAX_TOKENS = int(os.environ.get("GROQ_MAX_TOKENS", "4500"))

GRAPH_API_VERSION = "v20.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# Fail loudly and clearly at startup rather than letting a downstream
# library (e.g. supabase-py) raise a cryptic "Invalid API key" error.
_REQUIRED = {
    "PAGE_ACCESS_TOKEN": PAGE_ACCESS_TOKEN,
    "VERIFY_TOKEN": VERIFY_TOKEN,
    "APP_SECRET": APP_SECRET,
    "FERNET_KEY": FERNET_KEY,
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_SERVICE_KEY": SUPABASE_SERVICE_KEY,
}
_missing = [name for name, value in _REQUIRED.items() if not value]
if _missing:
    sys.exit(
        "Missing required environment variable(s): " + ", ".join(_missing) +
        "\nLocally: add them to a .env file (see .env.example) in the same folder as app.py."
        "\nOn Render/Railway/etc.: set them in the host's dashboard under Environment Variables."
    )