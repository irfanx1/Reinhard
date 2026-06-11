"""
module/rss/rss.py

ROOT CAUSE OF OLD-NEWS SPAM — FIXED
─────────────────────────────────────────────────────────────────────
The original code stored a SINGLE `last_sent_news_id` watermark per user
channel (e.g. taken from feed A). When the scheduler then checked feed B,
that watermark ID was never found in feed B's entries, so `_get_new_entries`
returned the entire feed (up to the cap) — ALL of which was old news.

HOW IT'S FIXED
─────────────────────────────────────────────────────────────────────
• PER-FEED watermarks: each user channel doc now stores
  `feed_watermarks: [{"url": "...", "wm": "last_entry_id"}, ...]`
  Every feed has its own independent watermark.

• `_get_new_entries` now returns (entries, found_bool).
  If the watermark is NOT in the feed (scrolled off) → ([], False).
  Callers update the watermark silently and send NOTHING.
  This alone eliminates the old-news burst.

• Init tick: ALL feed watermarks are set in one pass → `continue`.
  The channel never posts on the same tick it is initialised.

• NEW feeds added after a channel is created are also silently
  initialised on the next tick before any posting begins.

• ALL fetches are done ONCE per tick (not once per channel) to
  avoid redundant HTTP requests.

• FLOOD_WAIT → sleep the required duration, mark channel flooded,
  stop posting to that channel this tick.

• Duplicate detection within a tick uses a per-channel `seen_ids` set.

• Banned user channels are cleaned up on detection.
─────────────────────────────────────────────────────────────────────
"""

import re
import time
from urllib.parse import urlparse, parse_qs
import asyncio
import os
import feedparser
import aiohttp
from bs4 import BeautifulSoup
import yt_dlp
from pyrogram import Client

from config import STICKER_ID

# Hard cap: max entries sent per feed per tick (prevents burst after long downtime)
_MAX_ENTRIES_PER_TICK = 3


# ─── YouTube helpers ──────────────────────────────────────────────────────────

def extract_youtube_watch_url(yt_url: str) -> str:
    if "youtube.com/embed/" in yt_url or "youtube-nocookie.com/embed/" in yt_url:
        video_id = yt_url.split("/embed/")[-1].split("?")[0].split("/")[0]
        return f"https://www.youtube.com/watch?v={video_id}"
    if "youtube.com/watch" in yt_url:
        parsed = urlparse(yt_url)
        v = parse_qs(parsed.query).get("v", [""])[0]
        if v:
            return f"https://www.youtube.com/watch?v={v}"
    return yt_url


async def find_youtube_iframe(page_url: str):
    CONTENT_SELECTORS = [
        ".news-body", ".entry-content", "article", "main", "#content", ".content"
    ]
    YT_DOMAINS = ("youtube.com", "youtube-nocookie.com")

    def _is_yt(src):
        return src and any(d in src for d in YT_DOMAINS)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(page_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")

        iframe = None
        for sel in CONTENT_SELECTORS:
            block = soup.select_one(sel)
            if block:
                iframe = block.find("iframe", src=lambda s: _is_yt(s))
                if iframe:
                    break
        if not iframe:
            iframe = soup.find("iframe", src=lambda s: _is_yt(s))
        if not iframe:
            return None

        yt_url = iframe["src"]
        if yt_url.startswith("//"):
            yt_url = "https:" + yt_url
        elif yt_url.startswith("/"):
            yt_url = "https://www.youtube.com" + yt_url
        return extract_youtube_watch_url(yt_url)
    except Exception as e:
        print(f"[YouTube] Error scanning {page_url}: {e}")
    return None


async def download_and_send_video(
    app: Client,
    chat_id,
    yt_url: str,
    caption: str,
    safe_id: str,
) -> bool:
    ydl_opts = {
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "outtmpl": f"/tmp/ytvideo_{safe_id}.%(ext)s",
        "quiet": True,
        "merge_output_format": "mp4",
    }
    if os.path.exists("./cookies.txt"):
        ydl_opts["cookiefile"] = "./cookies.txt"

    video_path = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, yt_url, True)
            video_path = ydl.prepare_filename(info)
        if not os.path.exists(video_path):
            mp4 = video_path.rsplit(".", 1)[0] + ".mp4"
            if os.path.exists(mp4):
                video_path = mp4
        await app.send_video(chat_id=chat_id, video=video_path, caption=caption)
        return True
    except Exception as e:
        print(f"[YouTube] yt-dlp failed for {yt_url}: {e}")
        return False
    finally:
        if video_path and os.path.exists(video_path):
            try:
                os.remove(video_path)
            except Exception:
                pass


# ─── Thumbnail helper ─────────────────────────────────────────────────────────

async def get_page_thumbnail(page_url: str):
    def valid(src):
        return src and src.startswith("http") and "spacer.gif" not in src

    SELECTORS = [".news-body", ".entry-content", "article", "main", "#content", ".content"]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(page_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")

        og = soup.find("meta", property="og:image")
        if og and valid(og.get("content")):
            return og["content"]

        fig = soup.find("figure")
        if fig:
            img = fig.find("img")
            if img:
                src = img.get("data-src") or img.get("src")
                if valid(src):
                    return src

        for sel in SELECTORS:
            block = soup.select_one(sel)
            if block:
                img = block.find("img")
                if img:
                    src = img.get("data-src") or img.get("src")
                    if valid(src):
                        return src
    except Exception as e:
        print(f"[Thumbnail] {page_url}: {e}")
    return None


# ─── RSS entry formatter ──────────────────────────────────────────────────────

async def format_rss_entry(entry, is_admin_channel: bool = False) -> tuple:
    """
    Returns (msg_html, thumbnail_url_or_None, link).
    is_admin_channel=True  → branded ◈ footer (no Read Full News link)
    is_admin_channel=False → standard Read Full News link
    """
    title = entry.get("title", "No Title")
    link  = entry.get("link", "")

    raw = entry.get("summary", "")
    if raw:
        summary = BeautifulSoup(raw, "html.parser").get_text(separator=" ", strip=True)
        if len(summary) > 800:
            summary = summary[:800] + "…"
    else:
        summary = ""

    thumbnail_url = None
    mt = getattr(entry, "media_thumbnail", None)
    if mt:
        thumbnail_url = mt[0].get("url")
    if not thumbnail_url and link:
        thumbnail_url = await get_page_thumbnail(link)

    if is_admin_channel:
        footer = (
            "<b><blockquote>━━━━━━━ ◈"
            "<a href='https://t.me/BotifyX_Pro_Botz'>Bᴏᴛɪғʏx ʙᴏᴛs</a>"
            "◈ ━━━━━━━</blockquote></b>"
        )
    else:
        footer = f"<b><blockquote><a href='{link}'>Read Full News</a></blockquote></b>"

    msg = (
        f"<b><blockquote>{title}</blockquote></b>\n"
        f"<blockquote expandable><i>{summary}</i></blockquote>\n"
        + footer
    )
    return msg, thumbnail_url, link


# ─── Config feed URLs ────────────────────────────────────────────────────────

def get_config_feed_urls() -> list:
    import config as _cfg
    urls = []
    for attr in sorted(dir(_cfg)):
        if attr.startswith("URL_") and len(attr) == 5:
            val = getattr(_cfg, attr, "")
            if isinstance(val, str) and val.strip():
                urls.append(val.strip())
    return list(dict.fromkeys(urls))


# ─── Flood-wait helper ────────────────────────────────────────────────────────

def _parse_flood_wait(exc: Exception) -> int:
    msg = str(exc)
    m = re.search(r"wait of (\d+) second", msg, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"FLOOD_WAIT[_X]*[^0-9]*(\d+)", msg)
    if m:
        return int(m.group(1))
    return 0


# ─── New entry detector ───────────────────────────────────────────────────────

def _get_new_entries(feed_entries: list, last_sent_id: str) -> tuple:
    """
    Returns (new_entries, watermark_found).

    new_entries     — list of entries strictly newer than last_sent_id,
                      newest-first, hard-capped at _MAX_ENTRIES_PER_TICK.
    watermark_found — True  if last_sent_id was found in the feed.
                      False if it has scrolled off (feed refreshed past it).

    CRITICAL: When watermark_found is False we return ([], False).
    Callers MUST update the watermark silently and send NOTHING.
    This prevents old-news spam after a stale watermark.
    """
    new = []
    for entry in feed_entries:
        eid = entry.get("id") or entry.get("link", "")
        if eid == last_sent_id:
            return new[:_MAX_ENTRIES_PER_TICK], True   # watermark found
        new.append(entry)
    # Watermark not found anywhere in the feed → scrolled off
    return [], False


# ─── Core sender ─────────────────────────────────────────────────────────────

async def _post_entry(app: Client, chat_id, entry, msg: str, thumbnail_url, link: str) -> bool:
    """
    Send one formatted news card to a channel.
    Returns True on success, False on any failure.
    On FLOOD_WAIT the function sleeps the required seconds before returning False.
    """
    ch = chat_id
    try:
        ch = int(chat_id)
    except (ValueError, TypeError):
        pass

    try:
        if thumbnail_url:
            await app.send_photo(chat_id=ch, photo=thumbnail_url, caption=msg)
        else:
            await app.send_message(chat_id=ch, text=msg, disable_web_page_preview=True)
    except Exception as e:
        err = str(e)
        if "FLOOD_WAIT" in err:
            wait = _parse_flood_wait(e)
            sleep_for = max(wait, 5) + 3
            print(f"[Post] FLOOD_WAIT on {ch}: sleeping {sleep_for}s.")
            await asyncio.sleep(sleep_for)
        else:
            print(f"[Post] Error sending to {ch}: {e}")
        return False

    # YouTube embed (best-effort, never blocks the news card)
    if link:
        yt_url = await find_youtube_iframe(link)
        if yt_url:
            entry_id = entry.get("id") or entry.get("link", "")
            safe_id  = "".join(c for c in str(entry_id) if c.isalnum())[-20:]
            title    = entry.get("title", "")
            await download_and_send_video(
                app, ch, yt_url,
                f"<b><blockquote>{title}</blockquote></b>",
                safe_id,
            )

    # Sticker
    if STICKER_ID:
        try:
            await app.send_sticker(chat_id=ch, sticker=STICKER_ID)
        except Exception as e:
            print(f"[Sticker] Could not send to {ch}: {e}")

    return True


# ─── Feed state helpers (global scheduler) ────────────────────────────────────

def _get_feed_state(db, feed_url: str):
    return db.feed_state.find_one({"feed_url": feed_url})

def _init_feed_state(db, feed_url: str, latest_id: str):
    db.feed_state.update_one(
        {"feed_url": feed_url},
        {"$set": {"feed_url": feed_url, "last_sent_id": latest_id, "initialized": True}},
        upsert=True,
    )
    print(f"[Global] Feed initialised (no backlog): {feed_url!r}")

def _update_feed_state(db, feed_url: str, new_last_id: str):
    db.feed_state.update_one(
        {"feed_url": feed_url},
        {"$set": {"last_sent_id": new_last_id}},
        upsert=True,
    )


# ─── Per-feed watermark helpers (user channel scheduler) ─────────────────────

def _load_wm_dict(ch_doc: dict) -> dict:
    """
    Load the per-feed watermarks from a channel doc.
    Schema: feed_watermarks = [{"url": str, "wm": str}, ...]
    Returns a plain dict: {feed_url: last_entry_id}
    """
    wm_list = ch_doc.get("feed_watermarks", [])
    return {item["url"]: item["wm"] for item in wm_list if item.get("url") and item.get("wm")}

def _build_wm_list(wm_dict: dict) -> list:
    """Convert {feed_url: wm_id} back to the list format for MongoDB."""
    return [{"url": url, "wm": wm} for url, wm in wm_dict.items()]


# ─── Global scheduler (admin channels) ───────────────────────────────────────

async def _global_tick(app: Client, db):
    """One fetch cycle: post new articles to all admin channels."""
    admin_channels = [doc["channel_id"] for doc in db.admin_channels.find({})]
    if not admin_channels:
        return

    config_feeds = get_config_feed_urls()
    db_feeds     = [doc["url"] for doc in db.rss_feeds.find({})]
    all_feeds    = list(dict.fromkeys(config_feeds + db_feeds))

    flooded_channels: set = set()

    for feed_url in all_feeds:
        try:
            feed = await asyncio.to_thread(feedparser.parse, feed_url)
        except Exception as e:
            print(f"[Global] Could not parse {feed_url}: {e}")
            continue

        if not feed.entries:
            continue

        state = _get_feed_state(db, feed_url)
        if not state:
            # First encounter — mark current top as watermark, post nothing
            latest_id = feed.entries[0].get("id") or feed.entries[0].get("link", "")
            _init_feed_state(db, feed_url, latest_id)
            continue

        new_entries, found = _get_new_entries(feed.entries, state["last_sent_id"])

        if not found:
            # Watermark scrolled off — silently advance, send nothing
            latest_id = feed.entries[0].get("id") or feed.entries[0].get("link", "")
            print(f"[Global] Watermark scrolled off for {feed_url!r} — updating silently.")
            _update_feed_state(db, feed_url, latest_id)
            continue

        if not new_entries:
            continue

        # Send oldest-first
        for entry in reversed(new_entries):
            entry_id = entry.get("id") or entry.get("link", "")

            # Global dedup — same entry_id never sent twice regardless of feed
            if db.sent_news.find_one({"entry_id": entry_id}):
                continue

            msg, thumbnail_url, link = await format_rss_entry(entry, is_admin_channel=True)
            sent_ok = False

            for ch in admin_channels:
                if ch in flooded_channels:
                    print(f"[Global] Skipping flooded channel {ch} this tick.")
                    continue
                ok = await _post_entry(app, ch, entry, msg, thumbnail_url, link)
                if ok:
                    sent_ok = True
                else:
                    flooded_channels.add(ch)
                await asyncio.sleep(2)

            if sent_ok:
                try:
                    db.sent_news.insert_one({
                        "entry_id": entry_id,
                        "title": entry.get("title", ""),
                    })
                except Exception:
                    pass  # duplicate key race — safe to ignore
                print(f"[Global] Sent: {entry.get('title', '')!r}")

            await asyncio.sleep(2)

        # Advance watermark to the newest article just processed
        newest_id = new_entries[0].get("id") or new_entries[0].get("link", "")
        _update_feed_state(db, feed_url, newest_id)


async def global_news_loop(app: Client, db):
    print("[Global Scheduler] Started.")
    while True:
        try:
            await _global_tick(app, db)
        except Exception as e:
            print(f"[Global Scheduler] Unexpected error: {e}")
        config   = db.global_settings.find_one({"_id": "config"}) or {}
        interval = max(60, int(config.get("interval", 300)))
        print(f"[Global Scheduler] Next check in {interval}s…")
        await asyncio.sleep(interval)


# ─── User channel scheduler ───────────────────────────────────────────────────

async def _user_channel_tick(app: Client, db):
    """
    Check every user channel.
    Each channel uses PER-FEED watermarks so a watermark from feed A
    never contaminates the new-entry detection for feed B.
    """
    now = time.time()

    config_feeds = get_config_feed_urls()
    db_feeds     = [doc["url"] for doc in db.rss_feeds.find({})]
    all_feeds    = list(dict.fromkeys(config_feeds + db_feeds))

    if not all_feeds:
        return

    # Pre-fetch ALL feeds ONCE so we don't re-HTTP for every channel
    fetched: dict = {}   # feed_url → parsed feed (or None)
    for feed_url in all_feeds:
        try:
            f = await asyncio.to_thread(feedparser.parse, feed_url)
            fetched[feed_url] = f if f.entries else None
        except Exception as e:
            print(f"[User] Could not parse {feed_url}: {e}")
            fetched[feed_url] = None

    for ch_doc in list(db.channels.find({})):
        user_id = ch_doc.get("user_id")

        # ── Ban check — clean up and skip ────────────────────────────
        user_doc = db.users.find_one({"user_id": user_id})
        if user_doc and user_doc.get("is_banned"):
            db.channels.delete_many({"user_id": user_id})
            print(f"[User] Removed all channels for banned user {user_id}.")
            continue

        # ── Interval throttle ─────────────────────────────────────────
        interval_secs = int(ch_doc.get("interval", 300))
        last_posted   = ch_doc.get("last_posted_at", 0)
        if now - last_posted < interval_secs:
            continue

        channel_id = ch_doc["channel_id"]

        # ── Load per-feed watermarks ──────────────────────────────────
        # Schema: feed_watermarks = [{"url": str, "wm": str}, ...]
        wm_dict = _load_wm_dict(ch_doc)

        # Detect channels that need full initialization:
        # • brand-new (no feed_watermarks key) OR
        # • migrated from old schema (has last_sent_news_id but no feed_watermarks)
        needs_full_init = (
            "feed_watermarks" not in ch_doc
            or len(wm_dict) == 0
        )

        if needs_full_init:
            # Set watermark to current top entry for EVERY feed, send nothing
            new_wm: dict = {}
            for feed_url, feed in fetched.items():
                if feed and feed.entries:
                    eid = feed.entries[0].get("id") or feed.entries[0].get("link", "")
                    if eid:
                        new_wm[feed_url] = eid
            db.channels.update_one(
                {"_id": ch_doc["_id"]},
                {"$set": {
                    "feed_watermarks":   _build_wm_list(new_wm),
                    "last_sent_news_id": None,   # clear old-schema field
                    "last_posted_at":    now,
                }},
            )
            print(
                f"[User] Channel {channel_id} initialised with watermarks for "
                f"{len(new_wm)} feed(s). No posts sent on init tick."
            )
            continue   # ← ALWAYS skip posting on the init tick

        # ── Collect new entries per feed ──────────────────────────────
        # Each item: (feed_url, entry_obj)
        new_entries_tagged: list = []
        watermarks_to_update: dict = {}

        for feed_url, feed in fetched.items():
            if not feed or not feed.entries:
                continue

            current_latest = feed.entries[0].get("id") or feed.entries[0].get("link", "")
            existing_wm    = wm_dict.get(feed_url)

            if not existing_wm:
                # New feed added after this channel was initialised — init silently
                watermarks_to_update[feed_url] = current_latest
                print(f"[User] Channel {channel_id}: new feed {feed_url!r} — init watermark, no send.")
                continue

            if existing_wm == current_latest:
                # No change since last check
                continue

            new_entries, found = _get_new_entries(feed.entries, existing_wm)

            if not found:
                # Watermark scrolled off the feed — advance silently, no posts
                watermarks_to_update[feed_url] = current_latest
                print(
                    f"[User] Channel {channel_id}: watermark for {feed_url!r} "
                    "scrolled off — updating silently, no old posts sent."
                )
                continue

            if new_entries:
                for e in new_entries:
                    new_entries_tagged.append((feed_url, e))
                # Advance watermark to the newest new entry
                newest = new_entries[0].get("id") or new_entries[0].get("link", "")
                watermarks_to_update[feed_url] = newest

        # ── Per-tick deduplication across feeds ───────────────────────
        # Same entry_id from two feeds in the same batch → send only once
        seen_ids: set = set()
        unique_to_send: list = []
        for feed_url, entry in new_entries_tagged:
            eid = entry.get("id") or entry.get("link", "")
            if eid not in seen_ids:
                seen_ids.add(eid)
                unique_to_send.append((feed_url, entry))

        # Send oldest-first (new_entries_tagged is newest-first)
        unique_to_send = list(reversed(unique_to_send))

        channel_kicked  = False
        channel_flooded = False

        for feed_url, entry in unique_to_send:
            if channel_kicked or channel_flooded:
                break

            msg, thumbnail_url, link = await format_rss_entry(entry, is_admin_channel=False)

            try:
                ok = await _post_entry(app, channel_id, entry, msg, thumbnail_url, link)
            except Exception as exc:
                err = str(exc).lower()
                print(f"[User] Exception posting to {channel_id}: {exc}")
                if "chat not found" in err or "forbidden" in err:
                    db.channels.delete_one({"_id": ch_doc["_id"]})
                    print(f"[User] Auto-removed {channel_id} (bot kicked).")
                    channel_kicked = True
                continue

            if ok:
                print(f"[User] → {channel_id}: {entry.get('title', '')!r}")
            else:
                channel_flooded = True  # _post_entry already slept the flood wait

            await asyncio.sleep(3)

        if channel_kicked:
            continue   # Don't write watermarks for a removed channel

        # ── Persist updated watermarks ────────────────────────────────
        merged_wm = dict(wm_dict)
        merged_wm.update(watermarks_to_update)

        db.channels.update_one(
            {"_id": ch_doc["_id"]},
            {"$set": {
                "feed_watermarks": _build_wm_list(merged_wm),
                "last_posted_at":  now,
            }},
        )


async def user_channel_news_loop(app: Client, db):
    print("[User Scheduler] Started.")
    while True:
        try:
            await _user_channel_tick(app, db)
        except Exception as e:
            print(f"[User Scheduler] Unexpected error: {e}")
        await asyncio.sleep(60)   # granularity: 1 min; actual interval is per-channel