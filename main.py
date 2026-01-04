from dotenv import load_dotenv
import os
import json
import aiohttp
import asyncio
from typing import Final
from datetime import time as dtime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG
# =========================
load_dotenv()

TOKEN: Final = os.getenv("TG_BOT_TOKEN", "")
BOT_USERNAME: Final = "@wqannouncementsbot"

WQ_URL: Final = (
    "https://api.worldquantbrain.com/users/self/messages"
    "?type=ANNOUNCEMENT&order=-dateCreated&limit=50&offset=0"
)

WQ_COOKIE: Final = os.getenv("WQ_COOKIE", "")

WQ_HEADERS: Final = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://platform.worldquantbrain.com/",
    "Origin": "https://platform.worldquantbrain.com",
    "Cookie": WQ_COOKIE,  # must look like "t=..."
}

# Poll once a day at 00:00 US Eastern Time
TZ = ZoneInfo("America/New_York")
DAILY_POLL_TIME = dtime(hour=0, minute=0, tzinfo=TZ)

STATE_FILE = "state.json"

if not TOKEN:
    raise RuntimeError("Missing TG_BOT_TOKEN in .env")
if not WQ_COOKIE:
    raise RuntimeError("Missing WQ_COOKIE in .env (should be like: t=...)")

# =========================
# STATE (persistent)
# =========================
WATCHING: set[int] = set()         # chat_ids that want daily updates
LAST_SEEN: dict[int, str] = {}     # chat_id -> last announcement id
COOKIE_EXPIRED_NOTIFIED: set[int] = set()

_state_lock = asyncio.Lock()


def load_state():
    global WATCHING, LAST_SEEN, COOKIE_EXPIRED_NOTIFIED
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        WATCHING = set(int(x) for x in raw.get("watching", []))
        LAST_SEEN = {int(k): str(v) for k, v in raw.get("last_seen", {}).items()}
        COOKIE_EXPIRED_NOTIFIED = set(int(x) for x in raw.get("cookie_expired_notified", []))
    except Exception as e:
        print("State load error:", e)


async def save_state():
    async with _state_lock:
        raw = {
            "watching": sorted(list(WATCHING)),
            "last_seen": {str(k): v for k, v in LAST_SEEN.items()},
            "cookie_expired_notified": sorted(list(COOKIE_EXPIRED_NOTIFIED)),
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2)


# =========================
# WQ FETCH
# =========================
async def fetch_announcements() -> list[dict]:
    async with aiohttp.ClientSession(headers=WQ_HEADERS) as session:
        async with session.get(WQ_URL, timeout=30) as resp:
            if resp.status in (401, 403):
                raise RuntimeError("WQ_AUTH_EXPIRED")
            resp.raise_for_status()
            data = await resp.json()

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "data", "items", "messages"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    raise ValueError("Unexpected WQ response format")


def get_item_id(item: dict) -> str:
    return str(
        item.get("id")
        or item.get("uuid")
        or item.get("messageId")
        or item.get("dateCreated")
        or item.get("createdAt")
        or (item.get("title", "") + "|" + (item.get("body", "")[:30] if item.get("body") else ""))
    )


def format_announcement(item: dict) -> str:
    title = item.get("title") or item.get("subject") or "Announcement"
    body = item.get("body") or item.get("message") or item.get("text") or ""
    date = item.get("dateCreated") or item.get("createdAt") or ""

    body = (body or "").strip()
    if len(body) > 2500:
        body = body[:2500] + "…"

    msg = f"🧠 {title}"
    if date:
        msg += f"\n🗓 {date}"
    if body:
        msg += f"\n\n{body}"
    return msg.strip()


def announcements_since_last(items: list[dict], last_seen_id: str | None) -> tuple[list[dict], str]:
    """
    items are assumed newest -> oldest.
    Returns (new_items_oldest_first, newest_id).
    - If last_seen_id is None: returns ([], newest_id) to set baseline without spamming.
    - If last_seen_id not found: returns all items (oldest_first) and newest_id.
    """
    if not items:
        return ([], "")

    newest_id = get_item_id(items[0])

    if last_seen_id is None:
        return ([], newest_id)

    collected = []
    for it in items:
        if get_item_id(it) == last_seen_id:
            break
        collected.append(it)

    collected.reverse()  # send oldest -> newest
    return (collected, newest_id)


async def notify_cookie_expired(app: Application):
    for chat_id in list(WATCHING):
        if chat_id in COOKIE_EXPIRED_NOTIFIED:
            continue
        COOKIE_EXPIRED_NOTIFIED.add(chat_id)
        await app.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ WorldQuant session expired.\n\n"
                "Daily updates have been turned OFF to avoid spam.\n"
                "Fix: log in to BRAIN → copy a fresh `t=...` cookie → update `.env` → restart the bot → /watch again."
            ),
        )
    WATCHING.clear()
    await save_state()


# =========================
# DAILY JOB (sends ALL since last check)
# =========================
async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    if not WATCHING:
        return

    try:
        items = await fetch_announcements()
        if not items:
            return

        for chat_id in list(WATCHING):
            last_id = LAST_SEEN.get(chat_id)
            new_items, newest_id = announcements_since_last(items, last_id)

            # first time baseline: don't spam old announcements
            if last_id is None:
                LAST_SEEN[chat_id] = newest_id
                continue

            for it in new_items:
                await app.bot.send_message(chat_id=chat_id, text=format_announcement(it))

            LAST_SEEN[chat_id] = newest_id

        await save_state()

    except RuntimeError as e:
        if str(e) == "WQ_AUTH_EXPIRED":
            print("Daily job: WQ auth expired")
            await notify_cookie_expired(app)
        else:
            print("Daily job runtime error:", e)
    except Exception as e:
        print("Daily job error:", e)


# =========================
# COMMANDS
# =========================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "BRAIN Announcements Bot\n\n"
        "/watch    – enable daily updates (00:00 US Eastern)\n"
        "/unwatch  – disable daily updates\n"
        "/latest   – newest announcement now\n"
        "/recent5  – most recent 5 announcements now"
    )


async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    WATCHING.add(chat_id)
    COOKIE_EXPIRED_NOTIFIED.discard(chat_id)

    # baseline now so the next daily run doesn't spam old announcements
    try:
        items = await fetch_announcements()
        if items:
            LAST_SEEN[chat_id] = get_item_id(items[0])
    except RuntimeError as e:
        if str(e) == "WQ_AUTH_EXPIRED":
            WATCHING.discard(chat_id)
            await save_state()
            await update.message.reply_text(
                "⚠️ Cannot watch: WorldQuant session expired. Refresh WQ_COOKIE in .env and restart."
            )
            return

    await save_state()
    await update.message.reply_text("✅ Daily updates ON (runs at 00:00 US Eastern).")


async def unwatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    WATCHING.discard(chat_id)
    await save_state()
    await update.message.reply_text("🛑 Daily updates OFF.")


async def latest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        items = await fetch_announcements()
    except RuntimeError as e:
        if str(e) == "WQ_AUTH_EXPIRED":
            await update.message.reply_text(
                "⚠️ WorldQuant session expired. Refresh WQ_COOKIE in .env and restart."
            )
            return
        raise

    if not items:
        await update.message.reply_text("No announcements found.")
        return

    chat_id = update.effective_chat.id
    LAST_SEEN[chat_id] = get_item_id(items[0])
    await save_state()
    await update.message.reply_text(format_announcement(items[0]))


async def recent5_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        items = await fetch_announcements()
    except RuntimeError as e:
        if str(e) == "WQ_AUTH_EXPIRED":
            await update.message.reply_text(
                "⚠️ WorldQuant session expired. Refresh WQ_COOKIE in .env and restart."
            )
            return
        raise

    if not items:
        await update.message.reply_text("No announcements found.")
        return

    lines = []
    for it in items[:5]:
        title = it.get("title") or it.get("subject") or "Announcement"
        date = it.get("dateCreated") or it.get("createdAt") or ""
        lines.append(f"• {title} ({date})".strip())

    chat_id = update.effective_chat.id
    LAST_SEEN[chat_id] = get_item_id(items[0])
    await save_state()
    await update.message.reply_text("\n".join(lines))


# =========================
# MESSAGE HANDLER (optional)
# =========================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").lower()
    message_type = update.message.chat.type

    if message_type == "group":
        if BOT_USERNAME not in text:
            return

    await update.message.reply_text("Use /watch, /unwatch, /latest, /recent5.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("Error:", context.error)


# =========================
# MAIN
# =========================
def main():
    print("Starting bot...")
    load_state()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("watch", watch_command))
    app.add_handler(CommandHandler("unwatch", unwatch_command))
    app.add_handler(CommandHandler("latest", latest_command))
    app.add_handler(CommandHandler("recent5", recent5_command))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    # Schedule daily job at 00:00 US Eastern Time
    app.job_queue.run_daily(daily_job, time=DAILY_POLL_TIME, name="daily_wq_poll")

    print("Bot running (daily poll at 00:00 US Eastern)...")
    app.run_polling(poll_interval=3)


if __name__ == "__main__":
    main()