import os, logging
import httpx
from telegram import (Update, ReplyKeyboardMarkup,
    InlineKeyboardMarkup, InlineKeyboardButton)
from telegram.ext import (Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters)

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["TOKEN"]
REDIS_URL = os.environ["UPSTASH_URL"].rstrip("/")
REDIS_TOKEN = os.environ["UPSTASH_TOKEN"]
BASE_URL = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", 10000))

BUTTON = "Acai time 🍇"
MENU = ReplyKeyboardMarkup([[BUTTON]], resize_keyboard=True)

# --- member list, stored in free Upstash Redis so it survives sleeping ---
async def redis(*cmd):
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(REDIS_URL,
            headers={"Authorization": f"Bearer {REDIS_TOKEN}"},
            json=[str(x) for x in cmd])
        r.raise_for_status()
        return r.json()["result"]

async def add_member(chat_id):
    await redis("SADD", "members", chat_id)

async def get_members():
    return [int(x) for x in await redis("SMEMBERS", "members")]

async def remove_member(chat_id):
    await redis("SREM", "members", chat_id)

# --- bot ---
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await add_member(update.effective_chat.id)
    await update.message.reply_text(
        "You're in the acai crew 🍇", reply_markup=MENU)

async def button_pressed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await add_member(update.effective_chat.id)
    ctx.user_data["awaiting_date"] = True
    await update.message.reply_text("What date? (e.g. Thursday 22 Oct)")

async def got_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_date"):
        return
    ctx.user_data["awaiting_date"] = False
    ctx.user_data["date"] = update.message.text
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, send it ✅", callback_data="yes"),
        InlineKeyboardButton("Cancel ❌", callback_data="no"),
    ]])
    await update.message.reply_text(
        f"Invite everyone for {update.message.text}?",
        reply_markup=keyboard)

async def confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    date = ctx.user_data.pop("date", None)
    if q.data != "yes" or not date:
        await q.edit_message_text("Cancelled.")
        return

    user = q.from_user
    name = f"@{user.username}" if user.username else user.first_name
    msg = f"{name} says: Acai time? ({date}) 🍇"

    sent = 0
    for chat_id in await get_members():
        try:
            await ctx.bot.send_message(chat_id, msg)
            sent += 1
        except Exception:
            await remove_member(chat_id)  # blocked the bot
    await q.edit_message_text(f"Invite sent to {sent} people 🍇")

app = Application.builder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.Text([BUTTON]), button_pressed))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, got_date))
app.add_handler(CallbackQueryHandler(confirm))

print("Acai bot starting (webhook mode)...", flush=True)
app.run_webhook(
    listen="0.0.0.0",
    port=PORT,
    url_path=TOKEN,
    webhook_url=f"{BASE_URL}/{TOKEN}",
)
