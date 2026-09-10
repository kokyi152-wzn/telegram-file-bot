import os
import re
import secrets
import string
import tempfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import InputFile, Update
from telegram.ext import (
    Application,
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


def clean_filename(filename: str) -> str:
    if not filename:
        return "movie"
    return remove_links_from_text(filename) or "movie"


async def send_file_by_doc(message, doc) -> None:
    media_type = doc["media_type"]
    caption = doc.get("caption") or None
    try:
        if media_type == "video":
            await message.reply_video(
                video=doc["file_id"],
                caption=caption,
                supports_streaming=True,
            )
        elif media_type == "document":
            await message.reply_document(
                document=doc["file_id"],
                filename=clean_filename(doc.get("filename")),
                caption=caption,
            )
        elif media_type == "photo":
            await message.reply_photo(photo=doc["file_id"], caption=caption)
        elif media_type == "text":
            await message.reply_text(doc.get("caption") or "")
    except Exception as e:
        print(f"Error sending media to user: {e}")
        await message.reply_text(
            "âŒ á€–á€­á€¯á€„á€ºá€•á€­á€¯á€·á€›á€¬á€™á€¾á€¬ error á€–á€¼á€…á€ºá€”á€±á€•á€«á€á€šá€ºá‹ á€á€á€€á€¼á€¬á€™á€¾ á€‘á€•á€ºá€€á€¼á€­á€¯á€¸á€…á€¬á€¸á€•á€«á‹"
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args:
        doc = files_col.find_one({"_id": args[0]})
        if doc:
            await send_file_by_doc(update.message, doc)
            return
        await update.message.reply_text("âŒ á€’á€®á€–á€­á€¯á€„á€ºá€€á€­á€¯ á€›á€¾á€¬á€™á€á€½á€±á€·á€•á€«á‹ (link á€™á€¾á€¬á€¸á€”á€±á€”á€­á€¯á€„á€ºá€žá€Šá€º)")
        return

    if is_admin(update.effective_user.id):
        await update.message.reply_text(
            "Welcome Admin!\n\n"
            "á€–á€­á€¯á€„á€º/Video á€•á€­á€¯á€·á€•á€« - Deeplink á€‘á€¯á€á€ºá€•á€±á€¸á€™á€šá€º\n"
            "Forward á€•á€¼á€®á€¸ á€•á€­á€¯á€·á€•á€« - Channel á€™á€¾á€¬ post á€á€„á€ºá€•á€±á€¸á€™á€šá€º\n"
            "á€˜á€šá€º bot á€€á€­á€¯á€™á€†á€­á€¯ start á€œá€¯á€•á€ºá€œá€­á€¯á€·á€›á€á€²á€· á€œá€°á€á€­á€¯á€„á€ºá€¸ á€–á€­á€¯á€„á€ºá€›á€”á€­á€¯á€„á€ºá€•á€«á€á€šá€º"
        )
    else:
        await update.message.reply_text(
            "á€’á€® bot á€€ movie/á€–á€­á€¯á€„á€ºá€á€½á€±á€€á€­á€¯ deeplink á€€á€”á€±á€á€…á€ºá€†á€„á€·á€º á€›á€›á€¾á€­á€”á€­á€¯á€„á€ºá€á€²á€· bot á€•á€«á‹\n"
            "Admin á€•á€±á€¸á€‘á€¬á€¸á€á€²á€· link á€€á€”á€± á€–á€­á€¯á€„á€ºá€›á€šá€°á€•á€«á‹"
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
        await update.message.reply_text("âŒ Database error á€–á€¼á€…á€ºá€”á€±á€•á€«á€á€šá€ºá‹")
        return

    deeplink = build_deeplink(context.bot.username, short_id)
    final_caption = f"{caption}\n\nðŸ”— {deeplink}" if caption else f"ðŸ”— {deeplink}"

    try:
        await update.message.reply_video(
            video=video.file_id,
            caption=final_caption,
            supports_streaming=True,
        )
    except Exception as e:
        print(f"Error sending video: {e}")
        await update.message.reply_text("Error á€–á€¼á€…á€ºá€”á€±á€•á€«á€á€šá€ºá‹")


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
        await update.message.reply_text("âŒ Database error á€–á€¼á€…á€ºá€”á€±á€•á€«á€á€šá€ºá‹")
        return

    deeplink = build_deeplink(context.bot.username, short_id)
    final_caption = f"{caption}\n\nðŸ”— {deeplink}" if caption else f"ðŸ”— {deeplink}"

    try:
        await update.message.reply_document(
            document=doc.file_id,
            caption=final_caption,
        )
    except Exception as e:
        print(f"Error sending document: {e}")
        await update.message.reply_text("Error á€–á€¼á€…á€ºá€”á€±á€•á€«á€á€šá€ºá‹")


async def download_and_reupload(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    media_type: str,
    media,
    caption: str,
) -> str:
    """Download file from source and re-upload it fresh, so the bot owns a copy.

    Returns the new (bot-owned) Telegram file_id.
    """
    bot = context.bot

    with tempfile.TemporaryDirectory() as tmp_dir:
        file = await bot.get_file(media.file_id)
        suffix_map = {
            "video": ".mp4",
            "document": Path(file.file_path or "").suffix or ".bin",
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

        clean_name = clean_filename(getattr(media, "file_name", None) or media_type)

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


async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    msg = update.message
    caption = remove_links_from_text(msg.caption or msg.text or "")
    caption = re.sub(r"@\w+", "", caption)
    caption = remove_links_from_text(caption)

    try:
        media_info = None
        if msg.video:
            new_file_id = await download_and_reupload(
                update, context, "video", msg.video, caption
            )
            media_info = ("video", new_file_id, msg.video.file_size, msg.video.file_name)
        elif msg.document:
            new_file_id = await download_and_reupload(
                update, context, "document", msg.document, caption
            )
            media_info = ("document", new_file_id, msg.document.file_size, msg.document.file_name)
        elif msg.photo:
            new_file_id = await download_and_reupload(
                update, context, "photo", msg.photo[-1], caption
            )
            media_info = ("photo", new_file_id, None, None)

        if media_info:
            media_type, new_file_id, file_size, filename = media_info
            short_id = store_file(
                new_file_id,
                media_type,
                caption=caption,
                filename=filename,
                file_size=file_size,
            )
            deeplink = build_deeplink(context.bot.username, short_id)
            await msg.reply_text("âœ… Movie post á€á€„á€ºá€•á€¼á€®á€¸á€•á€«á€•á€¼á€®!")
            await msg.reply_text(f"ðŸ”— Deeplink: {deeplink}")
            return

        if caption:
            short_id = store_file("", "text", caption=caption)
            deeplink = build_deeplink(context.bot.username, short_id)
            await context.bot.send_message(chat_id=CHANNEL_ID, text=caption)
            await msg.reply_text("âœ… Post á€á€„á€ºá€•á€¼á€®á€¸á€•á€«á€•á€¼á€®!")
            await msg.reply_text(f"ðŸ”— Deeplink: {deeplink}")
    except Exception as e:
        print(f"Error posting forwarded: {e}")
        await msg.reply_text("âŒ Post á€á€„á€ºá€›á€¬á€™á€¾á€¬ error á€–á€¼á€…á€ºá€”á€±á€•á€«á€á€šá€ºá‹")


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
    print(f"Error: {context.error}")


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

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VIDEO & ~filters.FORWARDED, handle_video))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.FORWARDED, handle_document))
    app.add_handler(MessageHandler(filters.FORWARDED, handle_forwarded))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))
    app.add_error_handler(on_bot_error)

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
