# bot_with_ref.py
import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.environ.get("8213937413:AAHmp7SHCITYExufiYvQtEJJbZP7Svi4Uwg")
API_BASE = os.environ.get("API_BASE", "https://your-server.example.com")  # আপনার Flask সার্ভারের URL
REF_SECRET = os.environ.get("REF_SECRET")  # যদি আপনি সার্ভারে সিক্রেট হেডার চেক করেন

REF_REGISTER_ENDPOINT = f"{API_BASE.rstrip('/')}/api/referral/register"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    CommandHandler /start
    context.args will contain words after /start
    Example: user clicks t.me/yourbot?start=ref123 -> context.args == ['ref123']
    """
    user = update.effective_user
    name = user.first_name or "User"

    # get start param (if any)
    start_args = context.args  # list of tokens passed to /start
    msg_lines = [f"স্বাগতম {name}! 👋"]

    if start_args:
        # Example: start_args == ['ref123'] or ['ref123xyz']
        param = start_args[0]
        msg_lines.append(f"আপনি একটি রেফারাল লিংক দিয়ে এসেছেন: {param}")

        # try to extract ref id if it starts with 'ref'
        referrer_id = None
        if param.startswith("ref"):
            referrer_id = param.replace("ref", "").strip()

        # call your backend to register referral (optional, recommended)
        if referrer_id:
            payload = {
                "newUserId": str(user.id),
                "referrerId": str(referrer_id),
                "first_name": user.first_name,
                "last_name": user.last_name,
                "username": user.username
            }
            headers = {"Content-Type": "application/json"}
            if REF_SECRET:
                headers["X-REF-SECRET"] = REF_SECRET
            try:
                resp = requests.post(REF_REGISTER_ENDPOINT, json=payload, headers=headers, timeout=6)
                j = resp.json() if resp.status_code == 200 else {"success": False}
                if j.get("success") and j.get("credited"):
                    cents = j.get("referrerBalanceCents", 0)
                    rcnt = j.get("referrerReferralCount", 0)
                    msg_lines.append("রেফারাল সফলভাবে রেকর্ড করা হয়েছে — ধন্যবাদ!")
                    msg_lines.append(f"আপনার রেফারারকে কমিশন দেওয়া হয়েছে। (রেফারার মোট রেফার: {rcnt})")
                else:
                    msg_lines.append("রেফারাল বিবরণ সার্ভারে রেকর্ড করা হয়েছে অথবা ইতিমধ্যে প্রক্রিয়াকৃত।")
            except Exception as e:
                # সার্ভারে কোনো সমস্যায় লগিং করে ব্যবহারকারীকে অনুগ্রহ করে জানিয়ে দিন
                print("Referral register error:", e)
                msg_lines.append("রেফারাল রেজিস্ট্রেশনে সার্ভার ত্রুটি হয়েছে — পরে চেষ্টা করুন।")
        else:
            msg_lines.append("রেফারাল প্যারাম থেকে referrer ID পাওয়া যায়নি।")
    else:
        msg_lines.append("এই বটের সাহায্যে আপনি অ্যাড দেখে আয় ও রেফারাল বোনাস পেতে পারবেন।")

    await update.message.reply_text("\n".join(msg_lines))

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    print("Bot started (polling)...")
    app.run_polling()
