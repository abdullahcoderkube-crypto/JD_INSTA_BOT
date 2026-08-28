# jdwebnship Instagram Ordering Bot

Lets a retailer log into their jdwebnship account and place an order,
entirely from an Instagram DM, via two buttons: **Login** and **Order**.

## How it works

1. Retailer messages your Instagram Business account.
2. Bot shows a **Login** button (if not authenticated) or **Place Order** /
   **Log Out** buttons (if already authenticated).
3. **Login**: bot asks for email, then password, calls jdwebnship's Login
   API, and stores the returned auth token (encrypted) against that
   retailer's Instagram ID. The password itself is never saved anywhere.
4. **Place Order**: bot asks for customer name, phone, address, product,
   and amount — one at a time — shows a summary, and on confirmation calls
   jdwebnship's Order Create API using the stored token.

## Setup

### 1. Meta / Instagram side
- Convert the target Instagram account to a **Business** or **Creator**
  account and connect it to a Facebook Page.
- Create an app at developers.facebook.com, add the **Messenger** product
  (Instagram messaging runs through the same Send API).
- Generate a Page Access Token, note your App Secret.
- Set your webhook URL to `https://yourdomain.com/webhook`, subscribe to
  the `messages` and `messaging_postbacks` fields.

### 2. Supabase setup
- Create a project at supabase.com.
- Open the SQL editor and run:
  ```sql
  create table sessions (
      psid text primary key,
      state text not null default 'NEW',
      auth_token text,
      temp_data jsonb not null default '{}'::jsonb,
      updated_at timestamptz not null default now()
  );
  ```
- From Project Settings → API, copy the **Project URL** and the
  **`service_role` key** (not the `anon` key — this app runs entirely
  server-side and needs elevated access to read/write any retailer's row).

### 3. Environment variables

```bash
export PAGE_ACCESS_TOKEN="..."
export VERIFY_TOKEN="choose-any-string"
export APP_SECRET="..."
export JD_LOGIN_URL="https://api.jdwebnship.com/<real-login-endpoint>"
export JD_ORDER_CREATE_URL="https://api.jdwebnship.com/<real-order-endpoint>"
export FERNET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export SUPABASE_URL="https://<your-project-ref>.supabase.co"
export SUPABASE_SERVICE_KEY="..."
```

### 4. Install & run

```bash
pip install -r requirements.txt
python app.py
```

For production, run behind gunicorn + a reverse proxy with a real TLS
cert (Meta requires HTTPS), e.g.:

```bash
gunicorn -w 2 -b 0.0.0.0:5000 app:app
```

## Files

| File                   | Purpose                                              |
|------------------------|-------------------------------------------------------|
| `app.py`               | Webhook receiver + conversation state machine         |
| `messenger_client.py`  | Sends text/buttons/quick-replies via Meta Send API     |
| `jdwebnship_client.py` | Calls the Login and Order Create APIs                 |
| `state_manager.py`     | Per-retailer session storage (Supabase, token encrypted) |
| `config.py`            | Environment-variable based configuration               |

## TODO before going live

1. **Plug in the real jdwebnship API contracts** in `jdwebnship_client.py`
   — field names, auth header format, and response shape are placeholders
   until you share the actual API docs.
2. **Token refresh**: if jdwebnship tokens expire, decide whether to
   silently re-login (would require storing credentials — not
   recommended) or require the retailer to log in again (current
   behavior, triggered automatically on a 401 from Order Create).
3. **Multi-retailer safety**: this already keys everything off the
   Instagram sender ID (PSID), so each retailer's token is isolated.
4. **Meta policy compliance**: review Messenger Platform policies for
   business messaging (standard messaging window, message tags) before
   sending confirmations outside the 24-hour window.
5. **Idempotency**: Meta may redeliver the same webhook event; consider
   deduping by `message.mid` if you see duplicate orders in testing.
6. Set `debug=False` and put a real WSGI server in front for production.
