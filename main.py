import asyncio
import os
import re
import secrets
import string
import tempfile
import threading
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
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


async def send_file_by_doc(message, doc) -> None:
    media_type = doc["media_type"]
    caption = doc.get("caption") or None
    try:
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
        await run_with_retry(_send)
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


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    video = update.message.video
    caption = remove_links_from_text(update.message.caption or video.file_name or "")

    try:
        short_id = store_file(
            video.file_id,
            "video",
            caption=caption,
            filename=getattr(video, "file_name", None),
            file_size=getattr(video, "file_size", None),
        )
    except Exception as e:
        print(f"Error storing video: {e}")
        await update.message.reply_text("❌ Database error ဖြစ်နေပါတယ်။")
        return

    deeplink = build_deeplink(context.bot.username, short_id)
    final_caption = f"{caption}\n\n🔗 {deeplink}" if caption else f"🔗 {deeplink}"

    try:
        await update.message.reply_video(
            video=video.file_id,
            caption=final_caption,
            supports_streaming=True,
        )
    except Exception as e:
        print(f"Error sending video: {e}")
        await update.message.reply_text("❌ ဖိုင်ပို့ရာမှာ error ဖြစ်နေပါတယ်။ ခဏကြာမှ ထပ်ကြိုးစားပါ။")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    doc = update.message.document
    caption = remove_links_from_text(update.message.caption or doc.file_name or "")

    try:
        short_id = store_file(
            doc.file_id,
            "document",
            caption=caption,
            filename=doc.file_name,
            file_size=doc.file_size,
        )
    except Exception as e:
        print(f"Error storing document: {e}")
        await update.message.reply_text("❌ Database error ဖြစ်နေပါတယ်။")
        return

    deeplink = build_deeplink(context.bot.username, short_id)
    final_caption = f"{caption}\n\n🔗 {deeplink}" if caption else f"🔗 {deeplink}"

    try:
        await update.message.reply_document(
            document=doc.file_id,
            caption=final_caption,
        )
    except Exception as e:
        print(f"Error sending document: {e}")
        await update.message.reply_text("❌ ဖိုင်ပို့ရာမှာ error ဖြစ်နေပါတယ်။ ခဏကြာမှ ထပ်ကြိုးစားပါ။")


MAX_REUPLOAD_BYTES = 45 * 1024 * 1024  # Bot API InputFile upload limit = 50MB; keep 5MB margin


async def send_by_file_id(bot, media, media_type, caption, original_name) -> str:
    """Send the original file_id straight to the channel (works up to 2GB)."""
    if media_type == "video":
        sent = await bot.send_video(
            chat_id=CHANNEL_ID,
            video=media.file_id,
            caption=caption or None,
            supports_streaming=True,
        )
        return sent.video.file_id
    elif media_type == "document":
        sent = await bot.send_document(
            chat_id=CHANNEL_ID,
            document=media.file_id,
            filename=clean_filename(original_name),
            caption=caption or None,
        )
        return sent.document.file_id
    elif media_type == "photo":
        sent = await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=media.file_id,
            caption=caption or None,
        )
        return sent.photo[-1].file_id
    raise ValueError(f"Unsupported media type: {media_type}")


async def reupload_media(bot, media, media_type, caption, original_name) -> str:
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
                chat_id=CHANNEL_ID,
                video=InputFile(local_path, filename=clean_name),
                caption=caption or None,
                supports_streaming=True,
                thumbnail=InputFile(thumb_path) if thumb_path else None,
            )
            return sent.video.file_id
        elif media_type == "document":
            sent = await bot.send_document(
                chat_id=CHANNEL_ID,
                document=InputFile(local_path, filename=clean_name),
                caption=caption or None,
            )
            return sent.document.file_id
        elif media_type == "photo":
            sent = await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=InputFile(local_path),
                caption=caption or None,
            )
            return sent.photo[-1].file_id
        raise ValueError(f"Unsupported media type: {media_type}")


async def send_media_to_channel(bot, media, media_type, caption, original_name) -> str:
    """Re-upload small files fresh; send big files by file_id; photos by file_id."""
    file_size = getattr(media, "file_size", None)

    if media_type == "photo":
        return await run_with_retry(
            lambda: send_by_file_id(bot, media, media_type, caption, original_name)
        )

    if file_size is not None and file_size > MAX_REUPLOAD_BYTES:
        return await run_with_retry(
            lambda: send_by_file_id(bot, media, media_type, caption, original_name)
        )

    try:
        return await run_with_retry(
            lambda: reupload_media(bot, media, media_type, caption, original_name)
        )
    except Exception as e:
        print(f"Re-upload failed ({e}); sending by file_id instead")
        return await run_with_retry(
            lambda: send_by_file_id(bot, media, media_type, caption, original_name)
        )


async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    msg = update.message
    caption = remove_links_from_text(msg.caption or msg.text or "")
    caption = re.sub(r"@\w+", "", caption)
    caption = remove_links_from_text(caption)
    caption = remove_hashtags(caption)

    try:
        media_info = None
        if msg.video:
            media = msg.video
            media_type = "video"
            media_info = await _forward_media(context, media, media_type, caption)
        elif msg.document:
            media = msg.document
            media_type = "document"
            media_info = await _forward_media(context, media, media_type, caption)
        elif msg.photo:
            media = msg.photo[-1]
            media_type = "photo"
            media_info = await _forward_media(context, media, media_type, "")

        if media_info:
            media_type, new_file_id, file_size, filename = media_info
            short_id = store_file(
                new_file_id,
                media_type,
                caption="" if media_type == "photo" else caption,
                filename=filename,
                file_size=file_size,
            )
            deeplink = build_deeplink(context.bot.username, short_id)
            await msg.reply_text("✅ Movie post တင်ပြီးပါပြီ!")
            await msg.reply_text(f"🔗 Deeplink: {deeplink}")
            return

        if caption:
            short_id = store_file("", "text", caption=caption)
            deeplink = build_deeplink(context.bot.username, short_id)
            await run_with_retry(
                lambda: context.bot.send_message(chat_id=CHANNEL_ID, text=caption)
            )
            await msg.reply_text("✅ Post တင်ပြီးပါပြီ!")
            await msg.reply_text(f"🔗 Deeplink: {deeplink}")
    except Exception as e:
        print(f"Error posting forwarded (type={msg.video and 'video' or msg.document and 'document' or msg.photo and 'photo' or 'text'}): {e}")
        print(traceback.format_exc())
        await msg.reply_text("❌ Post တင်ရာမှာ error ဖြစ်နေပါတယ်။")


async def _forward_media(context, media, media_type, caption):
    original_name = getattr(media, "file_name", None) or media_type
    new_file_id = await send_media_to_channel(
        context.bot, media, media_type, caption, original_name
    )
    return (media_type, new_file_id, getattr(media, "file_size", None), original_name)


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post:
        return

    text = remove_links_from_text(post.text or post.caption or "")

    try:
        if post.video:
            await context.bot.send_video(
                chat_id=CHANNEL_ID,
                video=post.video.file_id,
                caption=text or None,
                supports_streaming=True,
            )
        elif post.document:
            await context.bot.send_document(
                chat_id=CHANNEL_ID,
                document=post.document.file_id,
                caption=text or None,
            )
    except Exception as e:
        print(f"Error posting to channel: {e}")


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

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CallbackQueryHandler(admin_callback))
    app.add_handler(MessageHandler(filters.VIDEO & ~filters.FORWARDED, handle_video))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.FORWARDED, handle_document))
    app.add_handler(MessageHandler(filters.FORWARDED, handle_forwarded))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))
    app.add_error_handler(on_bot_error)

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()