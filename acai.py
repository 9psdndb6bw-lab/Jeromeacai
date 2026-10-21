import os, time, logging
import httpx
from telegram import (BotCommand, BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats, Update, ReplyKeyboardMarkup,
    InlineKeyboardMarkup, InlineKeyboardButton)
from telegram.ext import (Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ChatMemberHandler, ContextTypes, filters)

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["TOKEN"]
REDIS_URL = os.environ["UPSTASH_URL"].rstrip("/")
REDIS_TOKEN = os.environ["UPSTASH_TOKEN"]
BASE_URL = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", 10000))

BUTTON = "Acai time 🍇"
MEMBERS_BUTTON = "Members 👥"
MENU = ReplyKeyboardMarkup([[BUTTON, MEMBERS_BUTTON]], resize_keyboard=True)
EXPIRE = 60 * 60 * 24 * 30  # forget invites after 30 days

# ---------- storage (free Upstash Redis) ----------
async def redis(*cmd):
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(REDIS_URL,
            headers={"Authorization": f"Bearer {REDIS_TOKEN}"},
            json=[str(x) for x in cmd])
        r.raise_for_status()
        return r.json()["result"]

async def hgetall(key):
    flat = await redis("HGETALL", key) or []
    return dict(zip(flat[0::2], flat[1::2]))

async def add_member(chat_id):
    await redis("SADD", "members", chat_id)

async def remove_member(chat_id):
    await redis("SREM", "members", chat_id)

async def add_group(chat_id):
    await redis("SADD", "groups", chat_id)

async def remove_group(chat_id):
    await redis("SREM", "groups", chat_id)
    await redis("SREM", "members", chat_id)  # clean up older versions

async def split_chats():
    """People = private chats. Groups = group chats (negative ids)."""
    ids = [int(x) for x in await redis("SMEMBERS", "members")]
    gids = [int(x) for x in await redis("SMEMBERS", "groups")]
    people = [i for i in ids if i > 0]
    groups = list(set(gids) | {i for i in ids if i < 0})
    return people, groups

async def chat_name(bot, chat_id):
    chat = await bot.get_chat(chat_id)
    return f"@{chat.username}" if chat.username else (chat.first_name or "Someone")

# ---------- helpers ----------
def display_name(user):
    return f"@{user.username}" if user.username else (user.first_name or "Someone")

def rsvp_keyboard(invite_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("I'm in 🍇", callback_data=f"rsvp:{invite_id}:yes"),
        InlineKeyboardButton("Can't make it ❌", callback_data=f"rsvp:{invite_id}:no"),
    ]])

def render(meta, names, resp):
    order = {"yes": 0, "no": 1, "waiting": 2}
    icons = {"yes": "✅", "no": "❌", "waiting": "⏳"}
    words = {"yes": "accepted", "no": "declined", "waiting": "waiting for answer"}
    rows = sorted(
        ((resp.get(cid, "waiting"), name) for cid, name in names.items()),
        key=lambda x: (order[x[0]], x[1].lower()))
    lines = [f"{icons[s]} {n}: {words[s]}" for s, n in rows]
    return (f"{meta['inviter']} says: Acai time? ({meta['date']}) 🍇\n\n"
            + "\n".join(lines))

# ---------- bot flow ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await add_member(update.effective_chat.id)
    await update.message.reply_text(
        "You're in the acai crew 🍇", reply_markup=MENU)

async def group_membership(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Runs when the bot is added to / removed from a group."""
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return
    status = update.my_chat_member.new_chat_member.status
    if status in ("member", "administrator"):
        await add_group(chat.id)
    else:  # left or kicked
        await remove_group(chat.id)

async def members_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    people, groups = await split_chats()
    names = []
    for chat_id in people:
        try:
            names.append(await chat_name(ctx.bot, chat_id))
        except Exception:
            await remove_member(chat_id)
    if not names:
        await update.message.reply_text("Nobody's in the crew yet.", reply_markup=MENU)
        return
    names.sort(key=str.lower)
    text = f"Acai crew ({len(names)}) 🍇\n" + "\n".join(f"• {n}" for n in names)
    if groups:
        text += f"\n\n+ {len(groups)} group(s) following along"
    await update.message.reply_text(text, reply_markup=MENU)

async def button_pressed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await add_member(update.effective_chat.id)
    ctx.user_data["awaiting_date"] = True
    await update.message.reply_text(
        "What date? (e.g. Thursday 22 Oct)", reply_markup=MENU)

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

    sender_id = q.from_user.id
    inviter = display_name(q.from_user)
    invite_id = str(int(time.time()))

    # work out everyone's name
    names = {}
    people, groups = await split_chats()
    for chat_id in people:
        try:
            names[str(chat_id)] = await chat_name(ctx.bot, chat_id)
        except Exception:
            await remove_member(chat_id)
    names[str(sender_id)] = inviter

    # the person who started the invite is already in
    resp = {str(sender_id): "yes"}
    meta = {"date": date, "inviter": inviter}
    text = render(meta, names, resp)

    msgs = {}
    for chat_id in [int(c) for c in names]:
        try:
            m = await ctx.bot.send_message(
                chat_id, text, reply_markup=rsvp_keyboard(invite_id))
            msgs[str(chat_id)] = m.message_id
        except Exception:
            await remove_member(chat_id)

    # groups: show the same list, but they don't count as members
    gmsgs = {}
    for chat_id in groups:
        try:
            m = await ctx.bot.send_message(chat_id, text)
            gmsgs[str(chat_id)] = m.message_id
        except Exception:
            await remove_group(chat_id)

    # save the invite
    p = f"invite:{invite_id}"
    await redis("HSET", f"{p}:meta", "date", date, "inviter", inviter)
    for field, data in ((":names", names), (":resp", resp),
                        (":msgs", msgs), (":gmsgs", gmsgs)):
        for k, v in data.items():
            await redis("HSET", p + field, k, v)
    for field in (":meta", ":names", ":resp", ":msgs", ":gmsgs"):
        await redis("EXPIRE", p + field, EXPIRE)

    extra = f" + {len(gmsgs)} group(s)" if gmsgs else ""
    await q.edit_message_text(f"Invite sent to {len(msgs)} people{extra} 🍇")

async def rsvp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, invite_id, answer = q.data.split(":")
    p = f"invite:{invite_id}"

    meta = await hgetall(f"{p}:meta")
    if not meta:
        await q.answer("This invite has expired.")
        return

    await redis("HSET", f"{p}:resp", q.from_user.id, answer)
    await q.answer("Got it! 🍇" if answer == "yes" else "Noted")

    names = await hgetall(f"{p}:names")
    resp = await hgetall(f"{p}:resp")
    msgs = await hgetall(f"{p}:msgs")
    gmsgs = await hgetall(f"{p}:gmsgs")
    text = render(meta, names, resp)

    for chat_id, message_id in gmsgs.items():
        try:
            await ctx.bot.edit_message_text(
                text, chat_id=int(chat_id), message_id=int(message_id))
        except Exception:
            pass

    for chat_id, message_id in msgs.items():
        try:
            await ctx.bot.edit_message_text(
                text, chat_id=int(chat_id), message_id=int(message_id),
                reply_markup=rsvp_keyboard(invite_id))
        except Exception:
            pass  # e.g. message unchanged or chat gone

# ---------- wiring ----------
async def setup(application):
    # Command shortcuts only in private chats, none in groups
    await application.bot.delete_my_commands()  # default list (e.g. from BotFather)
    await application.bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
    await application.bot.set_my_commands(
        [BotCommand("members", "See who's in the crew")],
        scope=BotCommandScopeAllPrivateChats())

app = Application.builder().token(TOKEN).post_init(setup).build()
app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
app.add_handler(CommandHandler("members", members_cmd, filters=filters.ChatType.PRIVATE))
app.add_handler(ChatMemberHandler(group_membership, ChatMemberHandler.MY_CHAT_MEMBER))
app.add_handler(MessageHandler(
    filters.ChatType.PRIVATE & filters.Text([BUTTON]), button_pressed))
app.add_handler(MessageHandler(
    filters.ChatType.PRIVATE & filters.Text([MEMBERS_BUTTON]), members_cmd))
app.add_handler(MessageHandler(
    filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, got_date))
app.add_handler(CallbackQueryHandler(rsvp, pattern=r"^rsvp:"))
app.add_handler(CallbackQueryHandler(confirm, pattern=r"^(yes|no)$"))

print("Acai bot starting (webhook mode)...", flush=True)
app.run_webhook(
    listen="0.0.0.0",
    port=PORT,
    url_path=TOKEN,
    webhook_url=f"{BASE_URL}/{TOKEN}",
    allowed_updates=Update.ALL_TYPES,
)
