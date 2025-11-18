# server.py
import os
import json
import time
import hmac
import hashlib
import sqlite3
from urllib.parse import parse_qs
from flask import Flask, request, jsonify, g
from flask_cors import CORS
import requests

# Config via environment
DB_PATH = os.environ.get("DB_PATH", "referral.db")
REFERRAL_BONUS_CENTS = int(os.environ.get("REFERRAL_BONUS_CENTS", "50"))  # 50 = 0.5 USDT
BOT_TOKEN = os.environ.get("8213937413:AAHmp7SHCITYExufiYvQtEJJbZP7Svi4Uwg")  # REQUIRED for initData verification & bot messages
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")  # optional: to notify admin
SECRET_TOKEN = os.environ.get("SECRET_TOKEN")  # optional: extra header auth for API

app = Flask(__name__)
CORS(app)

# ---------- DB helpers ----------
def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = sqlite3.connect(DB_PATH, check_same_thread=False)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_db", None)
    if db:
        db.commit()
        db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    cur = db.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        first_name TEXT,
        last_name TEXT,
        username TEXT,
        balance_cents INTEGER DEFAULT 0,
        referral_count INTEGER DEFAULT 0,
        created_at INTEGER
    )''')
    cur.execute('''
    CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        new_user_id TEXT,
        referrer_id TEXT,
        payload_hash TEXT UNIQUE,
        credited INTEGER DEFAULT 0,
        created_at INTEGER
    )''')
    db.commit()
    db.close()

# ---------- Telegram initData verification ----------
def verify_telegram_initdata(init_data_string: str, bot_token: str) -> bool:
    """
    Verify initData string from Telegram WebApp.
    init_data_string: query-string like "key1=val1&key2=val2&hash=..."
    """
    if not init_data_string or not bot_token:
        return False
    try:
        qs = parse_qs(init_data_string, keep_blank_values=True)
        hash_list = qs.pop('hash', None)
        if not hash_list:
            return False
        received_hash = hash_list[0]

        items = []
        for k in sorted(qs.keys()):
            v = qs[k][0]
            items.append(f"{k}={v}")
        data_check_string = "\n".join(items)

        secret = hashlib.sha256(bot_token.encode('utf-8')).digest()
        computed_hmac = hmac.new(secret, data_check_string.encode('utf-8'), hashlib.sha256).hexdigest()
        return hmac.compare_digest(computed_hmac, received_hash)
    except Exception as e:
        app.logger.warning("initData verification error: %s", e)
        return False

# ---------- utility ----------
def compute_payload_hash(payload: dict) -> str:
    keys = ['newUserId', 'referrerId', 'initDataString']
    pieces = []
    for k in keys:
        v = payload.get(k) or ''
        if isinstance(v, (dict, list)):
            v = json.dumps(v, sort_keys=True)
        pieces.append(str(v))
    s = '|'.join(pieces)
    return hashlib.sha256(s.encode('utf-8')).hexdigest()

def send_bot_message(chat_id: str, text: str):
    if not BOT_TOKEN:
        return False, "No BOT_TOKEN configured"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.ok:
            return True, r.json()
        else:
            return False, r.text
    except Exception as e:
        return False, str(e)

# ---------- API endpoints ----------
@app.route("/api/referral/register", methods=["POST"])
def register_referral():
    # optional header auth
    if SECRET_TOKEN:
        header = request.headers.get('X-API-KEY') or request.headers.get('Authorization')
        if not header or header != SECRET_TOKEN:
            return jsonify({"success": False, "error": "Unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"success": False, "error": "Invalid JSON payload"}), 400

    new_user_id = str(payload.get("newUserId", "")).strip()
    referrer_id = str(payload.get("referrerId", "")).strip()
    init_data_string = payload.get("initDataString")  # may be None
    # validate presence
    if not new_user_id or not referrer_id:
        return jsonify({"success": False, "error": "Missing newUserId or referrerId"}), 400

    # If initDataString present, verify it
    if init_data_string:
        if not BOT_TOKEN:
            return jsonify({"success": False, "error": "Server missing BOT_TOKEN for verification"}), 500
        ok = verify_telegram_initdata(init_data_string, BOT_TOKEN)
        if not ok:
            return jsonify({"success": False, "error": "initData verification failed"}), 400

    db = get_db()
    cur = db.cursor()
    payload_hash = compute_payload_hash(payload)

    # idempotency: check if payload_hash seen
    cur.execute("SELECT id, credited FROM referrals WHERE payload_hash = ?", (payload_hash,))
    existing = cur.fetchone()
    if existing:
        already = bool(existing["credited"])
        # return current referrer stats
        cur.execute("SELECT balance_cents, referral_count FROM users WHERE id = ?", (referrer_id,))
        r = cur.fetchone()
        balance_cents = r["balance_cents"] if r else 0
        referral_count = r["referral_count"] if r else 0
        return jsonify({
            "success": True,
            "credited": already,
            "referrerBalanceCents": balance_cents,
            "referrerReferralCount": referral_count
        })

    now = int(time.time())
    # create referral record (not credited yet)
    cur.execute(
        "INSERT INTO referrals (new_user_id, referrer_id, payload_hash, credited, created_at) VALUES (?, ?, ?, 0, ?)",
        (new_user_id, referrer_id, payload_hash, now)
    )
    referral_row_id = cur.lastrowid

    # ensure referrer exists
    cur.execute("SELECT id FROM users WHERE id = ?", (referrer_id,))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users (id, first_name, last_name, username, balance_cents, referral_count, created_at) VALUES (?, ?, ?, ?, 0, 0, ?)",
            (referrer_id, payload.get("first_name"), payload.get("last_name"), payload.get("username"), now)
        )

    # credit referrer
    try:
        cur.execute("UPDATE users SET balance_cents = balance_cents + ?, referral_count = referral_count + 1 WHERE id = ?",
                    (REFERRAL_BONUS_CENTS, referrer_id))
        cur.execute("UPDATE referrals SET credited = 1 WHERE id = ?", (referral_row_id,))
        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({"success": False, "error": "Database error", "details": str(e)}), 500

    # fetch updated info
    cur.execute("SELECT balance_cents, referral_count FROM users WHERE id = ?", (referrer_id,))
    r = cur.fetchone()
    balance_cents = r["balance_cents"] if r else 0
    referral_count = r["referral_count"] if r else 0

    # optional: notify admin bot
    try:
        if ADMIN_CHAT_ID:
            text = f"✅ New referral credited\nReferrer: {referrer_id}\nNewUser: {new_user_id}\nAmount (cents): {REFERRAL_BONUS_CENTS}"
            send_bot_message(ADMIN_CHAT_ID, text)
    except Exception:
        pass

    return jsonify({
        "success": True,
        "credited": True,
        "referrerBalanceCents": balance_cents,
        "referrerReferralCount": referral_count
    })

# small admin endpoints
@app.route("/api/admin/users", methods=["GET"])
def list_users():
    cur = get_db().cursor()
    cur.execute("SELECT id, first_name, last_name, username, balance_cents, referral_count, created_at FROM users ORDER BY created_at DESC LIMIT 500")
    rows = cur.fetchall()
    return jsonify({"success": True, "users": [dict(r) for r in rows]})

@app.route("/api/admin/user/<user_id>", methods=["GET"])
def get_user(user_id):
    cur = get_db().cursor()
    cur.execute("SELECT id, first_name, last_name, username, balance_cents, referral_count, created_at FROM users WHERE id = ?", (user_id,))
    r = cur.fetchone()
    if not r:
        return jsonify({"success": False, "error": "User not found"}), 404
    return jsonify({"success": True, "user": dict(r)})

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
