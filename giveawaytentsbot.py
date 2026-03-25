import os
import json
import random
import logging
import asyncio
from datetime import datetime, timedelta, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================
BOT_TOKEN = "8669321299:AAGGhwAMBNTmwhWBvnF40LpJDf1BYoI3YR8"
ADMIN_ID = 7691071175
ALLOWED_GROUP_ID = -1003730637965

DATA_DIR = "data"
DB_FILE = os.path.join(DATA_DIR, "db.json")
os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("giveaway-bot")

db_lock = asyncio.Lock()

# =========================================================
# STATES
# =========================================================
(
    TITLE,
    BANNER,
    PRIZE,
    HOSTED_BY,
    WINNERS,
    CHANNEL,
    DURATION,
) = range(7)

# =========================================================
# DATABASE
# =========================================================
def default_db():
    return {
        "giveaways": {},
        "activity": {},
        "history": [],
        "users": {}
    }

def ensure_db_structure(data):
    if not isinstance(data, dict):
        data = {}

    if "giveaways" not in data or not isinstance(data["giveaways"], dict):
        data["giveaways"] = {}

    if "activity" not in data or not isinstance(data["activity"], dict):
        data["activity"] = {}

    if "history" not in data or not isinstance(data["history"], list):
        data["history"] = []

    if "users" not in data or not isinstance(data["users"], dict):
        data["users"] = {}

    return data

def load_db():
    if not os.path.exists(DB_FILE):
        db = default_db()
        save_db(db)
        return db

    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
    except Exception:
        db = default_db()

    db = ensure_db_structure(db)
    save_db(db)
    return db

def save_db(db):
    db = ensure_db_structure(db)
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

def now_utc():
    return datetime.now(timezone.utc)

def gen_giveaway_id():
    return str(int(datetime.now().timestamp() * 1000))

def is_admin(user_id: int):
    return user_id == ADMIN_ID

def save_user_info(user):
    db = load_db()
    db = ensure_db_structure(db)

    db["users"][str(user.id)] = {
        "id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }

    save_db(db)

def get_user_info(user_id):
    db = load_db()
    return db.get("users", {}).get(str(user_id), {})

def get_active_giveaways():
    db = load_db()
    return {gid: g for gid, g in db["giveaways"].items() if g.get("active")}

def get_giveaway(giveaway_id):
    db = load_db()
    return db["giveaways"].get(giveaway_id)

def save_giveaway(giveaway):
    db = load_db()
    db["giveaways"][giveaway["id"]] = giveaway
    save_db(db)

def add_history(giveaway):
    db = load_db()
    db["history"] = [x for x in db["history"] if x.get("id") != giveaway["id"]]
    db["history"].append(giveaway)
    save_db(db)

# =========================================================
# HELPERS
# =========================================================
def format_time_left(end_iso):
    end_time = datetime.fromisoformat(end_iso)
    diff = end_time - now_utc()
    total = int(diff.total_seconds())

    if total <= 0:
        return "0d 0h 0m"

    days = total // 86400
    hours = (total % 86400) // 3600
    mins = (total % 3600) // 60
    return f"{days}d {hours}h {mins}m"

def user_mention(user_id):
    info = get_user_info(user_id)
    username = info.get("username")
    first_name = info.get("first_name") or "User"

    if username:
        return f"@{username}"
    return f"<a href='tg://user?id={user_id}'>{first_name}</a>"

def winners_text(winners):
    if not winners:
        return "<b>❌ No valid winners found.</b>"

    medals = ["🥇", "🥈", "🥉"]
    lines = []

    for i, uid in enumerate(winners, start=1):
        medal = medals[i - 1] if i <= 3 else "🏆"
        lines.append(f"<b>{medal} Winner {i}:</b> {user_mention(uid)}")

    return "\n".join(lines)

def build_caption(g):
    entries = len(g.get("participants", []))
    return (
        f"<b>🎁 Prize:</b> {g['prize']}\n\n"
        f"<b>👤 Hosted By:</b> {g['hosted_by']}\n\n"
        f"<b>⭐ Giveaway Conditions:</b>\n"
        f"<b>• Must be a member of {g['required_channel']}</b>\n\n"
        f"<b>🏆 Winners:</b> {g['winners_count']}\n\n"
        f"<b>✅ Entries:</b> {entries}\n\n"
        f"<b>⏳ Giveaway Ends In:</b> {format_time_left(g['end_time'])}\n\n"
        f"<b>To participate in the giveaway, press the button below!</b>"
    )

def build_group_keyboard(g):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{g['required_channel'].replace('@', '')}")],
        [InlineKeyboardButton("🎉 Participate", callback_data=f"participate|{g['id']}")]
    ])

def build_admin_manage_keyboard(giveaway_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Status", callback_data=f"status|{giveaway_id}")],
        [InlineKeyboardButton("🎲 Reroll Winner", callback_data=f"reroll|{giveaway_id}")],
        [InlineKeyboardButton("🛑 Cancel Giveaway", callback_data=f"cancel_giveaway|{giveaway_id}")]
    ])

async def only_admin_dm(update: Update):
    if not update.effective_user or not update.effective_chat:
        return False
    return is_admin(update.effective_user.id) and update.effective_chat.type == "private"

# =========================================================
# HARD FIXED CHANNEL CHECK
# =========================================================
async def is_user_in_required_channel(context: ContextTypes.DEFAULT_TYPE, channel_username: str, user_id: int):
    try:
        channel_username = channel_username.strip()

        if not channel_username.startswith("@"):
            channel_username = "@" + channel_username

        member = await context.bot.get_chat_member(chat_id=channel_username, user_id=user_id)

        status = member.status

        # Valid statuses = joined
        if status in [
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
            ChatMemberStatus.RESTRICTED,
            "member",
            "administrator",
            "creator",
            "restricted",
        ]:
            return True

        if status in [
            ChatMemberStatus.LEFT,
            ChatMemberStatus.BANNED,
            "left",
            "kicked",
        ]:
            return False

        return False

    except Forbidden:
        logger.warning(f"Bot cannot access required channel {channel_username}. Make bot admin there.")
        return False
    except Exception as e:
        logger.warning(f"Membership check failed for user {user_id} in {channel_username}: {e}")
        return False

# =========================================================
# SAFE EDIT
# =========================================================
async def safe_edit_caption(context: ContextTypes.DEFAULT_TYPE, g):
    try:
        await context.bot.edit_message_caption(
            chat_id=g["group_id"],
            message_id=g["group_message_id"],
            caption=build_caption(g),
            parse_mode=ParseMode.HTML,
            reply_markup=build_group_keyboard(g)
        )
    except BadRequest as e:
        msg = str(e).lower()
        if "message is not modified" in msg:
            return
        logger.warning(f"Edit caption bad request: {e}")
    except Exception as e:
        logger.warning(f"Edit caption failed: {e}")

# =========================================================
# START
# =========================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await only_admin_dm(update):
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Manage Giveaways", callback_data="manage_panel")],
        [InlineKeyboardButton("📜 History", callback_data="history_panel")]
    ])

    await update.message.reply_text(
        "<b>👋 Giveaway Bot Admin Panel</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )

# =========================================================
# ADMIN PANEL
# =========================================================
async def panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    if not is_admin(query.from_user.id):
        return

    data = query.data or ""

    try:
        await query.answer()
    except:
        pass

    if data == "manage_panel":
        active = get_active_giveaways()
        text = f"<b>🎛 Giveaway Manager</b>\n\n<b>Active Giveaways:</b> {len(active)}"

        keyboard = [[InlineKeyboardButton("➕ Create Giveaway", callback_data="create_giveaway")]]

        for gid, g in active.items():
            keyboard.append([
                InlineKeyboardButton(f"🎁 {g['prize'][:25]}", callback_data=f"open_manage|{gid}")
            ])

        await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "history_panel":
        db = load_db()
        history = db.get("history", [])

        if not history:
            await query.message.reply_text("<b>📜 No giveaway history found.</b>", parse_mode=ParseMode.HTML)
            return

        lines = []
        for g in history[-10:]:
            lines.append(f"<b>• {g['prize']} | {g.get('status', 'ended')} | Entries: {len(g.get('participants', []))}</b>")

        await query.message.reply_text("<b>📜 Last 10 Giveaway History</b>\n\n" + "\n".join(lines), parse_mode=ParseMode.HTML)

    elif data.startswith("open_manage|"):
        gid = data.split("|")[1]
        g = get_giveaway(gid)

        if not g:
            await query.message.reply_text("<b>❌ Giveaway not found.</b>", parse_mode=ParseMode.HTML)
            return

        await query.message.reply_text(
            f"<b>🎁 Giveaway Manage</b>\n\n<b>Prize:</b> {g['prize']}",
            parse_mode=ParseMode.HTML,
            reply_markup=build_admin_manage_keyboard(gid)
        )

    elif data.startswith("status|"):
        gid = data.split("|")[1]
        g = get_giveaway(gid)

        if not g:
            await query.message.reply_text("<b>❌ Giveaway not found.</b>", parse_mode=ParseMode.HTML)
            return

        await query.message.reply_text(
            f"<b>📊 Giveaway Status</b>\n\n"
            f"<b>Prize:</b> {g['prize']}\n"
            f"<b>Hosted By:</b> {g['hosted_by']}\n"
            f"<b>Winners:</b> {g['winners_count']}\n"
            f"<b>Entries:</b> {len(g.get('participants', []))}\n"
            f"<b>Required Channel:</b> {g['required_channel']}\n"
            f"<b>Time Left:</b> {format_time_left(g['end_time'])}\n",
            parse_mode=ParseMode.HTML
        )

    elif data.startswith("cancel_giveaway|"):
        gid = data.split("|")[1]
        g = get_giveaway(gid)

        if not g:
            await query.message.reply_text("<b>❌ Giveaway not found.</b>", parse_mode=ParseMode.HTML)
            return

        g["active"] = False
        g["status"] = "cancelled"
        save_giveaway(g)
        add_history(g)

        await query.message.reply_text("<b>🛑 Giveaway cancelled successfully.</b>", parse_mode=ParseMode.HTML)

    elif data.startswith("reroll|"):
        gid = data.split("|")[1]
        g = get_giveaway(gid)

        if not g:
            await query.message.reply_text("<b>❌ Giveaway not found.</b>", parse_mode=ParseMode.HTML)
            return

        winners = pick_weighted_winners(g)

        await query.message.reply_text(
            f"<b>🎲 Rerolled Winners</b>\n\n{winners_text(winners)}",
            parse_mode=ParseMode.HTML
        )

# =========================================================
# CREATE FLOW
# =========================================================
async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return ConversationHandler.END

    if not is_admin(query.from_user.id):
        return ConversationHandler.END

    try:
        await query.answer()
    except:
        pass

    context.user_data.clear()
    await query.message.reply_text("<b>Send giveaway title:</b>", parse_mode=ParseMode.HTML)
    return TITLE

async def title_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await only_admin_dm(update):
        return ConversationHandler.END

    context.user_data["title"] = update.message.text
    await update.message.reply_text("<b>Send banner photo:</b>", parse_mode=ParseMode.HTML)
    return BANNER

async def banner_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await only_admin_dm(update):
        return ConversationHandler.END

    if not update.message.photo:
        await update.message.reply_text("<b>❌ Send a photo.</b>", parse_mode=ParseMode.HTML)
        return BANNER

    context.user_data["banner_file_id"] = update.message.photo[-1].file_id
    await update.message.reply_text("<b>Send prize:</b>", parse_mode=ParseMode.HTML)
    return PRIZE

async def prize_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await only_admin_dm(update):
        return ConversationHandler.END

    context.user_data["prize"] = update.message.text
    await update.message.reply_text("<b>Send hosted by name:</b>", parse_mode=ParseMode.HTML)
    return HOSTED_BY

async def hosted_by_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await only_admin_dm(update):
        return ConversationHandler.END

    context.user_data["hosted_by"] = update.message.text
    await update.message.reply_text("<b>How many winners? (1,2,3...)</b>", parse_mode=ParseMode.HTML)
    return WINNERS

async def winners_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await only_admin_dm(update):
        return ConversationHandler.END

    try:
        count = int(update.message.text)
        if count < 1:
            raise ValueError
    except:
        await update.message.reply_text("<b>❌ Send a valid number.</b>", parse_mode=ParseMode.HTML)
        return WINNERS

    context.user_data["winners_count"] = count
    await update.message.reply_text("<b>Send required channel username (example: @mychannel):</b>", parse_mode=ParseMode.HTML)
    return CHANNEL

async def channel_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await only_admin_dm(update):
        return ConversationHandler.END

    ch = update.message.text.strip()
    if not ch.startswith("@"):
        ch = "@" + ch

    context.user_data["required_channel"] = ch
    await update.message.reply_text("<b>Send duration in minutes (example: 60):</b>", parse_mode=ParseMode.HTML)
    return DURATION

async def duration_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await only_admin_dm(update):
        return ConversationHandler.END

    try:
        mins = int(update.message.text)
        if mins < 1:
            raise ValueError
    except:
        await update.message.reply_text("<b>❌ Send valid minutes.</b>", parse_mode=ParseMode.HTML)
        return DURATION

    gid = gen_giveaway_id()
    end_time = now_utc() + timedelta(minutes=mins)

    giveaway = {
        "id": gid,
        "active": True,
        "status": "active",
        "title": context.user_data["title"],
        "banner_file_id": context.user_data["banner_file_id"],
        "prize": context.user_data["prize"],
        "hosted_by": context.user_data["hosted_by"],
        "winners_count": context.user_data["winners_count"],
        "required_channel": context.user_data["required_channel"],
        "group_id": ALLOWED_GROUP_ID,
        "participants": [],
        "end_time": end_time.isoformat(),
        "created_at": now_utc().isoformat(),
        "group_message_id": None,
        "last_repost_date": now_utc().date().isoformat()
    }

    sent = await context.bot.send_photo(
        chat_id=ALLOWED_GROUP_ID,
        photo=giveaway["banner_file_id"],
        caption=build_caption(giveaway),
        parse_mode=ParseMode.HTML,
        reply_markup=build_group_keyboard(giveaway)
    )

    giveaway["group_message_id"] = sent.message_id
    save_giveaway(giveaway)

    try:
        await context.bot.unpin_all_chat_messages(chat_id=ALLOWED_GROUP_ID)
    except:
        pass

    try:
        await context.bot.pin_chat_message(
            chat_id=ALLOWED_GROUP_ID,
            message_id=sent.message_id,
            disable_notification=True
        )
    except Exception as e:
        logger.warning(f"Pin failed: {e}")

    await update.message.reply_text(
        f"<b>✅ Giveaway created successfully!</b>\n\n<b>ID:</b> {gid}",
        parse_mode=ParseMode.HTML,
        reply_markup=build_admin_manage_keyboard(gid)
    )

    context.user_data.clear()
    return ConversationHandler.END

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("<b>❌ Giveaway setup cancelled.</b>", parse_mode=ParseMode.HTML)
    return ConversationHandler.END

# =========================================================
# PARTICIPATE FIXED
# =========================================================
async def participate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        return

    user = query.from_user
    data = query.data or ""

    if not data.startswith("participate|"):
        try:
            await query.answer("Invalid giveaway button.", show_alert=True)
        except:
            pass
        return

    giveaway_id = data.split("|", 1)[1]

    if query.message.chat.id != ALLOWED_GROUP_ID:
        try:
            await query.answer("This button works only in giveaway group.", show_alert=True)
        except:
            pass
        return

    try:
        async with db_lock:
            db = load_db()
            db = ensure_db_structure(db)

            g = db["giveaways"].get(giveaway_id)

            if not g:
                await query.answer("Giveaway not found.", show_alert=True)
                return

            if not g.get("active"):
                await query.answer("This giveaway has ended.", show_alert=True)
                return

            db["users"][str(user.id)] = {
                "id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name
            }

            joined = await is_user_in_required_channel(context, g["required_channel"], user.id)
            if not joined:
                save_db(db)
                await query.answer("Join required channel first.", show_alert=True)
                return

            if "participants" not in g or not isinstance(g["participants"], list):
                g["participants"] = []

            if user.id in g["participants"]:
                save_db(db)
                await query.answer("Already joined this giveaway.", show_alert=True)
                return

            g["participants"].append(user.id)
            db["giveaways"][giveaway_id] = g
            save_db(db)

        await safe_edit_caption(context, g)

        try:
               await query.answer("Joined Successfully!", show_alert=True)
        except:
            pass

        logger.info(f"User {user.id} joined giveaway {giveaway_id}")

    except Exception as e:
        logger.exception(f"Participate callback crashed: {e}")
        try:
            await query.answer("Temporary error. Try again.", show_alert=True)
        except:
            pass

# =========================================================
# TRACK ACTIVITY
# =========================================================
async def track_group_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if update.effective_chat.id != ALLOWED_GROUP_ID:
        return

    user = update.effective_user
    if not user or user.is_bot:
        return

    save_user_info(user)

    db = load_db()
    db = ensure_db_structure(db)

    group_key = str(ALLOWED_GROUP_ID)
    if group_key not in db["activity"]:
        db["activity"][group_key] = {}

    user_key = str(user.id)
    db["activity"][group_key][user_key] = db["activity"][group_key].get(user_key, 0) + 1
    save_db(db)

# =========================================================
# WINNER PICK
# =========================================================
def pick_weighted_winners(giveaway):
    db = load_db()
    participants = giveaway.get("participants", [])
    activity = db["activity"].get(str(ALLOWED_GROUP_ID), {})

    valid = []
    for uid in participants:
        msg_count = activity.get(str(uid), 0)
        weight = max(1, msg_count + 1)
        valid.extend([uid] * weight)

    if not valid:
        return []

    unique_participants = list(set(participants))
    winner_count = min(giveaway["winners_count"], len(unique_participants))

    winners = []
    attempts = 0
    while len(winners) < winner_count and attempts < 10000:
        picked = random.choice(valid)
        if picked not in winners:
            winners.append(picked)
        attempts += 1

    return winners

# =========================================================
# CLEAN LEAVERS
# =========================================================
async def clean_invalid_participants(context: ContextTypes.DEFAULT_TYPE, g):
    if not g.get("active"):
        return g

    participants = g.get("participants", [])
    if not participants:
        return g

    valid_users = []
    changed = False

    for uid in participants:
        ok = await is_user_in_required_channel(context, g["required_channel"], uid)
        if ok:
            valid_users.append(uid)
        else:
            changed = True

    if changed:
        g["participants"] = valid_users
        save_giveaway(g)

    return g

# =========================================================
# DAILY REPOST
# =========================================================
async def repost_daily_if_needed(context: ContextTypes.DEFAULT_TYPE, g):
    if not g.get("active"):
        return g

    today = now_utc().date().isoformat()
    if g.get("last_repost_date") == today:
        return g

    try:
        old_message_id = g.get("group_message_id")
        if old_message_id:
            try:
                await context.bot.delete_message(chat_id=g["group_id"], message_id=old_message_id)
            except:
                pass

        sent = await context.bot.send_photo(
            chat_id=g["group_id"],
            photo=g["banner_file_id"],
            caption=build_caption(g),
            parse_mode=ParseMode.HTML,
            reply_markup=build_group_keyboard(g)
        )

        g["group_message_id"] = sent.message_id
        g["last_repost_date"] = today
        save_giveaway(g)

        try:
            await context.bot.unpin_all_chat_messages(chat_id=g["group_id"])
        except:
            pass

        try:
            await context.bot.pin_chat_message(
                chat_id=g["group_id"],
                message_id=sent.message_id,
                disable_notification=True
            )
        except:
            pass

    except Exception as e:
        logger.warning(f"Daily repost failed: {e}")

    return g

# =========================================================
# PERIODIC TASKS
# =========================================================
async def periodic_tasks(context: ContextTypes.DEFAULT_TYPE):
    active = get_active_giveaways()

    for gid, g in active.items():
        if not g.get("active"):
            continue

        end_time = datetime.fromisoformat(g["end_time"])

        if now_utc() >= end_time:
            winners = pick_weighted_winners(g)
            result_text = winners_text(winners)

            final_caption = (
                f"<b>🎉 Giveaway Ended</b>\n\n"
                f"<b>🎁 Prize:</b> {g['prize']}\n\n"
                f"<b>👤 Hosted By:</b> {g['hosted_by']}\n\n"
                f"<b>🏆 Winners:</b> {g['winners_count']}\n\n"
                f"<b>✅ Entries:</b> {len(g.get('participants', []))}\n\n"
                f"{result_text}"
            )

            try:
                await context.bot.edit_message_caption(
                    chat_id=g["group_id"],
                    message_id=g["group_message_id"],
                    caption=final_caption,
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.warning(f"Final update failed: {e}")

            try:
                await context.bot.send_message(
                    chat_id=g["group_id"],
                    text=f"<b>🎊 Giveaway Results</b>\n\n{result_text}",
                    parse_mode=ParseMode.HTML
                )
            except:
                pass

            g["active"] = False
            g["status"] = "ended"
            g["winners"] = winners
            save_giveaway(g)
            add_history(g)
            continue

        g = await clean_invalid_participants(context, g)
        g = await repost_daily_if_needed(context, g)
        await safe_edit_caption(context, g)

# =========================================================
# MAIN
# =========================================================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(CallbackQueryHandler(panel_callback, pattern="^(manage_panel|history_panel)$"))
    app.add_handler(CallbackQueryHandler(panel_callback, pattern="^(open_manage\\|.*|status\\|.*|cancel_giveaway\\|.*|reroll\\|.*)$"))

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(create_start, pattern="^create_giveaway$")],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, title_step)],
            BANNER: [MessageHandler(filters.PHOTO, banner_step)],
            PRIZE: [MessageHandler(filters.TEXT & ~filters.COMMAND, prize_step)],
            HOSTED_BY: [MessageHandler(filters.TEXT & ~filters.COMMAND, hosted_by_step)],
            WINNERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, winners_step)],
            CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, channel_step)],
            DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, duration_step)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        per_message=False
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(participate_callback, pattern="^participate\\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, track_group_activity))

    app.job_queue.run_repeating(periodic_tasks, interval=10, first=10)

    print("Giveaway Bot is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()