# server.py
import sqlite3
import hashlib
import json
import time
from flask import Flask, request, jsonify, g
from flask_cors import CORS

DB_PATH = 'referral.db'
REFERRAL_BONUS_CENTS = 50  # 0.5 USDT = 50 cents (1 USDT = 100 cents)
SECRET_TOKEN = None  # যদি চান, এখানে একটি স্ট্রিং দেন এবং client থেকে 'X-API-KEY' হিসেবে পাঠান

app = Flask(__name__)
CORS(app)  # সাধারণ ক্ষেত্রে সবকিছু খুলে দিলে dev সুবিধা; production-এ origin সীমাবদ্ধ করুন


def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        # enable check_same_thread=False for simple multi-thread dev servers
        db = g._database = sqlite3.connect(DB_PATH, check_same_thread=False)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
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


def compute_payload_hash(payload: dict) -> str:
    # Create deterministic hash from important fields to ensure idempotency
    keys = ['newUserId', 'referrerId', 'initDataString']
    pieces = []
    for k in keys:
        v = payload.get(k) or ''
        if isinstance(v, (dict, list)):
            v = json.dumps(v, sort_keys=True)
        pieces.append(str(v))
    s = '|'.join(pieces)
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


@app.route('/api/referral/register', methods=['POST'])
def register_referral():
    # Optional simple auth
    if SECRET_TOKEN:
        header_token = request.headers.get('X-API-KEY') or request.headers.get('Authorization')
        if not header_token or header_token != SECRET_TOKEN:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({'success': False, 'error': 'Invalid JSON payload'}), 400

    new_user_id = str(payload.get('newUserId', '')).strip()
    referrer_id = str(payload.get('referrerId', '')).strip()
    first_name = payload.get('first_name')
    last_name = payload.get('last_name')
    username = payload.get('username')
    init_data = payload.get('initDataString') or ''

    if not new_user_id or not referrer_id:
        return jsonify({'success': False, 'error': 'Missing newUserId or referrerId'}), 400

    payload_hash = compute_payload_hash(payload)
    db = get_db()
    cur = db.cursor()

    # Check if we've already processed this exact referral payload (idempotency)
    cur.execute('SELECT id, credited FROM referrals WHERE payload_hash = ?', (payload_hash,))
    row = cur.fetchone()
    if row:
        # Already seen this event
        already_credited = bool(row['credited'])
        # Get current referrer info to return
        cur.execute('SELECT balance_cents, referral_count FROM users WHERE id = ?', (referrer_id,))
        r = cur.fetchone()
        balance_cents = r['balance_cents'] if r else 0
        referral_count = r['referral_count'] if r else 0
        return jsonify({
            'success': True,
            'credited': already_credited,
            'referrerBalanceCents': balance_cents,
            'referrerReferralCount': referral_count
        })

    # Insert referral record (not credited yet)
    now = int(time.time())
    cur.execute('''
      INSERT INTO referrals (new_user_id, referrer_id, payload_hash, credited, created_at)
      VALUES (?, ?, ?, 0, ?)
    ''', (new_user_id, referrer_id, payload_hash, now))
    referral_row_id = cur.lastrowid

    # Ensure referrer exists in users table
    cur.execute('SELECT id FROM users WHERE id = ?', (referrer_id,))
    if not cur.fetchone():
        cur.execute('''
          INSERT INTO users (id, first_name, last_name, username, balance_cents, referral_count, created_at)
          VALUES (?, ?, ?, ?, 0, 0, ?)
        ''', (referrer_id, None, None, None, now))

    # Now credit referrer (business rule: only credit if the referred user is NEW and hasn't been used before)
    # We will mark referral credited and increment referrer balance and referral_count.
    try:
        cur.execute('''
          UPDATE users
          SET balance_cents = balance_cents + ?, referral_count = referral_count + 1
          WHERE id = ?
        ''', (REFERRAL_BONUS_CENTS, referrer_id))

        cur.execute('''
          UPDATE referrals
          SET credited = 1
          WHERE id = ?
        ''', (referral_row_id,))
        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': 'Database error', 'details': str(e)}), 500

    # Return updated referrer info
    cur.execute('SELECT balance_cents, referral_count FROM users WHERE id = ?', (referrer_id,))
    r = cur.fetchone()
    balance_cents = r['balance_cents'] if r else 0
    referral_count = r['referral_count'] if r else 0

    return jsonify({
        'success': True,
        'credited': True,
        'referrerBalanceCents': balance_cents,
        'referrerReferralCount': referral_count
    })


@app.route('/api/admin/user/<user_id>', methods=['GET'])
def get_user(user_id):
    # Simple admin view of a user (for debugging)
    cur = get_db().cursor()
    cur.execute('SELECT id, first_name, last_name, username, balance_cents, referral_count, created_at FROM users WHERE id = ?', (user_id,))
    r = cur.fetchone()
    if not r:
        return jsonify({'success': False, 'error': 'User not found'}), 404
    return jsonify({'success': True, 'user': dict(r)})


@app.route('/api/admin/users', methods=['GET'])
def list_users():
    cur = get_db().cursor()
    cur.execute('SELECT id, first_name, last_name, username, balance_cents, referral_count, created_at FROM users ORDER BY created_at DESC LIMIT 200')
    rows = cur.fetchall()
    return jsonify({'success': True, 'users': [dict(x) for x in rows]})


if __name__ == '__main__':
    init_db()
    print("Starting referral server on http://0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=True)
