import os
import re
import tempfile
from pathlib import Path
from dotenv import load_dotenv
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
ADMIN_ID = int(os.getenv("ADMIN_ID"))
CHANNEL_ID = os.getenv("CHANNEL_ID")


def remove_links_from_text(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"https?://[^\s]+", "", text)
    text = re.sub(r"www\.[^\s]+", "", text)
    text = re.sub(r"t\.me/[^\s]+", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def generate_deeplink(bot_username: str, file_id: str) -> str:
    unique_id = str(int(__import__("time").time() * 1000))[-8:]
    return f"https://t.me/{bot_username}?start={unique_id}"


def clean_filename(filename: str) -> str:
    if not filename:
        return "movie"
    return remove_links_from_text(filename) or "movie"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Bot ကို Admin သာ အသုံးပြုနိုင်ပါတယ်။")
        return

    await update.message.reply_text(
        "Welcome Admin!\n\n"
        "ဖိုင်/Video ပို့ပါ - Deeplink ထုတ်ပေးမယ်\n"
        "Forward ပြီး ပို့ပါ - Channel မှာ post တင်ပေးမယ်"
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    video = update.message.video
    caption = update.message.caption or video.file_name or ""
    caption = remove_links_from_text(caption)

    bot_username = context.bot.username
    deeplink = generate_deeplink(bot_username, video.file_id)

    final_caption = f"{caption}\n\n🔗 {deeplink}" if caption else f"🔗 {deeplink}"

    try:
        await update.message.reply_video(
            video=video.file_id,
            caption=final_caption,
        )
    except Exception as e:
        print(f"Error sending video: {e}")
        await update.message.reply_text("Error ဖြစ်နေပါတယ်။")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    doc = update.message.document
    caption = update.message.caption or doc.file_name or ""
    caption = remove_links_from_text(caption)

    bot_username = context.bot.username
    deeplink = generate_deeplink(bot_username, doc.file_id)

    final_caption = f"{caption}\n\n🔗 {deeplink}" if caption else f"🔗 {deeplink}"

    try:
        await update.message.reply_document(
            document=doc.file_id,
            caption=final_caption,
        )
    except Exception as e:
        print(f"Error sending document: {e}")
        await update.message.reply_text("Error ဖြစ်နေပါတယ်။")


async def download_and_reupload(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    media_type: str,
    media,
    caption: str,
) -> None:
    """Download file from source and re-upload it fresh, so the bot owns a copy.

    ဒီနည်းနဲ့ source channel ပျက်သွားရင်လည်း bot ရဲ့ post မပျက်တော့ဘူး။
    """
    bot = context.bot
    msg = update.message

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

        try:
            if media_type == "video":
                await bot.send_video(
                    chat_id=CHANNEL_ID,
                    video=InputFile(local_path, filename=clean_name),
                    caption=caption or None,
                    supports_streaming=True,
                    thumbnail=InputFile(thumb_path) if thumb_path else None,
                )
            elif media_type == "document":
                await bot.send_document(
                    chat_id=CHANNEL_ID,
                    document=InputFile(local_path, filename=clean_name),
                    caption=caption or None,
                )
            elif media_type == "photo":
                await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=InputFile(local_path),
                    caption=caption or None,
                )

            deeplink = generate_deeplink(bot.username, media.file_id)
            await msg.reply_text("✅ Movie post တင်ပြီးပါပြီ!")
            await msg.reply_text(f"🔗 Deeplink: {deeplink}")
        except Exception as e:
            print(f"Error uploading fresh copy: {e}")
            await msg.reply_text("❌ Post တင်ရာမှာ error ဖြစ်နေပါတယ်。")


async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    msg = update.message
    caption = remove_links_from_text(msg.caption or msg.text or "")
    caption = re.sub(r"@\w+", "", caption)
    caption = remove_links_from_text(caption)

    try:
        if msg.video:
            await download_and_reupload(
                update, context, "video", msg.video, caption
            )
        elif msg.document:
            await download_and_reupload(
                update, context, "document", msg.document, caption
            )
        elif msg.photo:
            await download_and_reupload(
                update, context, "photo", msg.photo[-1], caption
            )
        elif caption:
            deeplink = generate_deeplink(context.bot.username, "text")
            await context.bot.send_message(chat_id=CHANNEL_ID, text=caption)
            await msg.reply_text("✅ Post တင်ပြီးပါပြီ!")
            await msg.reply_text(f"🔗 Deeplink: {deeplink}")
    except Exception as e:
        print(f"Error posting forwarded: {e}")
        await msg.reply_text("❌ Post တင်ရာမှာ error ဖြစ်နေပါတယ်။")


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post:
        return

    text = post.text or post.caption or ""
    text = remove_links_from_text(text)

    try:
        if post.video:
            await context.bot.send_video(
                chat_id=CHANNEL_ID,
                video=post.video.file_id,
                caption=text or None,
            )
        elif post.document:
            await context.bot.send_document(
                chat_id=CHANNEL_ID,
                document=post.document.file_id,
                caption=text or None,
            )
    except Exception as e:
        print(f"Error posting to channel: {e}")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VIDEO & ~filters.FORWARDED, handle_video))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.FORWARDED, handle_document))
    app.add_handler(MessageHandler(filters.FORWARDED, handle_forwarded))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
