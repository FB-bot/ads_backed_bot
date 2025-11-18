# server.py
import os
import json
import time
import hmac
import hashlib
import sqlite3
from urllib.parse import parse_qs
from flask import Flask, request, jsonify, g, abort
from flask_cors import CORS
import requests

# ---------------- Config (via env) ----------------
DB_PATH = os.environ.get("DB_PATH", "referral.db")
REFERRAL_BONUS_CENTS = int(os.environ.get("REFERRAL_BONUS_CENTS", "50"))

# IMPORTANT: set these as environment variables in your Render dashboard
BOT_TOKEN = os.environ.get("8213937413:AAHmp7SHCITYExufiYvQtEJJbZP7Svi4Uwg")                 # e.g. 8213937... (do NOT hardcode token in file)
ADMIN_CHAT_ID = os.environ.get("1849126202")         # e.g. 1849126202
SECRET_TOKEN = os.environ.get("noobxvau")           # e.g. noobxvau
# flexible webhook secret (checks two possible env names)
WEBHOOK_SECRET = os.environ.get("noobxvau") or os.environ.get("smartearn") or "change_me_secret"
# your frontend webapp url (Netlify)
WEBAPP_URL = os.environ.get("https://mysmartearn.netlify.app/", "")           # e.g. https://mysmartearn.netlify.app
BOT_USERNAME = os.environ.get("@mysmartearn_bot", "")       # e.g. mysmartearn_bot
# optional PUBLIC base url for building webhook if different
PUBLIC_BASE_URL = os.environ.get("https://ads-backed-bot.onrender.com", "")  # e.g. https://ads-backed-bot.onrender.com

app = Flask(__name__)
CORS(app)

# ---------------- Database helpers ----------------
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
    )
    ''')
    cur.execute('''
    CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        new_user_id TEXT,
        referrer_id TEXT,
        payload_hash TEXT UNIQUE,
        credited INTEGER DEFAULT 0,
        created_at INTEGER
    )
    ''')
    db.commit()
    db.close()

# ---------------- Utilities ----------------
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

def verify_telegram_initdata(init_data_string: str, bot_token: str) -> bool:
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

def send_bot_message(chat_id: str, text: str, reply_markup: dict = None):
    if not BOT_TOKEN:
        app.logger.warning("send_bot_message: BOT_TOKEN not configured")
        return False, "BOT_TOKEN not configured"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.ok:
            app.logger.info("send_bot_message ok: chat_id=%s", chat_id)
            return True, r.json()
        else:
            app.logger.warning("send_bot_message failed: status=%s resp=%s", r.status_code, r.text)
            return False, r.text
    except Exception as e:
        app.logger.exception("send_bot_message exception: %s", e)
        return False, str(e)

# ---------------- Simple index & health ----------------
@app.route("/", methods=["GET"])
def index():
    host = PUBLIC_BASE_URL or WEBAPP_URL or request.url_root.rstrip("/")
    webhook_url = f"{host}/telegram/webhook/{WEBHOOK_SECRET}" if host else f"/telegram/webhook/{WEBHOOK_SECRET}"
    return f"""
    <html>
      <head><meta charset="utf-8"><title>Smart Earning API</title></head>
      <body style="font-family: system-ui, sans-serif; padding:24px;">
        <h2>Smart Earning — Backend</h2>
        <p>Status: <strong>Running</strong></p>
        <ul>
          <li>Webhook URL (configured): <code>{webhook_url}</code></li>
          <li><a href="/api/admin/users">/api/admin/users</a> — List users (protected by SECRET_TOKEN if set)</li>
          <li><a href="/set-webhook">/set-webhook</a> — Set Telegram webhook (protected if SECRET_TOKEN)</li>
          <li>/api/referral/register — POST endpoint (use client fetch)</li>
        </ul>
        <p style="color:#666;font-size:13px">Note: some routes require POST or SECRET_TOKEN; check logs for runtime info.</p>
      </body>
    </html>
    """, 200

@app.route("/health", methods=["GET"])
def health():
    return {"status":"ok","uptime": True}, 200

# ---------------- Referral API ----------------
@app.route("/api/referral/register", methods=["POST"])
def register_referral():
    if SECRET_TOKEN:
        header = request.headers.get("X-API-KEY") or request.headers.get("Authorization")
        if not header or header != SECRET_TOKEN:
            return jsonify({"success": False, "error": "Unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"success": False, "error": "Invalid JSON payload"}), 400

    new_user_id = str(payload.get("newUserId", "")).strip()
    referrer_id = str(payload.get("referrerId", "")).strip()
    init_data_string = payload.get("initDataString")

    if not new_user_id or not referrer_id:
        return jsonify({"success": False, "error": "Missing newUserId or referrerId"}), 400

    if init_data_string:
        if not BOT_TOKEN:
            return jsonify({"success": False, "error": "Server missing BOT_TOKEN for verification"}), 500
        ok = verify_telegram_initdata(init_data_string, BOT_TOKEN)
        if not ok:
            return jsonify({"success": False, "error": "initData verification failed"}), 400

    db = get_db()
    cur = db.cursor()
    payload_hash = compute_payload_hash(payload)

    cur.execute("SELECT id, credited FROM referrals WHERE payload_hash = ?", (payload_hash,))
    existing = cur.fetchone()
    if existing:
        already = bool(existing["credited"])
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
    cur.execute(
        "INSERT INTO referrals (new_user_id, referrer_id, payload_hash, credited, created_at) VALUES (?, ?, ?, 0, ?)",
        (new_user_id, referrer_id, payload_hash, now)
    )
    referral_row_id = cur.lastrowid

    cur.execute("SELECT id FROM users WHERE id = ?", (referrer_id,))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users (id, first_name, last_name, username, balance_cents, referral_count, created_at) VALUES (?, ?, ?, ?, 0, 0, ?)",
            (referrer_id, payload.get("first_name"), payload.get("last_name"), payload.get("username"), now)
        )

    try:
        cur.execute(
            "UPDATE users SET balance_cents = balance_cents + ?, referral_count = referral_count + 1 WHERE id = ?",
            (REFERRAL_BONUS_CENTS, referrer_id)
        )
        cur.execute("UPDATE referrals SET credited = 1 WHERE id = ?", (referral_row_id,))
        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({"success": False, "error": "Database error", "details": str(e)}), 500

    cur.execute("SELECT balance_cents, referral_count FROM users WHERE id = ?", (referrer_id,))
    r = cur.fetchone()
    balance_cents = r["balance_cents"] if r else 0
    referral_count = r["referral_count"] if r else 0

    try:
        if ADMIN_CHAT_ID:
            send_bot_message(ADMIN_CHAT_ID, f"✅ New referral credited\nReferrer: {referrer_id}\nNewUser: {new_user_id}\nAmount (cents): {REFERRAL_BONUS_CENTS}")
    except Exception:
        pass

    return jsonify({
        "success": True,
        "credited": True,
        "referrerBalanceCents": balance_cents,
        "referrerReferralCount": referral_count
    })

# ---------------- Telegram webhook handling ----------------
@app.route(f"/telegram/webhook/{WEBHOOK_SECRET}", methods=["POST"])
def telegram_webhook():
    # Optional: if you set secret_token in setWebhook, Telegram will include header X-Telegram-Bot-Api-Secret-Token
    # header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    # if header_secret and header_secret != WEBHOOK_SECRET:
    #     abort(403)

    update = request.get_json(silent=True)
    app.logger.info("TG update received: %s", json.dumps(update))
    if not update:
        return jsonify({"ok": False, "error": "no update"}), 400

    try:
        if "message" in update:
            msg = update["message"]
            chat = msg.get("chat", {})
            chat_id = chat.get("id")
            text = msg.get("text", "") or ""

            if text and text.strip().startswith("/start"):
                welcome_text = (
                    "স্বাগতম! 🎉\n\n"
                    "এই বটটি আপনাকে অ্যাড দেখিয়ে USDT আয় করতে সাহায্য করবে।\n"
                    "নীচের বাটন থেকে ওয়েব অ্যাপ খুলে কাজ শুরু করুন।"
                )
                webapp_url = WEBAPP_URL or (f"https://t.me/{BOT_USERNAME}/{WEBAPP_URL.split('/')[-1]}" if BOT_USERNAME and WEBAPP_URL else "")
                if not webapp_url:
                    text2 = welcome_text + "\n\n(ওয়েবঅ্যাপ URL কনফিগার করা নেই।)"
                    ok, resp = send_bot_message(chat_id, text2)
                    app.logger.info("send_bot_message -> ok=%s resp=%s", ok, resp)
                else:
                    reply_markup = {
                        "inline_keyboard": [
                            [
                                {"text": "Open Smart Earning", "web_app": {"url": webapp_url}}
                            ]
                        ]
                    }
                    ok, resp = send_bot_message(chat_id, welcome_text, reply_markup)
                    app.logger.info("send_bot_message -> ok=%s resp=%s", ok, resp)

                return jsonify({"ok": True}), 200

        if "web_app_data" in update:
            wad = update["web_app_data"]
            chat = update.get("message", {}).get("chat", {})
            chat_id = chat.get("id")
            data = wad.get("data")
            ack = "ধন্যবাদ! আপনি Web App খুলেছেন।"
            ok, resp = send_bot_message(chat_id, ack)
            app.logger.info("send_bot_message -> ok=%s resp=%s", ok, resp)
            return jsonify({"ok": True}), 200

    except Exception as e:
        app.logger.exception("telegram_webhook error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True}), 200

# ---------------- set-webhook helper (protected) ----------------
@app.route("/set-webhook", methods=["POST", "GET"])
def set_webhook():
    if SECRET_TOKEN:
        header = request.headers.get("X-API-KEY") or request.args.get("api_key") or request.headers.get("Authorization")
        if not header or header != SECRET_TOKEN:
            return jsonify({"success": False, "error": "Unauthorized"}), 401

    if not BOT_TOKEN:
        return jsonify({"success": False, "error": "BOT_TOKEN not configured"}), 500

    base_url = PUBLIC_BASE_URL or WEBAPP_URL or ""
    if not base_url:
        return jsonify({"success": False, "error": "Set PUBLIC_BASE_URL or WEBAPP_URL env var to the deployed domain"}), 400

    base_url = base_url.rstrip("/")
    webhook_url = f"{base_url}/telegram/webhook/{WEBHOOK_SECRET}"
    params = {"url": webhook_url}
    # If you want Telegram to include secret_token header, add:
    params["secret_token"] = WEBHOOK_SECRET

    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
    try:
        r = requests.get(tg_url, params=params, timeout=10)
        return jsonify({"success": True, "telegram_response": r.json()}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ---------------- Admin endpoints (small) ----------------
@app.route("/api/admin/users", methods=["GET"])
def list_users():
    if SECRET_TOKEN:
        header = request.headers.get("X-API-KEY") or request.args.get("api_key") or request.headers.get("Authorization")
        if not header or header != SECRET_TOKEN:
            return jsonify({"success": False, "error": "Unauthorized"}), 401
    cur = get_db().cursor()
    cur.execute("SELECT id, first_name, last_name, username, balance_cents, referral_count, created_at FROM users ORDER BY created_at DESC LIMIT 500")
    rows = cur.fetchall()
    return jsonify({"success": True, "users": [dict(r) for r in rows]})

@app.route("/api/admin/user/<user_id>", methods=["GET"])
def get_user(user_id):
    if SECRET_TOKEN:
        header = request.headers.get("X-API-KEY") or request.args.get("api_key") or request.headers.get("Authorization")
        if not header or header != SECRET_TOKEN:
            return jsonify({"success": False, "error": "Unauthorized"}), 401
    cur = get_db().cursor()
    cur.execute("SELECT id, first_name, last_name, username, balance_cents, referral_count, created_at FROM users WHERE id = ?", (user_id,))
    r = cur.fetchone()
    if not r:
        return jsonify({"success": False, "error": "User not found"}), 404
    return jsonify({"success": True, "user": dict(r)})

# ---------------- Run ----------------
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
