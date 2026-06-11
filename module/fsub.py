"""
module/fsub.py

Force-subscription guard.

check_fsub(client, message) → bool
  True  = user may proceed
  False = FSUB message sent; caller must abort the command
"""

import asyncio
import random

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus          # ← FIX: import enum
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import FSUB_CHANNEL_ID, FSUB_CHANNEL_USERNAME, START_PICS

# ── Valid statuses that mean "user is in the channel" ──────────────────────
_JOINED = (
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
)


async def check_fsub(client: Client, message: Message) -> bool:
    """
    Returns True if FSUB is disabled or the user is already a member.
    Returns False after sending the join-prompt (with auto-delete in 5 min).
    """
    if not FSUB_CHANNEL_ID:
        return True   # FSUB not configured

    try:
        member = await client.get_chat_member(FSUB_CHANNEL_ID, message.from_user.id)
        # FIX: compare against ChatMemberStatus enum values, not plain strings
        if member.status in _JOINED:
            return True
    except Exception:
        pass  # not a member or any other error → treat as not joined

    # ── Build FSUB prompt ──────────────────────────────────────────────
    pic = random.choice(START_PICS) if START_PICS else None
    caption = (
        "<b><blockquote>"
        "ʏᴏᴜ ᴍᴜsᴛ ᴊᴏɪɴ ᴏᴜʀ ᴄʜᴀɴɴᴇʟ ᴛᴏ ᴜsᴇ ᴛʜɪs ʙᴏᴛ!\n"
        "ᴄʟɪᴄᴋ ᴊᴏɪɴ, ᴛʜᴇɴ ʜɪᴛ ʀᴇᴛʀʏ."
        "</blockquote></b>"
    )
    ch_url = f"https://t.me/{FSUB_CHANNEL_USERNAME}"
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 ᴊᴏɪɴ ᴄʜᴀɴɴᴇʟ", url=ch_url)],
        [InlineKeyboardButton("🔄 ʀᴇᴛʀʏ", callback_data="fsub:retry")],
    ])

    try:
        if pic:
            sent = await message.reply_photo(pic, caption=caption, reply_markup=markup)
        else:
            sent = await message.reply(caption, reply_markup=markup)
    except Exception:
        return True   # can't send prompt → let them through

    # Auto-delete after 5 minutes regardless of action
    async def _auto_delete():
        await asyncio.sleep(300)
        try:
            await sent.delete()
        except Exception:
            pass

    asyncio.create_task(_auto_delete())
    return False


async def recheck_fsub(client: Client, user_id: int) -> bool:
    """Used by the Retry callback — returns True if user has now joined."""
    if not FSUB_CHANNEL_ID:
        return True
    try:
        member = await client.get_chat_member(FSUB_CHANNEL_ID, user_id)
        # FIX: compare against ChatMemberStatus enum values, not plain strings
        return member.status in _JOINED
    except Exception:
        return False
