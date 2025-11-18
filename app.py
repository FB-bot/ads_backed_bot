# bot_with_ref.py
import os
import json
import logging
import asyncio
import requests

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("8213937413:AAHmp7SHCITYExufiYvQtEJJbZP7Svi4Uwg")
API_BASE = os.environ.get("API_BASE", "http://127.0.0.1:5000")
REF_SECRET = os.environ.get("REF_SECRET")  # optional

if not BOT_TOKEN:
    logger.error("BOT_TOKEN is not set in environment variables.")
    raise SystemExit("Set BOT_TOKEN env variable and retry.")

async def post_referral_async(payload):
    """Do blocking requests.post in thread to avoid blocking asyncio loop."""
    headers = {"Content-Type": "application/json"}
    if REF_SECRET:
        headers["X-REF-SECRET"] = REF_SECRET
    loop = asyncio.get_running_loop()
    def do_post():
        try:
            r = requests.post(API_BASE.rstrip('/') + "/api/referral/register", json=payload, headers=headers, timeout=8)
            return r.status_code, r.text
        except Exception as e:
            return None, str(e)
    return await loop.run_in_executor(None, do_post)

def extract_start_param(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context.args works for /start <payload>
    if context.args:
        return context.args[0]
    # fallback: parse text (rare)
    if update.message and update.message.text:
        parts = update.message.text.split()
        if len(parts) > 1:
            return parts[1]
    return None

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    start_param = extract_start_param(update, context)
    await update.message.reply_text(f"স্বাগতম, {user.first_name or 'user'}! Processing start...")

    if start_param and start_param.startswith("ref"):
        referrer_id = start_param.replace("ref","").strip()
        new_user_id = str(user.id)
        payload = {
            "newUserId": new_user_id,
            "referrerId": str(referrer_id),
            "first_name": user.first_name,
            "last_name": user.last_name,
            "username": user.username
        }
        status, text = await post_referral_async(payload)
        if status == 200:
            try:
                j = json.loads(text)
                if j.get("success"):
                    if j.get("credited"):
                        bal = j.get("referrerBalanceCents", 0) / 100.0
                        cnt = j.get("referrerReferralCount", 0)
                        await update.message.reply_text(f"Referral verified — referrer credited. Referrer new balance: USDT {bal:.2f} (refs: {cnt})")
                    else:
                        await update.message.reply_text("Referral recorded earlier (no new credit).")
                else:
                    await update.message.reply_text("Referral API returned error.")
            except Exception as e:
                await update.message.reply_text("Bad response from referral server.")
                logger.exception("parse error")
        else:
            await update.message.reply_text(f"Failed to contact referral server: {text}")
    else:
        await update.message.reply_text("No referral parameter found in /start.")

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("This bot supports referral links. Use t.me/YourBot?start=ref<your_id> to share.")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))

    logger.info("Starting bot...")
    app.run_polling()

if __name__ == "__main__":
    main()
