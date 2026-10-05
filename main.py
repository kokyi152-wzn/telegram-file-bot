import asyncio
import os
import re
import secrets
import string
import tempfile
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram import error as tg_error
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_ID", "").split(",") if x.strip()]
CHANNEL_ID = os.getenv("CHANNEL_ID")
# CHANNEL_IDS (comma-separated) is the master list of channels to post to.
# Falls back to the legacy CHANNEL_ID so old configs keep working.
CHANNEL_IDS = [int(x.strip()) for x in os.getenv("CHANNEL_IDS", "").split(",") if x.strip()]
_raw_channel_ids = CHANNEL_IDS or ([int(CHANNEL_ID)] if CHANNEL_ID else [])
# De-duplicate: the same chat_id listed twice would post every message twice.
POST_CHANNEL_IDS = list(dict.fromkeys(_raw_channel_ids))
if POST_CHANNEL_IDS:
    print(f"Posting to channels: {POST_CHANNEL_IDS}")
MONGODB_URI = os.getenv("MONGODB_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "telegram_bot")

if not ADMIN_IDS:
    raise SystemExit("ADMIN_ID environment variable is required (comma-separated IDs allowed)")

if not MONGODB_URI:
    raise SystemExit("MONGODB_URI environment variable is required")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


db_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
db = db_client[MONGO_DB_NAME]
files_col = db["files"]
files_col.create_index("created_at")


def remove_links_from_text(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"https?://[^\s]+", "", text)
    text = re.sub(r"www\.[^\s]+", "", text)
    text = re.sub(r"t\.me/[^\s]+", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def remove_hashtags(text: str) -> str:
    """Strip #hashtag words and '@mention' style noise, keeping the rest."""
    if not text:
        return text
    text = re.sub(r"#\S+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


_MYMEMORY_URL = "https://api.mymemory.translated.net/get"
_GOOGLE_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"


CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def contains_chinese(text: str) -> bool:
    return bool(text and CJK_RE.search(text))


_MYMEMORY_ERROR_MARKERS = ("LANGPAIR", "INVALID SOURCE", "MYMEMORY WARNING", "UNABLE TO", "NO QUOTA")


def _is_mymemory_error(text: str) -> bool:
    if not text:
        return False
    upper = text.upper()
    return any(marker in upper for marker in _MYMEMORY_ERROR_MARKERS)


async def _translate_text(text: str, target_lang: str) -> str:
    """Translate text to target_lang (MyMemory free API, Google fallback).

    Falls back to the original text on any error so posting never breaks.
    """
    if not text or not text.strip():
        return text
    if len(text.strip()) < 3:
        return text

    source_lang = "zh-CN" if contains_chinese(text) else "en"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            mymemory = await client.get(
                _MYMEMORY_URL,
                params={"q": text, "langpair": f"{source_lang}|{target_lang}"},
            )
            if mymemory.status_code == 200:
                data = mymemory.json()
                translated = (data.get("responseData") or {}).get("translatedText", "")
                if (
                    translated
                    and not data.get("quotaFinished")
                    and not _is_mymemory_error(translated)
                    and not (contains_chinese(text) and contains_chinese(translated))
                ):
                    return translated.strip()
    except Exception as e:
        print(f"MyMemory translation failed ({e}) — trying Google")

    params = {
        "client": "gtx",
        "sl": "auto",
        "tl": target_lang,
        "dt": "t",
        "q": text,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(_GOOGLE_TRANSLATE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        parts = []
        for seg in (data[0] or []):
            if seg and seg[0]:
                parts.append(seg[0])
        translated = "".join(parts).strip()
        return translated if translated else text
    except Exception as e:
        print(f"Google translation failed ({e}) — using original text")
        return text


async def translate_to_myanmar(text: str) -> str:
    return await _translate_text(text, "my")


def _clean_title(text: str) -> str:
    if not text:
        return ""
    text = remove_links_from_text(text)
    text = re.sub(r"@\w+", " ", text)
    text = remove_hashtags(text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip().strip("._ -")


def _file_stem(name: str) -> str:
    if not name:
        return ""
    return Path(_clean_title(name)).stem


async def make_movie_title(file_name: str, caption_fallback: str = "") -> str:
    """Channel title = the file's ORIGINAL name from the computer/drive.

    The Telegram caption is ignored. If the name is Chinese, it is posted as
    '<English> (<Myanmar>)'. Falls back to the cleaned caption only when the
    file has no name at all (e.g. bare Telegram photos).
    """
    name = _file_stem(file_name)
    if not name:
        name = _clean_title(caption_fallback)
    if not name:
        return ""
    if not contains_chinese(name):
        return name
    en = (await _translate_text(name, "en") or name).strip()
    my = (await _translate_text(name, "my") or name).strip()
    if not my or my == en:
        return en
    return f"{en} ({my})"


_CHANNEL_LOCK = asyncio.Lock()
_last_channel_post_time = 0.0
MIN_POST_INTERVAL = 1.2


async def _pace_channel_post():
    """Serialize + rate-limit every channel send (~1 msg/sec, flood-safe)."""
    global _last_channel_post_time
    async with _CHANNEL_LOCK:
        elapsed = asyncio.get_event_loop().time() - _last_channel_post_time
        if _last_channel_post_time and elapsed < MIN_POST_INTERVAL:
            await asyncio.sleep(MIN_POST_INTERVAL - elapsed)
        _last_channel_post_time = asyncio.get_event_loop().time()


async def run_with_retry(coro_factory, max_retries: int = 12, base_pace: float = MIN_POST_INTERVAL):
    """Run an async call, retrying on Telegram rate limits and network errors."""
    await _pace_channel_post()
    retries = 0
    while True:
        try:
            return await coro_factory()
        except tg_error.RetryAfter as e:
            wait = max(1, getattr(e, "retry_after", 1)) + 2
            print(f"Rate limited — waiting {wait}s (try {retries + 1}/{max_retries})")
            await asyncio.sleep(wait)
            retries += 1
            if retries >= max_retries:
                raise
        except (tg_error.TimedOut, tg_error.NetworkError) as e:
            wait = min(2 ** retries, 30) + 2
            print(f"Network error ({e}) — retrying in {wait}s (try {retries + 1}/{max_retries})")
            await asyncio.sleep(wait)
            retries += 1
            if retries >= max_retries:
                raise


def generate_id(length: int = 6) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def store_file(
    file_id: str,
    media_type: str,
    caption: str = "",
    filename: str = None,
    file_size: int = None,
) -> str:
    doc = {
        "file_id": file_id,
        "media_type": media_type,
        "caption": caption or "",
        "filename": filename,
        "file_size": file_size,
        "created_at": datetime.utcnow(),
    }
    for _ in range(5):
        _id = generate_id()
        if not files_col.find_one({"_id": _id}):
            doc["_id"] = _id
            files_col.insert_one(doc)
            return _id
    raise RuntimeError("Could not generate a unique id")


def store_album(items, caption: str = "") -> str:
    """Store a whole album (group of photos/videos) under one short id."""
    doc = {
        "media_type": "album",
        "caption": caption or "",
        "items": items,
        "created_at": datetime.utcnow(),
    }
    for _ in range(5):
        _id = generate_id()
        if not files_col.find_one({"_id": _id}):
            doc["_id"] = _id
            files_col.insert_one(doc)
            return _id
    raise RuntimeError("Could not generate a unique id")


def build_deeplink(bot_username: str, short_id: str) -> str:
    return f"https://t.me/{bot_username}?start={short_id}"


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📊 ဖိုင်စာရင်း", callback_data="menu_stats")],
        [InlineKeyboardButton("📁 ဖိုင်ပို့နည်း", callback_data="menu_add")],
        [InlineKeyboardButton("ℹ️ Bot အကြောင်း", callback_data="menu_help")],
    ]
    return InlineKeyboardMarkup(keyboard)


def clean_filename(filename: str, fallback_ext: str = "") -> str:
    if not filename:
        return f"movie{fallback_ext}"
    cleaned = remove_links_from_text(filename)
    if not cleaned:
        return f"movie{fallback_ext}"
    _, ext = os.path.splitext(cleaned)
    if not ext and fallback_ext:
        cleaned += fallback_ext
    return cleaned


def english_file_name(title: str, original_name: str) -> str:
    """English-only file name for channel posts (keeps the original extension).

    'English (မြန်မာ)' -> 'English' + original extension, so the download
    name on the channel is English even when the source file was Chinese.
    """
    ext = Path(original_name or "").suffix
    en = title or ""
    m = re.match(r"^(.*?)\s*\(", en)
    if m:
        en = m.group(1).strip()
    base = _clean_title(en) or Path(en).stem
    if not base:
        base = Path(original_name or "movie").stem
    return clean_filename(base, fallback_ext=ext or ".mp4")


AUTO_DELETE_SECONDS = 300  # deeplink deliveries self-destruct after 5 min


async def _delete_after_delay(bot, chat_id: int, message_id: int, delay: int):
    """Delete a delivered message after `delay` seconds (copyright safety)."""
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        print(f"Deleted deeplink delivery {message_id} after {delay}s")
    except tg_error.BadRequest as e:
        # Message already deleted / too old to delete — that's fine.
        print(f"Auto-delete skipped ({e})")
    except Exception as e:
        print(f"Auto-delete failed: {e}")


def _input_media(item, caption=None):
    """Build an InputMedia object for send_media_group."""
    if item.get("media_type") == "photo":
        return InputMediaPhoto(media=item["file_id"], caption=caption)
    if item.get("media_type") == "video":
        return InputMediaVideo(
            media=item["file_id"], caption=caption, supports_streaming=True
        )
    return InputMediaDocument(
        media=item["file_id"],
        caption=caption,
        filename=clean_filename(item.get("filename")),
    )


def _extract_media(message):
    if message.video:
        return "video", message.video
    if message.document:
        return "document", message.document
    if message.photo:
        return "photo", message.photo[-1]
    return None, None


ALBUM_WAIT = 1.5
ALBUM_MAX = 10
ALBUM_GRACE = 0.6  # short wait so late album parts still join the same group

# Every admin upload goes through ONE serial worker, so the channel always
# shows them in the exact order the admin sent them (albums included).
_MEDIA_QUEUE = None
_MEDIA_WORKER = None


def _ensure_media_worker():
    global _MEDIA_QUEUE, _MEDIA_WORKER
    if _MEDIA_WORKER is None or _MEDIA_WORKER.done():
        _MEDIA_QUEUE = asyncio.Queue()
        _MEDIA_WORKER = asyncio.create_task(_media_worker())
    return _MEDIA_QUEUE


def _enqueue_media(context, message, forwarded=False):
    """Queue one upload in arrival order instead of posting it immediately."""
    media_type, media = _extract_media(message)
    if media is None:
        return
    queue = _ensure_media_worker()
    album_key = None
    if getattr(message, "media_group_id", None):
        album_key = f"{message.chat.id}:{message.media_group_id}"
    queue.put_nowait(
        {
            "context": context,
            "message": message,
            "media_type": media_type,
            "media": media,
            "file_name": getattr(media, "file_name", None) or "",
            "caption": message.caption or "",
            "album_key": album_key,
            "forwarded": forwarded,
        }
    )


async def _media_worker():
    """Process queued uploads one at a time, strictly in the order received."""
    pending = None
    while True:
        entry = await _MEDIA_QUEUE.get()
        try:
            key = entry["album_key"]

            if pending and pending["key"] != key:
                await _publish_album(pending["context"], pending)
                pending = None

            if key is None:
                if entry["forwarded"]:
                    await _publish_forward_single(
                        entry["context"],
                        entry["message"],
                        entry["media_type"],
                        entry["media"],
                    )
                else:
                    await _process_single(
                        entry["context"],
                        entry["message"],
                        entry["media_type"],
                        entry["media"],
                    )
            else:
                if pending is None:
                    pending = {
                        "key": key,
                        "context": entry["context"],
                        "message": entry["message"],
                        "items": [],
                    }
                pending["items"].append(
                    {
                        "media_type": entry["media_type"],
                        "media": entry["media"],
                        "file_name": entry["file_name"],
                        "caption": entry["caption"],
                    }
                )
                pending["message"] = entry["message"]

            # Album tail: nothing else is waiting, so publish the group.
            if pending and _MEDIA_QUEUE.empty():
                await asyncio.sleep(ALBUM_GRACE)
                if _MEDIA_QUEUE.empty():
                    await _publish_album(pending["context"], pending)
                    pending = None
        except Exception as e:
            print(f"Media processing failed: {e}")
            try:
                await entry["message"].reply_text(
                    f"❌ တင်ရာမှာ error ဖြစ်နေပါတယ်။\n{e}"
                )
            except Exception:
                pass


async def _album_items_from(buf):
    items = []
    for it in buf["items"]:
        title = await make_movie_title(it["file_name"], it["caption"])
        items.append(
            {
                "file_id": it["media"].file_id,
                "media_type": it["media_type"],
                "filename": english_file_name(title, it["file_name"]),
                "title": title,
                "file_size": getattr(it["media"], "file_size", None),
            }
        )
    return items


async def _publish_album(context, buf):
    """Album from the admin (sent or forwarded): repost the whole group to every
    channel as an album, then hand out ONE deeplink for the entire group."""
    message = buf["message"]
    items = await _album_items_from(buf)
    caption = next((i["title"] for i in items if i["title"]), "")

    await _post_album_to_channels(context, buf["items"], items, caption)

    try:
        short_id = store_album(items, caption=caption)
    except Exception as e:
        print(f"Error storing album: {e}")
        await message.reply_text("❌ Database error ဖြစ်နေပါတယ်။")
        return

    deeplink = build_deeplink(context.bot.username, short_id)
    await message.reply_text(
        f"✅ Post {len(items)} ခု အုပ်စုလိုက် တင်ပြီးပါပြီ!\n"
        f"🔗 Deeplink: {deeplink}"
    )


async def _post_album_to_channels(context, buf_items, items, caption):
    """Repost an album to every channel, keeping the group layout."""
    last_file_id = None
    visual = [i for i in items if i["media_type"] in ("photo", "video")]
    others = [i for i in items if i["media_type"] not in ("photo", "video")]

    if len(visual) >= 2:
        for chat_id in POST_CHANNEL_IDS:
            for start in range(0, len(visual), ALBUM_MAX):
                chunk = visual[start : start + ALBUM_MAX]
                group = [
                    _input_media(item, caption if idx == 0 else None)
                    for idx, item in enumerate(chunk)
                ]
                try:
                    sent = await run_with_retry(
                        lambda group=group, chat_id=chat_id: context.bot.send_media_group(
                            chat_id=chat_id, media=group
                        )
                    )
                    first = sent[0]
                    last_file_id = (
                        first.photo[-1].file_id if first.photo else first.video.file_id
                    )
                    print(f"Posted album ({len(chunk)} items) to channel {chat_id}")
                except Exception as e:
                    print(f"Album post failed for channel {chat_id}: {e}")
    elif len(visual) == 1:
        item = visual[0]
        source = next(b for b in buf_items if b["file_id"] == item["file_id"])
        for chat_id in POST_CHANNEL_IDS:
            try:
                last_file_id = await send_media_to_channel(
                    context.bot,
                    chat_id,
                    source["media"],
                    item["media_type"],
                    caption,
                    item["filename"],
                )
            except Exception as e:
                print(f"Failed to post {item['media_type']} to {chat_id}: {e}")

    for item in others:
        source = next(b for b in buf_items if b["file_id"] == item["file_id"])
        for chat_id in POST_CHANNEL_IDS:
            try:
                last_file_id = await send_media_to_channel(
                    context.bot,
                    chat_id,
                    source["media"],
                    item["media_type"],
                    item["title"],
                    item["filename"],
                )
            except Exception as e:
                print(f"Failed to post {item['media_type']} to {chat_id}: {e}")

    if not last_file_id:
        raise RuntimeError("Album could not be posted to any channel")
    return last_file_id


async def _send_album(message, items, caption):
    """Deliver a stored album back to the user, preserving the group."""
    sent_messages = []
    visual = [i for i in items if i.get("media_type") in ("photo", "video")]
    others = [i for i in items if i.get("media_type") not in ("photo", "video")]

    async def _send_one(item):
        if item.get("media_type") == "video":
            return await message.reply_video(
                video=item["file_id"], caption=caption, supports_streaming=True
            )
        if item.get("media_type") == "document":
            return await message.reply_document(
                document=item["file_id"],
                filename=clean_filename(item.get("filename")),
                caption=caption,
            )
        return await message.reply_photo(photo=item["file_id"], caption=caption)

    if len(visual) >= 2:
        for start in range(0, len(visual), ALBUM_MAX):
            chunk = visual[start : start + ALBUM_MAX]
            group = [
                _input_media(item, caption if idx == 0 else None)
                for idx, item in enumerate(chunk)
            ]

            async def _send_group(group=group):
                return await message.reply_media_group(media=group)

            try:
                sent_messages.extend(await run_with_retry(_send_group))
            except Exception as e:
                print(f"Album delivery failed: {e}")
    elif len(visual) == 1:
        try:
            sent = await run_with_retry(lambda: _send_one(visual[0]))
            if sent:
                sent_messages.append(sent)
        except Exception as e:
            print(f"Album delivery failed: {e}")

    for item in others:
        try:
            sent = await run_with_retry(lambda item=item: _send_one(item))
            if sent:
                sent_messages.append(sent)
        except Exception as e:
            print(f"Album item delivery failed: {e}")

    if not sent_messages:
        await message.reply_text(
            "❌ ဖိုင်ပို့ရာမှာ error ဖြစ်နေပါတယ်။ ခဏကြာမှ ထပ်ကြိုးစားပါ။"
        )
        return

    for msg in sent_messages:
        asyncio.create_task(
            _delete_after_delay(
                msg.get_bot(), msg.chat_id, msg.message_id, AUTO_DELETE_SECONDS
            )
        )


async def send_file_by_doc(message, doc) -> None:
    media_type = doc["media_type"]
    caption = doc.get("caption") or None
    try:
        if media_type == "album":
            await _send_album(message, doc.get("items") or [], caption)
            return

        async def _send():
            if media_type == "video":
                return await message.reply_video(
                    video=doc["file_id"],
                    caption=caption,
                    supports_streaming=True,
                )
            elif media_type == "document":
                return await message.reply_document(
                    document=doc["file_id"],
                    filename=clean_filename(doc.get("filename")),
                    caption=caption,
                )
            elif media_type == "photo":
                return await message.reply_photo(photo=doc["file_id"], caption=caption)
            elif media_type == "text":
                return await message.reply_text(doc.get("caption") or "")
        sent = await run_with_retry(_send)
        if sent is not None:
            # Deliveries self-destruct after 5 minutes automatically.
            context_bot = sent.get_bot()
            asyncio.create_task(
                _delete_after_delay(
                    context_bot, sent.chat_id, sent.message_id, AUTO_DELETE_SECONDS
                )
            )
    except Exception as e:
        print(f"Error sending media to user: {e}")
        await message.reply_text(
            "❌ ဖိုင်ပို့ရာမှာ error ဖြစ်နေပါတယ်။ ခဏကြာမှ ထပ်ကြိုးစားပါ။"
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args:
        doc = files_col.find_one({"_id": args[0]})
        if doc:
            await send_file_by_doc(update.message, doc)
            return
        await update.message.reply_text("❌ ဒီဖိုင်ကို ရှာမတွေ့ပါ။ (link မှားနေနိုင်သည်)")
        return

    if is_admin(update.effective_user.id):
        await update.message.reply_text(
            "🎛 Admin ကြိုဆိုပါတယ်!\n\n"
            "📤 ဖိုင်/Video ပို့ပါ → Deeplink ထုတ်ပေးမယ်\n"
            "↩️ Forward ပြီး ပို့ပါ → Channel မှာ post တင်ပေးမယ်\n"
            "👥 ဘယ်သူမဆို ဒီဖိုင်တွေကို ရရှိနိုင်ပါတယ်\n\n"
            "🎛 Menu ကြည့်ရန်: /menu",
            reply_markup=admin_menu_keyboard(),
        )
    else:
        await update.message.reply_text(
            "ဒီ bot မှာ ဖိုင်/Video တွေကို deeplink ကနေတစ်ဆင့် ရရှိနိုင်ပါတယ်။\n"
            "Admin ပေးထားတဲ့ link ကို နှိပ်ပြီး ဖိုင်ကို ရယူပါ။"
        )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "🎛 Admin Menu ကြည့်ရန် အောက်က button တွေကို နှိပ်ပါ:",
        reply_markup=admin_menu_keyboard(),
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    data = query.data
    menu = admin_menu_keyboard()

    if data == "menu_stats":
        total = files_col.count_documents({})
        videos = files_col.count_documents({"media_type": "video"})
        docs = files_col.count_documents({"media_type": "document"})
        photos = files_col.count_documents({"media_type": "photo"})
        texts = files_col.count_documents({"media_type": "text"})
        await query.edit_message_text(
            "📊 ဖိုင်စာရင်း\n\n"
            f"🎬 Video: {videos}\n"
            f"📄 Document: {docs}\n"
            f"🖼 Photo: {photos}\n"
            f"📝 Text: {texts}\n\n"
            f"အားလုံးပေါင်း: {total}\n\n"
            "🔝 ပြန်ကြည့်ရန် အောက်က Menu ကို သုံးပါ:",
            reply_markup=menu,
        )
    elif data == "menu_add":
        await query.edit_message_text(
            "📁 ဖိုင်ပို့နည်း\n\n"
            "1️⃣ ဖိုင်/Video ကို ဒီ bot ထဲ ပို့ပါ\n"
            "   → Deeplink ထုတ်ပေးပါမယ်\n\n"
            "2️⃣ Forward ပြီး ပို့ပါ\n"
            "   → Channel မှာ movie post တင်ပြီး Deeplink ထုတ်ပေးပါမယ်\n\n"
            "⚡ ဖိုင်ကြီးတွေကိုလည်း အဆင်ပြေ ပြေတင်နိုင်ပါတယ်",
            reply_markup=menu,
        )
    elif data == "menu_help":
        await query.edit_message_text(
            "ℹ️ Bot အကြောင်း\n\n"
            "• Admin ပို့တဲ့ဖိုင် → Deeplink ထုတ်ပေး\n"
            "• Deeplink နှိပ်သူ → ဖိုင် ရရှိမယ်\n"
            "• Forward လုပ်ထားတဲ့ post → Channel မှာ တင်ပေး\n"
            "• ဖိုင်နာမည်များကို မူရင်းအတိုင်း ထားပေး",
            reply_markup=menu,
        )
    else:
        await query.edit_message_text(
            "🎛 Admin Menu ကြည့်ရန် အောက်က button တွေကို နှိပ်ပါ:",
            reply_markup=menu,
        )


async def _process_single(context, message, media_type, media):
    """Store one media file and reply with its deeplink."""
    file_name = getattr(media, "file_name", None) or ""
    title = await make_movie_title(file_name, message.caption or "")
    post_name = english_file_name(title, file_name)

    try:
        short_id = store_file(
            media.file_id,
            media_type,
            caption=title,
            filename=post_name,
            file_size=getattr(media, "file_size", None),
        )
    except Exception as e:
        print(f"Error storing {media_type}: {e}")
        await message.reply_text("❌ Database error ဖြစ်နေပါတယ်။")
        return

    deeplink = build_deeplink(context.bot.username, short_id)
    final_caption = f"{title}\n\n🔗 {deeplink}" if title else f"🔗 {deeplink}"

    try:
        if media_type == "video":
            await message.reply_video(
                video=media.file_id,
                caption=final_caption,
                supports_streaming=True,
            )
        elif media_type == "document":
            await message.reply_document(
                document=media.file_id,
                filename=clean_filename(post_name) if title else None,
                caption=final_caption,
            )
        else:
            await message.reply_photo(photo=media.file_id, caption=final_caption)
    except Exception as e:
        print(f"Error sending {media_type}: {e}")
        await message.reply_text("❌ ဖိုင်ပို့ရာမှာ error ဖြစ်နေပါတယ်။ ခဏကြာမှ ထပ်ကြိုးစားပါ။")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    _enqueue_media(context, update.message)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    _enqueue_media(context, update.message)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    _enqueue_media(context, update.message)


MAX_REUPLOAD_BYTES = 45 * 1024 * 1024  # Bot API InputFile upload limit = 50MB; keep 5MB margin


async def send_by_file_id(bot, chat_id, media, media_type, caption, original_name) -> str:
    """Send the original file_id straight to a channel (works up to 2GB)."""
    if media_type == "video":
        sent = await bot.send_video(
            chat_id=chat_id,
            video=media.file_id,
            caption=caption or None,
            supports_streaming=True,
        )
        return sent.video.file_id
    elif media_type == "document":
        sent = await bot.send_document(
            chat_id=chat_id,
            document=media.file_id,
            filename=clean_filename(original_name),
            caption=caption or None,
        )
        return sent.document.file_id
    elif media_type == "photo":
        sent = await bot.send_photo(
            chat_id=chat_id,
            photo=media.file_id,
            caption=caption or None,
        )
        return sent.photo[-1].file_id
    raise ValueError(f"Unsupported media type: {media_type}")


async def reupload_media(bot, chat_id, media, media_type, caption, original_name) -> str:
    """Download the file and re-upload it fresh, so the bot owns a copy.

    Only used for files that fit the 50MB InputFile upload limit.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        file = await bot.get_file(media.file_id)
        suffix_map = {
            "video": ".mp4",
            "document": Path(original_name).suffix
            or Path(file.file_path or "").suffix
            or ".bin",
            "photo": ".jpg",
        }
        suffix = suffix_map.get(media_type, ".bin")

        local_path = os.path.join(tmp_dir, f"movie_{media_type}{suffix}")
        await file.download_to_drive(custom_path=local_path)

        thumb_path = None
        thumb_obj = getattr(media, "thumbnail", None)
        if thumb_obj:
            try:
                thumb_file = await bot.get_file(thumb_obj.file_id)
                thumb_path = os.path.join(tmp_dir, "thumb.jpg")
                await thumb_file.download_to_drive(custom_path=thumb_path)
            except Exception:
                thumb_path = None

        clean_name = clean_filename(original_name, fallback_ext=suffix)

        if media_type == "video":
            sent = await bot.send_video(
                chat_id=chat_id,
                video=InputFile(local_path, filename=clean_name),
                caption=caption or None,
                supports_streaming=True,
                thumbnail=InputFile(thumb_path) if thumb_path else None,
            )
            return sent.video.file_id
        elif media_type == "document":
            sent = await bot.send_document(
                chat_id=chat_id,
                document=InputFile(local_path, filename=clean_name),
                caption=caption or None,
            )
            return sent.document.file_id
        elif media_type == "photo":
            sent = await bot.send_photo(
                chat_id=chat_id,
                photo=InputFile(local_path),
                caption=caption or None,
            )
            return sent.photo[-1].file_id
        raise ValueError(f"Unsupported media type: {media_type}")


async def send_media_to_channel(bot, chat_id, media, media_type, caption, original_name) -> str:
    """Re-upload small files fresh; send big files by file_id; photos by file_id."""
    file_size = getattr(media, "file_size", None)

    if media_type == "photo":
        return await run_with_retry(
            lambda: send_by_file_id(bot, chat_id, media, media_type, caption, original_name)
        )

    if file_size is not None and file_size > MAX_REUPLOAD_BYTES:
        return await run_with_retry(
            lambda: send_by_file_id(bot, chat_id, media, media_type, caption, original_name)
        )

    try:
        return await run_with_retry(
            lambda: reupload_media(bot, chat_id, media, media_type, caption, original_name)
        )
    except Exception as e:
        print(f"Re-upload failed ({e}); sending by file_id instead")
        return await run_with_retry(
            lambda: send_by_file_id(bot, chat_id, media, media_type, caption, original_name)
        )


async def post_to_all_channels(bot, media, media_type, caption, original_name) -> str:
    """Post the same media into every configured channel; return last file_id.

    Each channel gets the bot's OWN upload/post (never a Telegram 'forward'),
    so the post survives even if the original forward source channel dies.
    """
    last_file_id = None
    for chat_id in POST_CHANNEL_IDS:
        try:
            last_file_id = await send_media_to_channel(
                bot, chat_id, media, media_type, caption, original_name
            )
            print(f"Posted {media_type} to channel {chat_id}")
        except Exception as e:
            print(f"Failed to post to channel {chat_id}: {e}")
            print(traceback.format_exc())
    if not last_file_id:
        raise RuntimeError("Media could not be posted to any channel")
    return last_file_id


async def _publish_forward_single(context, msg, media_type, media):
    """Repost one forwarded file to the channels, then hand out its deeplink."""
    name = getattr(media, "file_name", None) or ""
    title = await make_movie_title(name, (msg.caption or "").strip())
    media_info = await _forward_media(context, media, media_type, title)

    if media_info:
        media_type, new_file_id, file_size, filename = media_info
        short_id = store_file(
            new_file_id,
            media_type,
            caption=title,
            filename=filename,
            file_size=file_size,
        )
        deeplink = build_deeplink(context.bot.username, short_id)
        await msg.reply_text("✅ Movie post တင်ပြီးပါပြီ!")
        await msg.reply_text(f"🔗 Deeplink: {deeplink}")


async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    msg = update.message
    raw_caption = (msg.caption or "").strip()

    media = msg.video or msg.document or (msg.photo and msg.photo[-1])
    if media is None:
        caption = await translate_to_myanmar(raw_caption)
        if not caption:
            return
        short_id = store_file("", "text", caption=caption)
        deeplink = build_deeplink(context.bot.username, short_id)
        for chat_id in POST_CHANNEL_IDS:
            await run_with_retry(
                lambda chat_id=chat_id: context.bot.send_message(
                    chat_id=chat_id, text=caption
                )
            )
        await msg.reply_text("✅ Post တင်ပြီးပါပြီ!")
        await msg.reply_text(f"🔗 Deeplink: {deeplink}")
        return

    _enqueue_media(context, msg, forwarded=True)


async def _forward_media(context, media, media_type, caption):
    original_name = getattr(media, "file_name", None) or media_type
    post_name = english_file_name(caption, original_name)
    new_file_id = await post_to_all_channels(
        context.bot, media, media_type, caption, post_name
    )
    return (media_type, new_file_id, getattr(media, "file_size", None), post_name)


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post:
        return

    # Never repost back into the channel the post came from — that would make
    # every message appear twice in the same channel.
    targets = [c for c in POST_CHANNEL_IDS if c != post.chat_id]
    if not targets:
        return

    text = remove_links_from_text(post.text or post.caption or "")
    text = await translate_to_myanmar(text)

    try:
        if post.video:
            for chat_id in targets:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=post.video.file_id,
                    caption=text or None,
                    supports_streaming=True,
                )
        elif post.document:
            for chat_id in targets:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=post.document.file_id,
                    caption=text or None,
                )
        elif post.photo:
            for chat_id in targets:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=post.photo[-1].file_id,
                    caption=text or None,
                )
    except Exception as e:
        print(f"Error posting to channels: {e}")


async def on_bot_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Unhandled error: {context.error}")
    print(traceback.format_exc())


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def start_health_server():
    """Render free spins services down after ~15 min idle.

    This tiny HTTP server gives UptimeRobot something to ping every 5 min so
    the polling bot stays awake.
    """
    port = int(os.getenv("PORT", "8000"))
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    except OSError as e:
        print(f"Health server could not bind port {port}: {e}")
        return
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Health server listening on port {port}")


def main():
    start_health_server()

    # Python 3.12+ no longer auto-creates an event loop; python-telegram-bot 21.6
    # relies on asyncio.get_event_loop() internally, which raises on Python 3.13/3.14.
    # Create and set one explicitly so run_polling works on any Python version.
    asyncio.set_event_loop(asyncio.new_event_loop())

    while True:
        app = Application.builder().token(BOT_TOKEN).build()

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("menu", cmd_menu))
        app.add_handler(CallbackQueryHandler(admin_callback))
        app.add_handler(MessageHandler(filters.VIDEO & ~filters.FORWARDED, handle_video))
        app.add_handler(MessageHandler(filters.Document.ALL & ~filters.FORWARDED, handle_document))
        app.add_handler(MessageHandler(filters.PHOTO & ~filters.FORWARDED, handle_photo))
        app.add_handler(MessageHandler(filters.FORWARDED, handle_forwarded))
        app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))
        app.add_error_handler(on_bot_error)

        restart_delay = 10
        try:
            print("Bot is running...")
            # drop_pending_updates: never replay stale queued updates after a restart,
            # otherwise a huge flood backlog reprocesses and re-triggers rate limits.
            app.run_polling(drop_pending_updates=True)
        except tg_error.Conflict as e:
            print(f"Polling stopped by conflict ({e}). Restarting in 15s...")
            restart_delay = 15
        except tg_error.TelegramError as e:
            print(f"Telegram error stopped polling ({e}). Restarting in 10s...")
        except Exception as e:
            print(f"Unexpected error stopped polling ({e}). Restarting in 10s...")

        # Recreate the event loop for each restart (the old loop may be closed).
        try:
            app.shutdown()
        except Exception:
            pass
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        time.sleep(restart_delay)


if __name__ == "__main__":
    main()