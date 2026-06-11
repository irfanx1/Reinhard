"""
bot.py — AnimeNewsBot

Every user command is guarded by:
  1. Ban check   — banned users see nothing
  2. FSUB check  — must join the configured channel first

Commands
────────────────────────────────────────────
Public
  /start                 — welcome + random pic
  /help                  — command list

User control panel
  /sudo                  — inline panel to manage own channels

Admin (in ADMINS list or db.admins)
  /add_chnl  <ch>        — add channel to global broadcast
  /del_chnl  <ch>        — remove channel from global broadcast
  /chnl_list             — list global channels
  /addfeed   <url>       — add RSS feed
  /delfeed   <url>       — remove RSS feed
  /listfeeds             — list all feeds
  /setinterval <5min/2hr>— set global check interval
  /news      <url> [pos] — manually send entry to all global channels
  /stats                 — system resource stats
  /users                 — total unique users
  /ban       <id>        — ban a user
  /unban     <id>        — unban a user
  /ban_users             — list banned users
  /broadcast             — reply to any message to broadcast it
  /sudo_delchnl <ch>     — remove ANY user's channel (admin override)

Owner only
  /admin_list            — list all admins
  /add_admin  <id>       — grant admin
  /del_admin  <id>       — revoke admin
────────────────────────────────────────────

Schema note — user channel doc:
  feed_watermarks: [{"url": str, "wm": str}, ...]   ← per-feed watermarks
  interval:        int (seconds)
  last_posted_at:  float (unix timestamp)
  channel_id:      str  (always str(chat.id))
  channel_username: str | None
"""

import asyncio
import os
import random
import threading
import time
from datetime import timedelta

import psutil
import pymongo
import feedparser
from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus, ChatAction
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
)

from config import *
from webhook import start_webhook
from module.fsub import check_fsub, recheck_fsub
from module.rss.rss import (
    global_news_loop,
    user_channel_news_loop,
    format_rss_entry,
    find_youtube_iframe,
    download_and_send_video,
    get_config_feed_urls,
    _post_entry,
)

# ─── Boot time ───────────────────────────────────────────────────────────────

_START_TIME = time.time()

# ─── DB ──────────────────────────────────────────────────────────────────────

_mongo = pymongo.MongoClient(MONGO_URI)
db = _mongo["AnimeNewsBot"]

db.sent_news.create_index("entry_id",    unique=True, background=True)
db.users.create_index("user_id",         unique=True, background=True)
db.admins.create_index("user_id",        unique=True, background=True)
db.channels.create_index(
    [("user_id", pymongo.ASCENDING), ("channel_id", pymongo.ASCENDING)],
    unique=True, background=True,
)
db.admin_channels.create_index("channel_id", unique=True, background=True)

# ─── Client ──────────────────────────────────────────────────────────────────

app = Client("AnimeNewsBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
threading.Thread(target=start_webhook, daemon=True).start()

# ─── Conversation state (in-memory, per user) ────────────────────────────────

# { user_id: {"state": str, "panel_msg_id": int, "chat_id": int} }
_user_states: dict = {}


# ─── Utility helpers ─────────────────────────────────────────────────────────

def _is_owner(uid: int) -> bool:
    return uid == OWNER_ID

def _is_admin(uid: int) -> bool:
    return uid in ADMINS or bool(db.admins.find_one({"user_id": uid}))

def _is_banned(uid: int) -> bool:
    return bool(db.users.find_one({"user_id": uid, "is_banned": True}))

def _all_admin_ids() -> list:
    ids = set(ADMINS)
    for doc in db.admins.find({}):
        ids.add(doc["user_id"])
    return list(ids)

def _random_pic():
    return random.choice(START_PICS) if START_PICS else None

def _uptime_str(secs: float) -> str:
    td = timedelta(seconds=int(secs))
    d = td.days
    h, r = divmod(td.seconds, 3600)
    m, _ = divmod(r, 60)
    return f"{d}ᴅ {h}ʜ {m}ᴍ"

def _gb(b: int) -> str:
    return f"{b / 1024**3:.2f} ɢʙ"

def _mb(b: int) -> str:
    return f"{b / 1024**2:.2f} ᴍʙ"

async def _track_user(user):
    db.users.update_one(
        {"user_id": user.id},
        {"$setOnInsert": {
            "user_id":   user.id,
            "username":  user.username,
            "full_name": user.full_name,
            "is_banned": False,
        }},
        upsert=True,
    )

def _resolve_channel(raw: str):
    """Return (channel_id_or_username, display_str)."""
    raw = raw.strip()
    if raw.lstrip("-").isdigit():
        return int(raw), raw
    ch = raw if raw.startswith("@") else f"@{raw}"
    return ch, ch


# ─── Guard: ban + FSUB ───────────────────────────────────────────────────────

async def _guard(client: Client, message: Message) -> bool:
    uid = message.from_user.id
    if _is_banned(uid):
        await message.reply(
            "<b><blockquote>🚫 ʏᴏᴜ ᴀʀᴇ ʙᴀɴɴᴇᴅ ꜰʀᴏᴍ ᴜsɪɴɢ ᴛʜɪs ʙᴏᴛ.</blockquote></b>"
        )
        return False
    if _is_owner(uid) or _is_admin(uid):
        return True   # owner/admin bypass FSUB
    return await check_fsub(client, message)

async def _no_perm(message: Message):
    await message.reply(
        "<b><blockquote>⛔ ʏᴏᴜ ᴅᴏ ɴᴏᴛ ʜᴀᴠᴇ ᴩᴇʀᴍɪssɪᴏɴ ꜰᴏʀ ᴛʜɪs.</blockquote></b>"
    )


# ─── Quick ⚡ flash animation (non-start commands) ────────────────────────────

async def _flash(message: Message):
    """Send a quick ⚡ flash before the real reply."""
    try:
        m = await message.reply("ᴩʟᴇᴀsᴇ ᴡᴀɪᴛ...")
        await m.edit_text("⚡")
        await asyncio.sleep(0.5)
        await m.delete()
    except Exception:
        pass


# ─── /start ──────────────────────────────────────────────────────────────────

@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    if not await _guard(client, message):
        return

    await _track_user(message.from_user)
    user    = message.from_user
    mention = f"<a href='tg://user?id={user.id}'>{user.first_name}</a>"

    # ── Start animation ──────────────────────────────────────────────
    try:
        m = await message.reply_text("Wᴇᴡ...Hᴏᴡ ᴀʀᴇ ʏᴏᴜ \nᴡᴀɪᴛ ᴀ ᴍᴏᴍᴇɴᴛ. . .")
        await asyncio.sleep(0.4)
        await m.edit_text("⦿")
        await asyncio.sleep(0.5)
        await m.edit_text("⦿⦿")
        await asyncio.sleep(0.5)
        await message.reply_chat_action(ChatAction.CHOOSE_STICKER)
        await asyncio.sleep(2)
        await m.edit_text("I ᴀᴍ sᴛᴀʀᴛɪɴɢ...!!")
        await asyncio.sleep(0.4)
        await m.delete()
        await message.reply_sticker(
            "CAACAgQAAxkBAAKxCGnjjGOMolS6aezsB-4PsMH8QdcvAAJCFwACnUlZUNaiaMGYYnL1HgQ"
        )
    except Exception as e:
        print(f"[/start] Animation error: {e}")

    # ── Welcome card ─────────────────────────────────────────────────
    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("ᴍᴀɪɴ ʜᴜʙ", url="https://t.me/BotifyX_Pro_Botz"),
            InlineKeyboardButton("ꜱᴜᴩᴩᴏʀᴛ",  url="https://t.me/+ij3pcPOXv2U4MDll"),
        ],
        [InlineKeyboardButton("ᴅᴇᴠᴇʟᴏᴩᴇʀ", url="https://t.me/ITsANIMEN")],
    ])
    caption = (
        f"<b><blockquote>ʙᴀᴋᴋᴀᴀᴀ {mention}!!!\n\n"
        "ɪ ᴀᴍ ᴀɴ ᴀɴɪᴍᴇ ɴᴇᴡs ʙᴏᴛ.\n"
        "ɪ ꜰᴇᴛᴄʜ ᴀɴɪᴍᴇ ɴᴇᴡs ꜰʀᴏᴍ ʀss ꜰᴇᴇᴅs ᴀɴᴅ\n"
        "ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ ᴩᴏsᴛ ɪᴛ ᴛᴏ ᴀɴɪᴍᴇ ɴᴇᴡs ᴄʜᴀɴɴᴇʟs.\n\n"
        "ᴜsᴇ /sudo ᴛᴏ ᴍᴀɴᴀɢᴇ ʏᴏᴜʀ ᴏᴡɴ ᴄʜᴀɴɴᴇʟ.</blockquote></b>"
    )
    pic = _random_pic()
    try:
        if pic:
            await app.send_photo(message.chat.id, pic, caption=caption, reply_markup=buttons)
        else:
            await app.send_message(message.chat.id, caption, reply_markup=buttons)
    except Exception as e:
        print(f"[/start] Photo failed ({e}), sending text.")
        await app.send_message(message.chat.id, caption, reply_markup=buttons)


# ─── /help ───────────────────────────────────────────────────────────────────

@app.on_message(filters.command("help"))
async def help_cmd(client: Client, message: Message):
    if not await _guard(client, message):
        return
    await _flash(message)
    text = (
        "<b><blockquote>📋 ᴄᴏᴍᴍᴀɴᴅs\n\n"
        "/start — ᴡᴇʟᴄᴏᴍᴇ\n"
        "/help  — ᴛʜɪs ʟɪsᴛ\n"
        "/sudo  — ᴍᴀɴᴀɢᴇ ʏᴏᴜʀ ᴄʜᴀɴɴᴇʟ\n\n"
        "━━ ᴀᴅᴍɪɴ ━━\n"
        "/add_chnl &lt;ch&gt; — ᴀᴅᴅ ɢʟᴏʙᴀʟ ᴄʜᴀɴɴᴇʟ\n"
        "/del_chnl &lt;ch&gt; — ʀᴇᴍᴏᴠᴇ ɢʟᴏʙᴀʟ ᴄʜᴀɴɴᴇʟ\n"
        "/chnl_list — ʟɪsᴛ ɢʟᴏʙᴀʟ ᴄʜᴀɴɴᴇʟs\n"
        "/addfeed &lt;url&gt; — ᴀᴅᴅ ʀss ꜰᴇᴇᴅ\n"
        "/delfeed &lt;url&gt; — ʀᴇᴍᴏᴠᴇ ʀss ꜰᴇᴇᴅ\n"
        "/listfeeds — ʟɪsᴛ ꜰᴇᴇᴅs\n"
        "/setinterval &lt;5min/2hr&gt; — sᴇᴛ ɢʟᴏʙᴀʟ ɪɴᴛᴇʀᴠᴀʟ\n"
        "/news &lt;url&gt; [pos] — ᴍᴀɴᴜᴀʟ sᴇɴᴅ\n"
        "/stats — sʏsᴛᴇᴍ sᴛᴀᴛs\n"
        "/users — ᴛᴏᴛᴀʟ ᴜsᴇʀs\n"
        "/ban &lt;id&gt; /unban &lt;id&gt; /ban_users\n"
        "/broadcast — ʀᴇᴩʟʏ ᴛᴏ ᴍsɢ ᴛᴏ ʙʀᴏᴀᴅᴄᴀsᴛ\n"
        "/sudo_delchnl &lt;ch&gt; — ᴀᴅᴍɪɴ ᴏᴠᴇʀʀɪᴅᴇ\n\n"
        "━━ ᴏᴡɴᴇʀ ━━\n"
        "/admin_list /add_admin &lt;id&gt; /del_admin &lt;id&gt;"
        "</blockquote></b>"
    )
    await message.reply(text)


# ─── /sudo — User control panel ───────────────────────────────────────────────

def _sudo_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("ᴀᴅᴅ ᴄʜᴀɴɴᴇʟ",    callback_data="sudo:add"),
            InlineKeyboardButton("ᴅᴇʟᴇᴛᴇ ᴄʜᴀɴɴᴇʟ", callback_data="sudo:del_list"),
        ],
        [InlineKeyboardButton("sᴇᴛ ɪɴᴛᴇʀᴠᴀʟ",   callback_data="sudo:interval")],
        [
            InlineKeyboardButton("ꜱᴜᴩᴩᴏʀᴛ", url="https://t.me/+ij3pcPOXv2U4MDll"),
            InlineKeyboardButton("✖ ᴄʟᴏsᴇ", callback_data="sudo:close"),
        ],
    ])

def _sudo_caption() -> str:
    return (
        "<b><blockquote>⚙️ ʏᴏᴜʀ ᴄᴏɴᴛʀᴏʟ ᴩᴀɴᴇʟ\n\n"
        "ᴜsᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴs ʙᴇʟᴏᴡ ᴛᴏ ᴍᴀɴᴀɢᴇ\n"
        "ᴛʜᴇ ᴄʜᴀɴɴᴇʟs ᴡʜᴇʀᴇ ᴛʜᴇ ʙᴏᴛ ᴩᴏsᴛs ɴᴇᴡs ꜰᴏʀ ʏᴏᴜ.</blockquote></b>"
    )

@app.on_message(filters.command("sudo"))
async def sudo_cmd(client: Client, message: Message):
    if not await _guard(client, message):
        return
    await _track_user(message.from_user)
    await _flash(message)
    pic = _random_pic()
    try:
        if pic:
            sent = await app.send_photo(
                message.chat.id, pic,
                caption=_sudo_caption(),
                reply_markup=_sudo_markup(),
            )
        else:
            sent = await app.send_message(
                message.chat.id,
                _sudo_caption(),
                reply_markup=_sudo_markup(),
            )
        _user_states[message.from_user.id] = {
            "state":        "panel",
            "panel_msg_id": sent.id,
            "chat_id":      message.chat.id,
        }
    except Exception as e:
        print(f"[/sudo] {e}")


# ─── /sudo callbacks ──────────────────────────────────────────────────────────

@app.on_callback_query(filters.regex(r"^sudo:"))
async def sudo_callback(client: Client, cb: CallbackQuery):
    uid  = cb.from_user.id
    data = cb.data

    if _is_banned(uid):
        await cb.answer("🚫 ʏᴏᴜ ᴀʀᴇ ʙᴀɴɴᴇᴅ.", show_alert=True)
        return

    # ── Close ───────────────────────────────────────────────────────
    if data == "sudo:close":
        try:
            await cb.message.delete()
        except Exception:
            pass
        _user_states.pop(uid, None)
        await cb.answer()
        return

    # ── Home / Back ──────────────────────────────────────────────────
    if data in ("sudo:home", "sudo:back"):
        _user_states[uid] = {
            "state":        "panel",
            "panel_msg_id": cb.message.id,
            "chat_id":      cb.message.chat.id,
        }
        try:
            await cb.message.edit_caption(
                caption=_sudo_caption(), reply_markup=_sudo_markup()
            )
        except Exception:
            try:
                await cb.message.edit_text(
                    _sudo_caption(), reply_markup=_sudo_markup()
                )
            except Exception:
                pass
        await cb.answer()
        return

    # ── Add channel ─────────────────────────────────────────────────
    if data == "sudo:add":
        _user_states[uid] = {
            "state":        "waiting_add_channel",
            "panel_msg_id": cb.message.id,
            "chat_id":      cb.message.chat.id,
        }
        try:
            await cb.message.edit_caption(
                caption=(
                    "<b><blockquote>➕ ᴀᴅᴅ ᴄʜᴀɴɴᴇʟ\n\n"
                    "sᴇɴᴅ ʏᴏᴜʀ ᴄʜᴀɴɴᴇʟ ᴜsᴇʀɴᴀᴍᴇ (@example)\n"
                    "ᴏʀ ɪᴅ (-100xxxxxxxxx)\n\n"
                    "ᴍᴀᴋᴇ sᴜʀᴇ ᴛʜᴇ ʙᴏᴛ ɪs ᴀɴ ᴀᴅᴍɪɴ ᴛʜᴇʀᴇ!</blockquote></b>"
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("« ʙᴀᴄᴋ", callback_data="sudo:back")],
                ]),
            )
        except Exception:
            pass
        await cb.answer()
        return

    # ── Delete channel list ─────────────────────────────────────────
    if data == "sudo:del_list":
        user_channels = list(db.channels.find({"user_id": uid}))
        if not user_channels:
            await cb.answer("ʏᴏᴜ ʜᴀᴠᴇ ɴᴏ ᴄʜᴀɴɴᴇʟs.", show_alert=True)
            return

        buttons = []
        for doc in user_channels:
            label    = doc.get("channel_username") or str(doc["channel_id"])
            ch_id_cb = str(doc["channel_id"]).strip()
            buttons.append([
                InlineKeyboardButton(f"🗑 {label}", callback_data=f"sudo:del:{ch_id_cb}")
            ])
        buttons.append([InlineKeyboardButton("« ʙᴀᴄᴋ", callback_data="sudo:back")])
        try:
            await cb.message.edit_caption(
                caption="<b><blockquote>ꜱᴇʟᴇᴄᴛ ᴄʜᴀɴɴᴇʟ ᴛᴏ ʀᴇᴍᴏᴠᴇ:</blockquote></b>",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        except Exception:
            pass
        await cb.answer()
        return

    # ── Delete specific channel ──────────────────────────────────────
    if data.startswith("sudo:del:"):
        # channel_id is always stored as str(chat.id), strip any whitespace
        ch_id_str = data.split(":", 2)[2].strip()

        result = db.channels.delete_one({"user_id": uid, "channel_id": ch_id_str})
        if result.deleted_count:
            await cb.answer("✅ ᴄʜᴀɴɴᴇʟ ʀᴇᴍᴏᴠᴇᴅ.", show_alert=True)
        else:
            await cb.answer("❌ ɴᴏᴛ ꜰᴏᴜɴᴅ.", show_alert=True)

        # Refresh the delete list (or return to panel if now empty)
        user_channels = list(db.channels.find({"user_id": uid}))
        if not user_channels:
            _user_states[uid] = {"state": "panel"}
            try:
                await cb.message.edit_caption(
                    caption=_sudo_caption(), reply_markup=_sudo_markup()
                )
            except Exception:
                pass
        else:
            buttons = []
            for doc in user_channels:
                label    = doc.get("channel_username") or str(doc["channel_id"])
                ch_id_cb = str(doc["channel_id"]).strip()
                buttons.append([
                    InlineKeyboardButton(f"🗑 {label}", callback_data=f"sudo:del:{ch_id_cb}")
                ])
            buttons.append([InlineKeyboardButton("« ʙᴀᴄᴋ", callback_data="sudo:back")])
            try:
                await cb.message.edit_caption(
                    caption="<b><blockquote>ꜱᴇʟᴇᴄᴛ ᴄʜᴀɴɴᴇʟ ᴛᴏ ʀᴇᴍᴏᴠᴇ:</blockquote></b>",
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
            except Exception:
                pass
        return

    # ── Set interval ────────────────────────────────────────────────
    if data == "sudo:interval":
        user_ch  = db.channels.find_one({"user_id": uid})
        cur_secs = user_ch.get("interval", 300) if user_ch else 300
        cur_mins = cur_secs // 60
        cur_label = f"{cur_mins // 60:.0f}ʜʀ" if cur_secs >= 3600 else f"{cur_mins}ᴍɪɴ"

        _user_states[uid] = {
            "state":        "waiting_interval",
            "panel_msg_id": cb.message.id,
            "chat_id":      cb.message.chat.id,
        }
        try:
            await cb.message.edit_caption(
                caption=(
                    "<b><blockquote>⏱ sᴇᴛ ɪɴᴛᴇʀᴠᴀʟ\n\n"
                    "sᴇɴᴅ ᴛʜᴇ ɪɴᴛᴇʀᴠᴀʟ ɪɴ ᴏɴᴇ ᴏꜰ ᴛʜᴇsᴇ ꜰᴏʀᴍᴀᴛs:\n\n"
                    f"ᴄᴜʀʀᴇɴᴛ: <code>{cur_label}</code>\n\n"
                    "  5min  →  5 ᴍɪɴᴜᴛᴇs\n"
                    "  2hr   →  2 ʜᴏᴜʀs\n\n"
                    "ᴍɪɴ: 5min  •  ᴍᴀx: 12hr</blockquote></b>"
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("« ʙᴀᴄᴋ", callback_data="sudo:back")],
                ]),
            )
        except Exception:
            pass
        await cb.answer()
        return


# ─── FSUB retry callback ──────────────────────────────────────────────────────

@app.on_callback_query(filters.regex(r"^fsub:retry$"))
async def fsub_retry(client: Client, cb: CallbackQuery):
    uid    = cb.from_user.id
    joined = await recheck_fsub(client, uid)
    if joined:
        await cb.answer("✅ ᴠᴇʀɪꜰɪᴇᴅ! ʏᴏᴜ ᴄᴀɴ ɴᴏᴡ ᴜsᴇ ᴛʜᴇ ʙᴏᴛ.", show_alert=True)
        try:
            await cb.message.delete()
        except Exception:
            pass
    else:
        await cb.answer("⚠️ ᴩʟᴇᴀsᴇ ᴊᴏɪɴ ᴛʜᴇ ᴄʜᴀɴɴᴇʟ ꜰɪʀsᴛ!", show_alert=True)


# ─── Non-command message handler (conversation state) ────────────────────────

def _parse_interval(text: str):
    """Parse "5min" / "2hr" → seconds. Returns (secs, error_or_None)."""
    text = text.strip().lower().replace(" ", "")
    try:
        if text.endswith("hr"):
            secs = int(float(text[:-2]) * 3600)
        elif text.endswith("min"):
            secs = int(float(text[:-3]) * 60)
        else:
            return None, "ᴜsᴇ ꜰᴏʀᴍᴀᴛ: 5min ᴏʀ 2hr"
    except ValueError:
        return None, "ɪɴᴠᴀʟɪᴅ ɴᴜᴍʙᴇʀ."
    if secs < 300:
        return None, "ᴍɪɴɪᴍᴜᴍ ɪɴᴛᴇʀᴠᴀʟ ɪs 5ᴍɪɴ."
    if secs > 43200:
        return None, "ᴍᴀxɪᴍᴜᴍ ɪɴᴛᴇʀᴠᴀʟ ɪs 12ʜʀ."
    return secs, None


@app.on_message(filters.private & ~filters.command(
    ["start","help","sudo","add_chnl","del_chnl","chnl_list",
     "addfeed","delfeed","listfeeds","setinterval","news","stats",
     "users","ban","unban","ban_users","admin_list","add_admin",
     "del_admin","broadcast","sudo_delchnl"]
))
async def handle_user_input(client: Client, message: Message):
    uid        = message.from_user.id
    state_info = _user_states.get(uid)
    if not state_info:
        return

    state = state_info.get("state")

    # ── Waiting for channel to add ────────────────────────────────────
    if state == "waiting_add_channel":
        raw = message.text.strip() if message.text else ""
        if not raw:
            await message.reply(
                "<b><blockquote>❌ ᴩʟᴇᴀsᴇ sᴇɴᴅ ᴀ ᴠᴀʟɪᴅ ᴜsᴇʀɴᴀᴍᴇ ᴏʀ ɪᴅ.</blockquote></b>"
            )
            return

        channel_id, _ = _resolve_channel(raw)

        try:
            chat   = await client.get_chat(channel_id)
            me     = await client.get_me()
            member = await client.get_chat_member(channel_id, me.id)
            if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                await message.reply(
                    "<b><blockquote>❌ ʙᴏᴛ ɪs ɴᴏᴛ ᴀɴ ᴀᴅᴍɪɴ ɪɴ ᴛʜᴀᴛ ᴄʜᴀɴɴᴇʟ.\n"
                    "ᴍᴀᴋᴇ ɪᴛ ᴀᴅᴍɪɴ ꜰɪʀsᴛ, ᴛʜᴇɴ ᴛʀʏ ᴀɢᴀɪɴ.</blockquote></b>"
                )
                return
        except Exception as e:
            await message.reply(
                f"<b><blockquote>❌ ᴄᴀɴɴᴏᴛ ᴀᴄᴄᴇss ᴄʜᴀɴɴᴇʟ.\n<i>{e}</i></blockquote></b>"
            )
            return

        # Always store channel_id as str(chat.id) — consistent with delete logic
        ch_id_str    = str(chat.id)
        ch_username  = f"@{chat.username}" if chat.username else None

        if db.channels.find_one({"user_id": uid, "channel_id": ch_id_str}):
            await message.reply(
                "<b><blockquote>⚠️ ʏᴏᴜ ᴀʟʀᴇᴀᴅʏ ʜᴀᴠᴇ ᴛʜɪs ᴄʜᴀɴɴᴇʟ.</blockquote></b>"
            )
            return

        # Insert with empty feed_watermarks — scheduler will init on first tick
        db.channels.insert_one({
            "user_id":         uid,
            "channel_id":      ch_id_str,
            "channel_username": ch_username,
            "interval":        300,
            "feed_watermarks": [],    # scheduler populates this on first tick
            "last_posted_at":  0,
        })

        label = ch_username or chat.title or ch_id_str
        await message.reply(f"<b><blockquote>✅ ᴄʜᴀɴɴᴇʟ ᴀᴅᴅᴇᴅ: {label}</blockquote></b>")

        # Notify all admins
        user  = message.from_user
        notif = (
            f"<b><blockquote>🔔 ɴᴇᴡ ᴄʜᴀɴɴᴇʟ ᴀᴅᴅᴇᴅ ʙʏ ᴜsᴇʀ\n\n"
            f"ᴜsᴇʀ: {user.full_name}\n"
            f"ᴜsᴇʀɴᴀᴍᴇ: @{user.username or 'N/A'}\n"
            f"ɪᴅ: <code>{user.id}</code>\n\n"
            f"ᴄʜᴀɴɴᴇʟ: {chat.title}\n"
            f"ᴄʜ ᴜsᴇʀɴᴀᴍᴇ: {ch_username or 'N/A'}\n"
            f"ᴄʜ ɪᴅ: <code>{chat.id}</code></blockquote></b>"
        )
        for admin_id in _all_admin_ids():
            try:
                await app.send_message(admin_id, notif)
            except Exception:
                pass

        # Return panel to main view
        _user_states[uid]["state"] = "panel"
        try:
            panel_msg_id = state_info.get("panel_msg_id")
            chat_id      = state_info.get("chat_id")
            if panel_msg_id and chat_id:
                await app.edit_message_caption(
                    chat_id, panel_msg_id,
                    caption=_sudo_caption(),
                    reply_markup=_sudo_markup(),
                )
        except Exception:
            pass
        return

    # ── Waiting for interval ──────────────────────────────────────────
    if state == "waiting_interval":
        text      = message.text.strip() if message.text else ""
        secs, err = _parse_interval(text)
        if err:
            await message.reply(f"<b><blockquote>❌ {err}</blockquote></b>")
            return

        db.channels.update_many({"user_id": uid}, {"$set": {"interval": secs}})

        mins  = secs // 60
        label = f"{mins // 60:.0f}ʜʀ" if secs >= 3600 else f"{mins}ᴍɪɴ"
        await message.reply(
            f"<b><blockquote>✅ ɪɴᴛᴇʀᴠᴀʟ sᴇᴛ ᴛᴏ {label} ꜰᴏʀ ᴀʟʟ ʏᴏᴜʀ ᴄʜᴀɴɴᴇʟs.</blockquote></b>"
        )

        _user_states[uid]["state"] = "panel"
        try:
            panel_msg_id = state_info.get("panel_msg_id")
            chat_id      = state_info.get("chat_id")
            if panel_msg_id and chat_id:
                await app.edit_message_caption(
                    chat_id, panel_msg_id,
                    caption=_sudo_caption(),
                    reply_markup=_sudo_markup(),
                )
        except Exception:
            pass
        return


# ─── /add_chnl ───────────────────────────────────────────────────────────────

@app.on_message(filters.command("add_chnl"))
async def add_chnl(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return await _no_perm(message)
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(
            "<b><blockquote>ᴜsᴀɢᴇ: /add_chnl &lt;@username or id&gt;</blockquote></b>"
        )
        return

    await _flash(message)
    channel_id, _ = _resolve_channel(parts[1])
    try:
        chat   = await client.get_chat(channel_id)
        me     = await client.get_me()
        member = await client.get_chat_member(channel_id, me.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await message.reply(
                "<b><blockquote>❌ ᴍᴀᴋᴇ ᴛʜᴇ ʙᴏᴛ ᴀɴ ᴀᴅᴍɪɴ ꜰɪʀsᴛ.</blockquote></b>"
            )
            return
    except Exception as e:
        await message.reply(
            f"<b><blockquote>❌ ᴄᴀɴɴᴏᴛ ᴀᴄᴄᴇss ᴄʜᴀɴɴᴇʟ.\n<i>{e}</i></blockquote></b>"
        )
        return

    ch_id_str = str(chat.id)
    if db.admin_channels.find_one({"channel_id": ch_id_str}):
        await message.reply("<b><blockquote>⚠️ ᴀʟʀᴇᴀᴅʏ ɪɴ ᴛʜᴇ ʟɪsᴛ.</blockquote></b>")
        return

    db.admin_channels.insert_one({"channel_id": ch_id_str})
    label = f"@{chat.username}" if chat.username else chat.title or ch_id_str
    await message.reply(f"<b><blockquote>✅ ᴄʜᴀɴɴᴇʟ ᴀᴅᴅᴇᴅ: {label}</blockquote></b>")


# ─── /del_chnl ───────────────────────────────────────────────────────────────

@app.on_message(filters.command("del_chnl"))
async def del_chnl(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return await _no_perm(message)
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(
            "<b><blockquote>ᴜsᴀɢᴇ: /del_chnl &lt;@username or id&gt;</blockquote></b>"
        )
        return

    await _flash(message)
    raw = parts[1].strip()
    if raw.lstrip("-").isdigit():
        ch_id_str = str(int(raw))
    else:
        ch_id_str = raw if raw.startswith("@") else f"@{raw}"
        try:
            chat      = await client.get_chat(ch_id_str)
            ch_id_str = str(chat.id)
        except Exception:
            pass

    result = db.admin_channels.delete_one({"channel_id": ch_id_str})
    if result.deleted_count:
        await message.reply(
            f"<b><blockquote>✅ ᴄʜᴀɴɴᴇʟ ʀᴇᴍᴏᴠᴇᴅ: <code>{ch_id_str}</code></blockquote></b>"
        )
    else:
        await message.reply(
            "<b><blockquote>❌ ᴄʜᴀɴɴᴇʟ ɴᴏᴛ ꜰᴏᴜɴᴅ ɪɴ ᴛʜᴇ ʟɪsᴛ.</blockquote></b>"
        )


# ─── /chnl_list ──────────────────────────────────────────────────────────────

@app.on_message(filters.command("chnl_list"))
async def chnl_list(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return await _no_perm(message)
    await _flash(message)
    docs = list(db.admin_channels.find({}))
    if not docs:
        await message.reply(
            "<b><blockquote>ɴᴏ ɢʟᴏʙᴀʟ ᴄʜᴀɴɴᴇʟs. ᴜsᴇ /add_chnl.</blockquote></b>"
        )
        return

    lines = []
    for i, doc in enumerate(docs, 1):
        cid = doc["channel_id"]
        try:
            chat  = await client.get_chat(int(cid))
            label = f"@{chat.username}" if chat.username else f"{chat.title} [<code>{chat.id}</code>]"
        except Exception:
            label = f"<code>{cid}</code>"
        lines.append(f"{i}. {label}")

    await message.reply(
        "<b><blockquote>📡 ɢʟᴏʙᴀʟ ᴄʜᴀɴɴᴇʟs ~\n\n" + "\n".join(lines) + "</blockquote></b>"
    )


# ─── /addfeed ────────────────────────────────────────────────────────────────

@app.on_message(filters.command("addfeed"))
async def addfeed(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return await _no_perm(message)
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].startswith("http"):
        await message.reply("<b><blockquote>ᴜsᴀɢᴇ: /addfeed &lt;rss_url&gt;</blockquote></b>")
        return
    await _flash(message)
    url = parts[1].strip()
    if db.rss_feeds.find_one({"url": url}):
        await message.reply("<b><blockquote>⚠️ ᴀʟʀᴇᴀᴅʏ ɪɴ ᴛʜᴇ ʟɪsᴛ.</blockquote></b>")
        return
    db.rss_feeds.insert_one({"url": url})
    await message.reply(
        f"<b><blockquote>✅ ꜰᴇᴇᴅ ᴀᴅᴅᴇᴅ:\n<code>{url}</code></blockquote></b>"
    )


# ─── /delfeed ────────────────────────────────────────────────────────────────

@app.on_message(filters.command("delfeed"))
async def delfeed(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return await _no_perm(message)
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("<b><blockquote>ᴜsᴀɢᴇ: /delfeed &lt;rss_url&gt;</blockquote></b>")
        return
    await _flash(message)
    url = parts[1].strip()
    if url in get_config_feed_urls():
        await message.reply(
            "<b><blockquote>⚠️ ᴛʜɪs ɪs ᴀ ᴄᴏɴꜰɪɢ ꜰᴇᴇᴅ.\nEᴅɪᴛ config.py ᴛᴏ ʀᴇᴍᴏᴠᴇ ɪᴛ.</blockquote></b>"
        )
        return
    result = db.rss_feeds.delete_one({"url": url})
    if result.deleted_count:
        await message.reply(
            f"<b><blockquote>✅ ꜰᴇᴇᴅ ʀᴇᴍᴏᴠᴇᴅ:\n<code>{url}</code></blockquote></b>"
        )
    else:
        await message.reply("<b><blockquote>❌ ꜰᴇᴇᴅ ɴᴏᴛ ꜰᴏᴜɴᴅ.</blockquote></b>")


# ─── /listfeeds ──────────────────────────────────────────────────────────────

@app.on_message(filters.command("listfeeds"))
async def listfeeds(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return await _no_perm(message)
    await _flash(message)
    cfg      = get_config_feed_urls()
    dbs      = [doc["url"] for doc in db.rss_feeds.find({})]
    all_feeds = list(dict.fromkeys(cfg + dbs))
    if not all_feeds:
        await message.reply("<b><blockquote>ɴᴏ ꜰᴇᴇᴅs ᴄᴏɴꜰɪɢᴜʀᴇᴅ.</blockquote></b>")
        return
    lines = [f"{i}. <code>{u}</code>" for i, u in enumerate(all_feeds, 1)]
    await message.reply("<b>ꜰᴇᴇᴅs ʟɪsᴛᴇᴅ ~\n\n" + "\n".join(lines) + "</b>")


# ─── /setinterval ─────────────────────────────────────────────────────────────

@app.on_message(filters.command("setinterval"))
async def setinterval(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return await _no_perm(message)
    parts = message.text.split()

    if len(parts) < 2:
        cfg      = db.global_settings.find_one({"_id": "config"}) or {}
        cur_secs = cfg.get("interval", 300)
        cur_mins = cur_secs // 60
        cur_label = f"{cur_mins // 60:.0f}ʜʀ" if cur_secs >= 3600 else f"{cur_mins}ᴍɪɴ"
        await message.reply(
            f"<b><blockquote>⏱ sᴇᴛ ɢʟᴏʙᴀʟ ɴᴇᴡs ɪɴᴛᴇʀᴠᴀʟ\n\n"
            f"ᴄᴜʀʀᴇɴᴛ: <code>{cur_label}</code>\n\n"
            "ᴜsᴀɢᴇ: /setinterval &lt;5min&gt; ᴏʀ /setinterval &lt;2hr&gt;\n"
            "ᴍɪɴ: 5min  •  ᴍᴀx: 12hr</blockquote></b>"
        )
        return

    await _flash(message)
    secs, err = _parse_interval(parts[1])
    if err:
        await message.reply(
            f"<b><blockquote>❌ {err}\n\n"
            "ᴜsᴀɢᴇ: /setinterval &lt;5min&gt; ᴏʀ /setinterval &lt;2hr&gt;\n"
            "ᴍɪɴ: 5min  •  ᴍᴀx: 12hr</blockquote></b>"
        )
        return

    db.global_settings.update_one(
        {"_id": "config"}, {"$set": {"interval": secs}}, upsert=True
    )
    mins  = secs // 60
    label = f"{mins // 60:.0f}ʜʀ" if secs >= 3600 else f"{mins}ᴍɪɴ"
    await message.reply(
        f"<b><blockquote>✅ ɢʟᴏʙᴀʟ ɪɴᴛᴇʀᴠᴀʟ sᴇᴛ ᴛᴏ <code>{label}</code></blockquote></b>"
    )


# ─── /news — manual send (admin-style format) ─────────────────────────────────

@app.on_message(filters.command("news"))
async def news_cmd(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return await _no_perm(message)

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(
            "<b><blockquote>ᴜsᴀɢᴇ: /news &lt;rss_url&gt; [position]\n"
            "ᴩᴏs 1 = ʟᴀᴛᴇsᴛ (ᴅᴇꜰᴀᴜʟᴛ)</blockquote></b>"
        )
        return

    await _flash(message)
    rss_link = parts[1].strip()
    position = 0
    if len(parts) >= 3:
        try:
            position = int(parts[2]) - 1
            if position < 0:
                raise ValueError
        except ValueError:
            await message.reply(
                "<b><blockquote>❌ ᴩᴏsɪᴛɪᴏɴ ᴍᴜsᴛ ʙᴇ ᴀ ᴩᴏsɪᴛɪᴠᴇ ɪɴᴛᴇɢᴇʀ.</blockquote></b>"
            )
            return

    admin_channels = [doc["channel_id"] for doc in db.admin_channels.find({})]
    if not admin_channels:
        await message.reply(
            "<b><blockquote>⚠️ ɴᴏ ɢʟᴏʙᴀʟ ᴄʜᴀɴɴᴇʟs. ᴜsᴇ /add_chnl.</blockquote></b>"
        )
        return

    try:
        feed = await asyncio.to_thread(feedparser.parse, rss_link)
    except Exception as e:
        await message.reply(
            f"<b><blockquote>❌ ꜰᴇᴇᴅ ᴇʀʀᴏʀ: <i>{e}</i></blockquote></b>"
        )
        return

    if not feed.entries or position >= len(feed.entries):
        await message.reply(
            f"<b><blockquote>❌ ɴᴏ ᴇɴᴛʀʏ ᴀᴛ ᴩᴏs {position + 1} "
            f"(ꜰᴇᴇᴅ ʜᴀs {len(feed.entries)} ᴇɴᴛʀɪᴇs)</blockquote></b>"
        )
        return

    entry = feed.entries[position]
    # Always admin style for manual sends
    msg, thumbnail_url, link = await format_rss_entry(entry, is_admin_channel=True)

    errors = []
    for ch in admin_channels:
        ok = await _post_entry(app, ch, entry, msg, thumbnail_url, link)
        if not ok:
            errors.append(str(ch))
        await asyncio.sleep(1)

    if errors:
        await message.reply(
            f"<b><blockquote>⚠️ sᴇɴᴛ ᴡɪᴛʜ ᴇʀʀᴏʀs ᴏɴ: "
            f"<code>{', '.join(errors)}</code></blockquote></b>"
        )
    else:
        await message.reply(
            "<b><blockquote>✅ ɴᴇᴡs sᴇɴᴛ ᴛᴏ ᴀʟʟ ᴄʜᴀɴɴᴇʟs!</blockquote></b>"
        )


# ─── /stats ───────────────────────────────────────────────────────────────────

@app.on_message(filters.command("stats"))
async def stats_cmd(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return await _no_perm(message)
    await _flash(message)

    disk  = psutil.disk_usage("/")
    ram   = psutil.virtual_memory()
    swap  = psutil.swap_memory()
    cpu   = psutil.cpu_percent(interval=1)
    net   = psutil.net_io_counters()
    proc  = psutil.Process(os.getpid())
    p_cpu = proc.cpu_percent(interval=0.5)
    p_mem = proc.memory_info().rss

    try:
        import socket as _s
        _s.setdefaulttimeout(3)
        _s.socket(_s.AF_INET, _s.SOCK_STREAM).connect(("8.8.8.8", 53))
        net_ok = "✓ ᴀᴠᴀɪʟᴀʙʟᴇ"
    except Exception:
        net_ok = "✗ ᴜɴᴀᴠᴀɪʟᴀʙʟᴇ"

    def _status(pct, warn=85):
        return "✓ ɴᴏʀᴍᴀʟ" if pct < warn else "⚠ ʜɪɢʜ"

    text = (
        "<blockquote>"
        "   ✦ sʏsᴛᴇᴍ ᴜsᴀɢᴇ sᴛᴀᴛs\n"
        "≡ ʙᴏᴛ sᴛᴀᴛɪsᴛɪᴄs:\n"
        f"›› ᴛᴏᴛᴀʟ ᴜsᴇʀs: {db.users.count_documents({})}\n"
        f"›› ʙᴏᴛ sᴛᴀᴛᴜs: ✓ ʀᴜɴɴɪɴɢ\n"
        f"›› ᴜᴩᴛɪᴍᴇ: {_uptime_str(time.time() - _START_TIME)}\n"
        f"›› ᴀᴅᴍɪɴs: {len(_all_admin_ids())}\n"
        "≡ ᴅɪsᴋ ᴜsᴀɢᴇ:\n"
        f"›› ᴛᴏᴛᴀʟ: {_gb(disk.total)}\n"
        f"›› ᴜsᴇᴅ: {_gb(disk.used)} ({disk.percent}%)\n"
        f"›› ꜰʀᴇᴇ: {_gb(disk.free)}\n"
        f"›› sᴛᴀᴛᴜs: {_status(disk.percent)}\n"
        "≡ ʀᴀᴍ ᴜsᴀɢᴇ:\n"
        f"›› ᴛᴏᴛᴀʟ: {_gb(ram.total)}\n"
        f"›› ᴜsᴇᴅ: {_gb(ram.used)} ({ram.percent}%)\n"
        f"›› ꜰʀᴇᴇ: {_gb(ram.available)}\n"
        f"›› sᴛᴀᴛᴜs: {_status(ram.percent)}\n"
        "≡ sᴡᴀᴩ ᴜsᴀɢᴇ:\n"
        f"›› ᴛᴏᴛᴀʟ: {_gb(swap.total)}\n"
        f"›› ᴜsᴇᴅ: {_gb(swap.used)} ({swap.percent}%)\n"
        f"›› ꜰʀᴇᴇ: {_gb(swap.free)}\n"
        "≡ ᴄᴩᴜ & ɴᴇᴛᴡᴏʀᴋ:\n"
        f"›› ᴄᴩᴜ ᴜsᴀɢᴇ: {cpu}% {_status(cpu, 80)}\n"
        f"›› ɴᴇᴛᴡᴏʀᴋ: {net_ok}\n"
        f"›› ᴜᴩʟᴏᴀᴅᴇᴅ: {_mb(net.bytes_sent)}\n"
        f"›› ᴅᴏᴡɴʟᴏᴀᴅᴇᴅ: {_mb(net.bytes_recv)}\n"
        "≡ ʙᴏᴛ ʀᴇsᴏᴜʀᴄᴇ ᴜsᴀɢᴇ:\n"
        f"›› ᴄᴩᴜ: {p_cpu}%\n"
        f"›› ᴍᴇᴍᴏʀʏ: {_mb(p_mem)}\n"
        "• ᴜsᴇ ᴛʜɪs ɪɴꜰᴏ ᴛᴏ ᴍᴏɴɪᴛᴏʀ ʏᴏᴜʀ ʙᴏᴛ's ᴩᴇʀꜰᴏʀᴍᴀɴᴄᴇ!"
        "</blockquote>"
    )
    await message.reply(text)


# ─── /users ───────────────────────────────────────────────────────────────────

@app.on_message(filters.command("users"))
async def users_cmd(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return await _no_perm(message)
    await _flash(message)
    count = db.users.count_documents({})
    await message.reply(
        f"<b><blockquote>👥 ᴛᴏᴛᴀʟ ᴜɴɪqᴜᴇ ᴜsᴇʀs: <code>{count}</code></blockquote></b>"
    )


# ─── /ban ────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("ban"))
async def ban_cmd(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return await _no_perm(message)
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("<b><blockquote>ᴜsᴀɢᴇ: /ban &lt;user_id&gt;</blockquote></b>")
        return
    target = int(parts[1])
    if target == OWNER_ID:
        await message.reply("<b><blockquote>⛔ ᴄᴀɴɴᴏᴛ ʙᴀɴ ᴛʜᴇ ᴏᴡɴᴇʀ.</blockquote></b>")
        return

    await _flash(message)
    db.users.update_one({"user_id": target}, {"$set": {"is_banned": True}}, upsert=True)
    deleted = db.channels.delete_many({"user_id": target}).deleted_count
    db.action_log.insert_one({
        "action":           "ban",
        "target_user":      target,
        "by_admin":         message.from_user.id,
        "ts":               time.time(),
        "channels_removed": deleted,
    })
    print(f"[Ban] User {target} banned; {deleted} channel row(s) removed.")
    await message.reply(
        f"<b><blockquote>🚫 ᴜsᴇʀ <code>{target}</code> ʙᴀɴɴᴇᴅ.\n"
        f"ᴄʜᴀɴɴᴇʟs ʀᴇᴍᴏᴠᴇᴅ: {deleted}</blockquote></b>"
    )


# ─── /unban ──────────────────────────────────────────────────────────────────

@app.on_message(filters.command("unban"))
async def unban_cmd(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return await _no_perm(message)
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("<b><blockquote>ᴜsᴀɢᴇ: /unban &lt;user_id&gt;</blockquote></b>")
        return
    await _flash(message)
    target = int(parts[1])
    db.users.update_one({"user_id": target}, {"$set": {"is_banned": False}})
    db.action_log.insert_one({
        "action":      "unban",
        "target_user": target,
        "by_admin":    message.from_user.id,
        "ts":          time.time(),
    })
    await message.reply(
        f"<b><blockquote>✅ ᴜsᴇʀ <code>{target}</code> ᴜɴʙᴀɴɴᴇᴅ.</blockquote></b>"
    )


# ─── /ban_users ──────────────────────────────────────────────────────────────

@app.on_message(filters.command("ban_users"))
async def ban_users(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return await _no_perm(message)
    await _flash(message)
    banned = list(db.users.find({"is_banned": True}))
    if not banned:
        await message.reply("<b><blockquote>ɴᴏ ʙᴀɴɴᴇᴅ ᴜsᴇʀs.</blockquote></b>")
        return
    lines = []
    for doc in banned:
        name  = doc.get("full_name", "N/A")
        uname = f"@{doc['username']}" if doc.get("username") else "N/A"
        lines.append(f"• <code>{doc['user_id']}</code> — {name} ({uname})")
    await message.reply(
        "<b><blockquote>🚫 ʙᴀɴɴᴇᴅ ᴜsᴇʀs:\n\n" + "\n".join(lines) + "</blockquote></b>"
    )


# ─── /admin_list (owner only) ─────────────────────────────────────────────────

@app.on_message(filters.command("admin_list"))
async def admin_list(client: Client, message: Message):
    if not _is_owner(message.from_user.id):
        return await _no_perm(message)
    await _flash(message)
    ids   = _all_admin_ids()
    lines = [f"{i}. <code>{uid}</code>" for i, uid in enumerate(ids, 1)]
    await message.reply(
        "<b><blockquote>👮 ᴀᴅᴍɪɴ ʟɪsᴛ:\n\n" + "\n".join(lines) + "</blockquote></b>"
    )


# ─── /add_admin (owner only) ──────────────────────────────────────────────────

@app.on_message(filters.command("add_admin"))
async def add_admin(client: Client, message: Message):
    if not _is_owner(message.from_user.id):
        return await _no_perm(message)
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply(
            "<b><blockquote>ᴜsᴀɢᴇ: /add_admin &lt;user_id&gt;</blockquote></b>"
        )
        return
    await _flash(message)
    target = int(parts[1])
    if db.admins.find_one({"user_id": target}) or target in ADMINS:
        await message.reply("<b><blockquote>⚠️ ᴀʟʀᴇᴀᴅʏ ᴀɴ ᴀᴅᴍɪɴ.</blockquote></b>")
        return
    db.admins.insert_one({"user_id": target})
    await message.reply(
        f"<b><blockquote>✅ <code>{target}</code> ɢʀᴀɴᴛᴇᴅ ᴀᴅᴍɪɴ.</blockquote></b>"
    )


# ─── /del_admin (owner only) ──────────────────────────────────────────────────

@app.on_message(filters.command("del_admin"))
async def del_admin(client: Client, message: Message):
    if not _is_owner(message.from_user.id):
        return await _no_perm(message)
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply(
            "<b><blockquote>ᴜsᴀɢᴇ: /del_admin &lt;user_id&gt;</blockquote></b>"
        )
        return
    await _flash(message)
    target = int(parts[1])
    if target == OWNER_ID:
        await message.reply("<b><blockquote>⛔ ᴄᴀɴɴᴏᴛ ʀᴇᴍᴏᴠᴇ ᴏᴡɴᴇʀ.</blockquote></b>")
        return
    db.admins.delete_one({"user_id": target})
    await message.reply(
        f"<b><blockquote>✅ <code>{target}</code> ᴀᴅᴍɪɴ ʀᴇᴠᴏᴋᴇᴅ.</blockquote></b>"
    )


# ─── /broadcast ───────────────────────────────────────────────────────────────

@app.on_message(filters.command("broadcast"))
async def broadcast_cmd(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return await _no_perm(message)

    target_msg = message.reply_to_message
    if not target_msg:
        await message.reply(
            "<b><blockquote>ʀᴇᴩʟʏ ᴛᴏ ᴀ ᴍᴇssᴀɢᴇ (ᴛᴇxᴛ ᴏʀ ᴍᴇᴅɪᴀ) ᴀɴᴅ ᴜsᴇ /broadcast</blockquote></b>"
        )
        return

    await _flash(message)
    users = list(db.users.find({"is_banned": {"$ne": True}}))
    ok = fail = 0
    status_msg = await message.reply(
        f"<b><blockquote>📢 ʙʀᴏᴀᴅᴄᴀsᴛɪɴɢ ᴛᴏ {len(users)} ᴜsᴇʀs…</blockquote></b>"
    )

    for user_doc in users:
        uid = user_doc["user_id"]
        try:
            sent = await target_msg.copy(uid)
            try:
                await client.pin_chat_message(uid, sent.id, disable_notification=True)
            except Exception:
                pass
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)

    try:
        await status_msg.edit_text(
            f"<b><blockquote>📢 ʙʀᴏᴀᴅᴄᴀsᴛ ᴄᴏᴍᴩʟᴇᴛᴇ\n\n"
            f"✅ sᴇɴᴛ: {ok}\n❌ ꜰᴀɪʟᴇᴅ: {fail}</blockquote></b>"
        )
    except Exception:
        pass


# ─── /sudo_delchnl — admin override ──────────────────────────────────────────

@app.on_message(filters.command("sudo_delchnl"))
async def sudo_delchnl(client: Client, message: Message):
    if not _is_admin(message.from_user.id):
        return await _no_perm(message)
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(
            "<b><blockquote>ᴜsᴀɢᴇ: /sudo_delchnl &lt;@username or id&gt;</blockquote></b>"
        )
        return

    await _flash(message)
    raw = parts[1].strip()
    if raw.lstrip("-").isdigit():
        ch_id_str = str(int(raw))
    else:
        ch_id_str = raw if raw.startswith("@") else f"@{raw}"
        try:
            chat      = await client.get_chat(ch_id_str)
            ch_id_str = str(chat.id)
        except Exception:
            pass

    result = db.channels.delete_many({"channel_id": ch_id_str})
    db.action_log.insert_one({
        "action":        "sudo_delchnl",
        "channel_id":    ch_id_str,
        "by_admin":      message.from_user.id,
        "ts":            time.time(),
        "deleted_count": result.deleted_count,
    })
    if result.deleted_count:
        await message.reply(
            f"<b><blockquote>✅ ʀᴇᴍᴏᴠᴇᴅ <code>{ch_id_str}</code> "
            f"ꜰʀᴏᴍ {result.deleted_count} ᴜsᴇʀ(s).</blockquote></b>"
        )
    else:
        await message.reply(
            f"<b><blockquote>❌ <code>{ch_id_str}</code> ɴᴏᴛ ꜰᴏᴜɴᴅ ɪɴ ᴀɴʏ ᴜsᴇʀ.</blockquote></b>"
        )


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    await app.start()
    me = await app.get_me()
    print(f"[Bot] Started as @{me.username}")

    feed_urls = get_config_feed_urls()
    print(f"[Bot] Config feeds: {feed_urls}")
    print(f"[Bot] FSUB:    {'enabled' if FSUB_CHANNEL_ID else 'disabled'}")
    print(f"[Bot] Sticker: {'enabled' if STICKER_ID else 'disabled'}")

    asyncio.create_task(global_news_loop(app, db))
    asyncio.create_task(user_channel_news_loop(app, db))

    await asyncio.Event().wait()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())