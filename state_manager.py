"""
Tracks per-retailer conversation state and their jdwebnship auth token,
stored in a Supabase (Postgres) table instead of local SQLite.

Run this once in the Supabase SQL editor to create the table:

    create table sessions (
        psid text primary key,
        state text not null default 'NEW',
        auth_token text,
        temp_data jsonb not null default '{}'::jsonb,
        updated_at timestamptz not null default now()
    );

`temp_data` holds whatever partial info we're collecting mid-conversation
(e.g. email while waiting for password, or order fields while building an
order) — cleared once that step completes.

Auth tokens are encrypted with Fernet *before* being sent to Supabase, so
even with DB access, tokens aren't readable without FERNET_KEY. Passwords
are NEVER stored here or anywhere else.

Uses the Supabase *service_role* key because this runs entirely
server-side (your Flask app), never in a browser/client — so Postgres
Row Level Security is bypassed intentionally and on purpose here. Do not
reuse SUPABASE_SERVICE_KEY in any client-facing code.
"""
from cryptography.fernet import Fernet
from supabase import create_client

import config

_fernet = Fernet(config.FERNET_KEY) if config.FERNET_KEY else None
_supabase = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)

TABLE = "sessions"


def _encrypt(value: str) -> str:
    if not _fernet or value is None:
        return value
    return _fernet.encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    if not _fernet or value is None:
        return value
    return _fernet.decrypt(value.encode()).decode()


def init_db():
    """
    No-op kept for interface compatibility with the previous SQLite version.
    Create the `sessions` table in Supabase using the SQL in the module
    docstring above (one-time, via the Supabase SQL editor).
    """
    pass


def get_session(psid: str) -> dict:
    res = _supabase.table(TABLE).select("*").eq("psid", psid).execute()
    rows = res.data

    if not rows:
        _supabase.table(TABLE).insert(
            {"psid": psid, "state": "NEW", "temp_data": {}}
        ).execute()
        return {"psid": psid, "state": "NEW", "auth_token": None, "temp_data": {}}

    row = rows[0]
    return {
        "psid": row["psid"],
        "state": row["state"],
        "auth_token": _decrypt(row["auth_token"]) if row.get("auth_token") else None,
        "temp_data": row.get("temp_data") or {},
    }


def update_session(psid: str, state: str = None, auth_token: str = None, temp_data: dict = None):
    session = get_session(psid)  # ensures a row exists, gives us current values

    payload = {"state": state if state is not None else session["state"]}
    if auth_token is not None:
        payload["auth_token"] = _encrypt(auth_token)
    payload["temp_data"] = temp_data if temp_data is not None else session["temp_data"]

    _supabase.table(TABLE).update(payload).eq("psid", psid).execute()


def clear_auth(psid: str):
    """Log the retailer out — drop token and reset state."""
    _supabase.table(TABLE).update(
        {"state": "NEW", "auth_token": None, "temp_data": {}}
    ).eq("psid", psid).execute()
