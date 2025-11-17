# app.py
import os
import sqlite3
import hashlib
import hmac
import urllib.parse
import logging
from flask import Flask, request, jsonify, g
from flask_cors import CORS
import requests

# ---------------- Config ----------------
DATABASE = os.environ.get('DATABASE_PATH', os.path.join(os.path.dirname(__file__), 'data.db'))
REFERRAL_BONUS_CENTS = int(os.environ.get('REFERRAL_BONUS_CENTS', '50'))  # default 50 cents = 0.5 USDT
TELEGRAM_BOT_TOKEN = os.environ.get('8213937413:AAHmp7SHCITYExufiYvQtEJJbZP7Svi4Uwg', '')  # set this in env (DO NOT hardcode token)
PORT = int(os.environ.get('PORT', 5000))
# ----------------------------------------

# Flask app
app = Flask(__name__)
CORS(app)  # for development; in production, restrict origins

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("referral-server")

# ---------- DB helpers ----------
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        # ensure directory exists if path contains folder
        dirpath = os.path.dirname(DATABASE)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        db = g._database = sqlite3.connect(DATABASE, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
      id TEXT PRIMARY KEY,
      first_name TEXT,
      last_name TEXT,
      username TEXT,
      balance_cents INTEGER DEFAULT 0,
      referral_count INTEGER DEFAULT 0,
      registered_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS referrals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      referrer_id TEXT NOT NULL,
      new_user_id TEXT NOT NULL UNIQUE,
      credited INTEGER DEFAULT 0,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(referrer_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS withdraws (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id TEXT NOT NULL,
      amount_cents INTEGER NOT NULL,
      wallet TEXT,
      status TEXT DEFAULT 'pending',
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    db.commit()

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

# ---------- Telegram initData verification ----------
def verify_init_data(init_data_string: str) -> bool:
    """
    Verify Telegram WebApp initData string using bot token.
    Returns True if TELEGRAM_BOT_TOKEN not set (skip) or verification OK.
    """
    if not TELEGRAM_BOT_TOKEN:
        return True

    try:
        # parse into dict (raw percent-encoded values)
        kv = {}
        for part in init_data_string.split('&'):
            if '=' in part:
                k, v = part.split('=', 1)
                kv[k] = v
        if 'hash' not in kv:
            return False
        hash_received = kv.pop('hash')

        items = []
        for k in sorted(kv.keys()):
            v_dec = urllib.parse.unquote(kv[k])
            items.append(f"{k}={v_dec}")
        data_check_string = '\n'.join(items)

        secret_key = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode()).digest()
        mac = hmac.new(secret_key, data_check_string.encode(), digestmod=hashlib.sha256)
        computed_hex = mac.hexdigest()
        return hmac.compare_digest(computed_hex, hash_received)
    except Exception as e:
        logger.exception("initData verify error: %s", e)
        return False

# ---------- Helper: send telegram message ----------
def send_message(chat_id, text, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not configured; cannot send message")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=8)
        if resp.status_code != 200:
            logger.warning("sendMessage failed: %s %s", resp.status_code, resp.text)
        return resp.status_code == 200
    except Exception as e:
        logger.exception("send_message error: %s", e)
        return False

# ---------- Helper: process referral (idempotent) ----------
def process_referral_server_side(referrer_id, new_user_id, first_name=None, last_name=None, username=None):
    """
    Insert referral record; credit referrer if not previously credited.
    Returns dict with status and stats.
    """
    db = get_db()
    try:
        db.execute('BEGIN')
        # ensure referrer user exists (without overwriting existing names)
        db.execute('INSERT OR IGNORE INTO users (id, first_name, last_name, username) VALUES (?, ?, ?, ?)',
                   (referrer_id, None, None, None))
        db.execute('INSERT OR IGNORE INTO users (id, first_name, last_name, username) VALUES (?, ?, ?, ?)',
                   (new_user_id, first_name, last_name, username))
        try:
            db.execute('INSERT INTO referrals (referrer_id, new_user_id, credited) VALUES (?, ?, 0)', (referrer_id, new_user_id))
        except sqlite3.IntegrityError:
            # already exists -> idempotent
            db.execute('ROLLBACK')
            row = db.execute('SELECT balance_cents, referral_count FROM users WHERE id = ?', (referrer_id,)).fetchone()
            balance = row['balance_cents'] if row else 0
            rcount = row['referral_count'] if row else 0
            return {"success": True, "credited": False, "referrerBalanceCents": balance, "referrerReferralCount": rcount, "message": "already processed"}

        # credit referrer
        db.execute('UPDATE users SET balance_cents = balance_cents + ?, referral_count = referral_count + 1 WHERE id = ?',
                   (REFERRAL_BONUS_CENTS, referrer_id))
        db.execute('UPDATE referrals SET credited = 1 WHERE new_user_id = ?', (new_user_id,))
        db.execute('COMMIT')
        row = db.execute('SELECT balance_cents, referral_count FROM users WHERE id = ?', (referrer_id,)).fetchone()
        balance = row['balance_cents'] if row else 0
        rcount = row['referral_count'] if row else 0
        return {"success": True, "credited": True, "referrerBalanceCents": balance, "referrerReferralCount": rcount, "message": "credited"}
    except Exception as e:
        db.execute('ROLLBACK')
        logger.exception("process_referral_server_side error")
        return {"success": False, "message": "server error"}

# ---------- Routes ----------
@app.route('/api/referral/register', methods=['POST'])
def register_referral():
    """
    Expected JSON:
    {
      "newUserId": "...",
      "referrerId": "...",
      "first_name": "...", (optional)
      "last_name": "...",  (optional)
      "username": "...",   (optional)
      "initDataString": "raw_string" (optional)
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    new_user = data.get('newUserId')
    referrer = data.get('referrerId')
    first_name = data.get('first_name') or None
    last_name = data.get('last_name') or None
    username = data.get('username') or None
    init_data_string = data.get('initDataString')

    if not new_user or not referrer:
        return jsonify(success=False, message='newUserId and referrerId required'), 400

    # Optional verify initData
    if init_data_string and TELEGRAM_BOT_TOKEN:
        ok = verify_init_data(init_data_string)
        if not ok:
            return jsonify(success=False, message='initData verification failed'), 403

    res = process_referral_server_side(referrer, new_user, first_name, last_name, username)
    if res.get("success"):
        return jsonify(res)
    else:
        return jsonify(res), 500

@app.route('/api/user/<user_id>', methods=['GET'])
def get_user(user_id):
    db = get_db()
    row = db.execute('SELECT id, balance_cents, referral_count FROM users WHERE id = ?', (user_id,)).fetchone()
    if not row:
        return jsonify(success=False, message='User not found'), 404
    return jsonify(success=True, user={'id': row['id'], 'balance_cents': row['balance_cents'], 'referral_count': row['referral_count']}), 200

@app.route('/api/withdraw', methods=['POST'])
def log_withdraw():
    """
    Optional: client can POST withdraw requests here for admin processing.
    Expected JSON:
    { userId, amount_usdt (string or number), wallet, note (optional) }
    """
    data = request.get_json(force=True, silent=True) or {}
    user_id = data.get('userId')
    amount = data.get('amount_usdt')
    wallet = data.get('wallet') or data.get('walletAddress') or None

    if not user_id or amount is None:
        return jsonify(success=False, message='userId and amount_usdt required'), 400
    try:
        # amount in USDT -> convert to cents
        amount_cents = int(float(amount) * 100)
    except Exception:
        return jsonify(success=False, message='invalid amount'), 400

    db = get_db()
    try:
        db.execute('INSERT INTO withdraws (user_id, amount_cents, wallet, status) VALUES (?, ?, ?, ?)',
                   (user_id, amount_cents, wallet, 'pending'))
        return jsonify(success=True, message='Withdraw request logged'), 200
    except Exception:
        logger.exception("log_withdraw error")
        return jsonify(success=False, message='Server error'), 500

# ---------- Webhook to receive Telegram updates ----------
@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    logger.info("Telegram update: %s", update)

    # handle message with text (e.g., "/start ref123")
    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        user = msg.get("from", {})
        text = msg.get("text", "") or ""

        # handle /start with payload: "/start ref<id>"
        if text.startswith("/start"):
            parts = text.split()
            payload = parts[1] if len(parts) > 1 else None

            # Telegram deep link also can pass start_param via Web App open; this handles classic start with payload
            if payload and payload.startswith("ref"):
                referrer_id = payload.replace("ref", "")
                new_user_id = str(user.get("id"))
                first_name = user.get("first_name")
                last_name = user.get("last_name")
                username = user.get("username")
                res = process_referral_server_side(referrer_id, new_user_id, first_name, last_name, username)
                if res.get("success"):
                    if res.get("credited"):
                        send_message(chat_id, f"স্বাগতম {first_name or 'User'}! আপনি রেজিস্টার হয়েছেন — রেফারারকে বোনাস দেয়া হয়েছে।")
                    else:
                        send_message(chat_id, f"স্বাগতম {first_name or 'User'}! আপনার রেফারাল পূর্বে প্রসেস করা হয়েছে অথবা আপনি আগে থেকেই রেজিস্টারড।")
                else:
                    send_message(chat_id, "দুঃখিত — রেফারাল প্রসেসিং সময় সমস্যা হয়েছে। পরে চেষ্টা করুন।")
            else:
                # Normal /start without payload
                send_message(chat_id, f"স্বাগতম {user.get('first_name','User')}! Smart Earning এ আপনাকে স্বাগতম।")
    # handle other update types if needed (callback_query, etc.)
    return jsonify({"ok": True})

# ---------- Startup ----------
if __name__ == '__main__':
    with app.app_context():
        init_db()
    # Use host 0.0.0.0 for container/Render; for production use gunicorn
    app.run(host='0.0.0.0', port=PORT)
