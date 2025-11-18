# bot_with_ref.py
import os
import json
import logging
import asyncio
import requests

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Read env vars at import time
BOT_TOKEN = os.environ.get("8213937413:AAHmp7SHCITYExufiYvQtEJJbZP7Svi4Uwg")  # <-- ঠিক করা: ENV key "BOT_TOKEN"
API_BASE = os.environ.get("API_BASE", "http://127.0.0.1:5000")
REF_SECRET = os.environ.get("REF_SECRET")  # optional

async def post_referral_async(payload):
    """Post referral in a thread to avoid blocking asyncio loop."""
    headers = {"Content-Type": "application/json"}
    if REF_SECRET:
        headers["X-REF-SECRET"] = REF_SECRET

    loop = asyncio.get_running_loop()

    def do_post():
        try:
            url = API_BASE.rstrip('/') + "/api/referral/register"
            r = requests.post(url, json=payload, headers=headers, timeout=8)
            return r.status_code, r.text
        except Exception as e:
            logger.exception("Error posting referral")
            return None, str(e)

    return await loop.run_in_executor(None, do_post)

def extract_start_param(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Extract parameter passed to /start.
    Supports: /start <payload> and deep links (start=...)
    """
    # context.args works for /start <payload>
    if context.args:
        return context.args[0]

    # fallback to message text (if present)
    msg = getattr(update, "message", None) or getattr(update, "effective_message", None)
    if msg and getattr(msg, "text", None):
        parts = msg.text.split()
        if len(parts) > 1:
            return parts[1]
    return None

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # use effective_user / effective_message for robustness
    user = update.effective_user
    message = update.effective_message

    first_name = (user.first_name or "user") if user else "user"
    await message.reply_text(f"স্বাগতম, {first_name}! Processing start...")

    start_param = extract_start_param(update, context)
    if start_param and start_param.startswith("ref"):
        referrer_id = start_param.replace("ref", "").strip()
        new_user_id = str(user.id) if user else "unknown"
        payload = {
            "newUserId": new_user_id,
            "referrerId": str(referrer_id),
            "first_name": user.first_name if user else None,
            "last_name": user.last_name if user else None,
            "username": user.username if user else None,
        }

        status, text = await post_referral_async(payload)
        if status == 200:
            try:
                j = json.loads(text)
                if j.get("success"):
                    if j.get("credited"):
                        bal = j.get("referrerBalanceCents", 0) / 100.0
                        cnt = j.get("referrerReferralCount", 0)
                        await message.reply_text(
                            f"Referral verified — referrer credited. Referrer new balance: USDT {bal:.2f} (refs: {cnt})"
                        )
                    else:
                        await message.reply_text("Referral recorded earlier (no new credit).")
                else:
                    # API returned success=False
                    err_msg = j.get("error") or "Referral API returned error."
                    await message.reply_text(f"Referral API error: {err_msg}")
            except Exception:
                logger.exception("Failed to parse referral server response")
                await message.reply_text("Bad response from referral server.")
        else:
            await message.reply_text(f"Failed to contact referral server: {text}")
    else:
        await message.reply_text("No referral parameter found in /start.")

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    await message.reply_text("This bot supports referral links. Use t.me/YourBot?start=ref<your_id> to share.")

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set in environment variables. Set BOT_TOKEN and restart bot.")
        raise SystemExit("Set BOT_TOKEN environment variable and retry.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))

    logger.info("Starting bot (polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
