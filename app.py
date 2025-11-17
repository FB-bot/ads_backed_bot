# app.py
"""
Flask referral server.
POST /api/referral/register
GET  /api/user/<id>

Set TELEGRAM_BOT_TOKEN env var if you want to verify Telegram initData signature.
"""

import os
import sqlite3
import hashlib
import hmac
import json
from flask import Flask, request, jsonify, g
from datetime import datetime

# ---------- Config ----------
DATABASE = os.path.join(os.path.dirname(__file__), 'data.db')
REFERRAL_BONUS_CENTS = int(os.environ.get('REFERRAL_BONUS_CENTS', '50'))  # 0.5 USDT = 50 cents
TELEGRAM_BOT_TOKEN = os.environ.get('8213937413:AAHmp7SHCITYExufiYvQtEJJbZP7Svi4Uwg', '')  # optional, for initData verification
PORT = int(os.environ.get('PORT', 5000))
# ---------------------------

app = Flask(__name__)

# ---------- DB helpers ----------
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE, timeout=30, isolation_level=None)  # autocommit disabled by using explicit transactions
        db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    # Create tables if not exists
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
      new_user_id TEXT NOT NULL UNIQUE, -- UNIQUE enforces idempotency
      credited INTEGER DEFAULT 0,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(referrer_id) REFERENCES users(id)
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
    init_data_string should be the raw query string passed by Telegram (e.g. "key=value&key2=value2&hash=...")
    Returns True if valid OR TELEGRAM_BOT_TOKEN not set (skip).
    """
    if not TELEGRAM_BOT_TOKEN:
        # verification disabled
        return True

    try:
        # parse into dict
        kv = {}
        for part in init_data_string.split('&'):
            if '=' in part:
                k, v = part.split('=', 1)
                kv[k] = v  # keep raw encoded value (Telegram docs use decoded values for check-string)
        if 'hash' not in kv:
            return False
        hash_received = kv.pop('hash')

        # build data_check_string from sorted keys with decoded values
        items = []
        for k in sorted(kv.keys()):
            # decode percent-encoding for comparison as Telegram expects decoded values
            v = kv[k]
            try:
                v_dec = request.unquote(v)  # attempt - fallback if not available
            except Exception:
                # fallback: replace + and %20 handling
                v_dec = v.replace('+', ' ')
            items.append(f"{k}={v_dec}")
        data_check_string = '\n'.join(items)

        secret_key = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode()).digest()
        hmac_obj = hmac.new(secret_key, data_check_string.encode(), digestmod=hashlib.sha256)
        computed_hex = hmac_obj.hexdigest()
        # Telegram provides hex lowercase; compare safely
        return hmac.compare_digest(computed_hex, hash_received)
    except Exception as e:
        app.logger.warning("initData verify error: %s", e)
        return False

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
      "initDataString": "raw_string" (optional, used for verification)
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

    db = get_db()
    try:
        # Begin transaction
        db.execute('BEGIN')
        # Ensure referrer exists in users
        db.execute('INSERT OR IGNORE INTO users (id, first_name, last_name, username) VALUES (?, ?, ?, ?)',
                   (referrer, None, None, None))
        # Ensure new user exists
        db.execute('INSERT OR IGNORE INTO users (id, first_name, last_name, username) VALUES (?, ?, ?, ?)',
                   (new_user, first_name, last_name, username))

        # Try insert referral record - new_user_id is UNIQUE so duplicate will produce IntegrityError
        try:
            db.execute('INSERT INTO referrals (referrer_id, new_user_id, credited) VALUES (?, ?, 0)', (referrer, new_user))
        except sqlite3.IntegrityError:
            # Already recorded -> idempotent: rollback and return existing stats
            db.execute('ROLLBACK')
            row = db.execute('SELECT balance_cents, referral_count FROM users WHERE id = ?', (referrer,)).fetchone()
            balance = row['balance_cents'] if row else 0
            rcount = row['referral_count'] if row else 0
            return jsonify(success=True, message='Referral already processed (idempotent).', credited=False,
                           referrerBalanceCents=balance, referrerReferralCount=rcount)

        # New referral recorded — credit referrer
        db.execute('UPDATE users SET balance_cents = balance_cents + ?, referral_count = referral_count + 1 WHERE id = ?',
                   (REFERRAL_BONUS_CENTS, referrer))
        db.execute('UPDATE referrals SET credited = 1 WHERE new_user_id = ?', (new_user,))
        db.execute('COMMIT')

        row = db.execute('SELECT balance_cents, referral_count FROM users WHERE id = ?', (referrer,)).fetchone()
        balance = row['balance_cents'] if row else 0
        rcount = row['referral_count'] if row else 0
        return jsonify(success=True, message='Referral recorded and bonus credited.', credited=True,
                       referrerBalanceCents=balance, referrerReferralCount=rcount)
    except Exception as e:
        db.execute('ROLLBACK')
        app.logger.exception("Register referral error")
        return jsonify(success=False, message='Server error'), 500

@app.route('/api/user/<user_id>', methods=['GET'])
def get_user(user_id):
    db = get_db()
    row = db.execute('SELECT id, balance_cents, referral_count FROM users WHERE id = ?', (user_id,)).fetchone()
    if not row:
        return jsonify(success=False, message='User not found'), 404
    return jsonify(success=True, user={'id': row['id'], 'balance_cents': row['balance_cents'], 'referral_count': row['referral_count']}), 200

# ---------- Startup ----------
if __name__ == '__main__':
    os.makedirs(os.path.dirname(DATABASE), exist_ok=True) if os.path.dirname(DATABASE) else None
    with app.app_context():
        init_db()
    app.run(host='0.0.0.0', port=PORT)
