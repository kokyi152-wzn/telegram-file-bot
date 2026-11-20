"""Post-link listener for the user account.

When the owner sends a channel post link to this bot's DM, every post from
that link up to the newest post of the source channel is forwarded back into
the same DM.  The bot then processes those forwarded posts as usual.

Runs in its own thread with its own asyncio loop so it never interferes with
python-telegram-bot's polling loop.
"""
import asyncio
import os
import re
import threading

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto, MessageService

API_ID = int(os.getenv("TG_API_ID") or "31778137")
API_HASH = os.getenv("TG_API_HASH") or "357247ed45b28459c6600e31cce5b773"
SESSION = (os.getenv("TG_SESSION") or "").strip()
BOT_USERNAME = os.getenv("FORWARD_BOT", "@GussesFootballprowzn_bot").strip()
DELAY = float(os.getenv("FORWARD_DELAY", "1.5") or "1.5")

_PRIVATE = re.compile(r"t\.me/c/(\d+)/(\d+)")
_PUBLIC = re.compile(r"t\.me/([A-Za-z0-9_]+)/(\d+)")


def parse_link(text):
    """Return (source_ref, start_id) from a channel post link, else None."""
    if not text:
        return None
    m = _PRIVATE.search(text)
    if m:
        return "-100" + m.group(1), int(m.group(2))
    m = _PUBLIC.search(text)
    if m and m.group(1).lower() not in ("s", "c", "share", "joinchat"):
        return "@" + m.group(1), int(m.group(2))
    return None


class _Forwarder:
    def __init__(self, client, bot):
        self.client = client
        self.bot = bot
        self.queue = asyncio.Queue()
        self.busy = False

    async def submit(self, text):
        parsed = parse_link(text)
        if not parsed:
            print("[link-forwarder] no channel post link found, ignoring", flush=True)
            return
        await self.queue.put(parsed)
        print(f"[link-forwarder] queued {parsed}", flush=True)
        if not self.busy:
            asyncio.create_task(self.run())

    async def run(self):
        self.busy = True
        try:
            while not self.queue.empty():
                ref, start = await self.queue.get()
                try:
                    await self.forward(ref, start)
                except Exception as exc:
                    print(f"[link-forwarder] ERROR {type(exc).__name__}: {exc}", flush=True)
        finally:
            self.busy = False

    async def forward(self, ref, start):
        client = self.client
        try:
            source = await client.get_entity(ref)
        except Exception as exc:
            print(f"[link-forwarder] cannot open {ref}: {exc}", flush=True)
            return
        title = getattr(source, "title", str(ref))
        head = await client.get_messages(source, limit=1)
        end = head[0].id if head else 0
        print(f"[link-forwarder] {title}: {start} -> {end}", flush=True)
        if start > end:
            print("[link-forwarder] start is newer than latest post, nothing to do", flush=True)
            return

        pending = []
        sent = 0

        async def flush():
            nonlocal pending, sent
            if not pending:
                return
            ids = [m.id for m in pending]
            while True:
                try:
                    await client.forward_messages(self.bot, ids, from_peer=source)
                    sent += len(ids)
                    print(f"[link-forwarder] forwarded {ids[0]}..{ids[-1]} (total {sent})", flush=True)
                    break
                except FloodWaitError as exc:
                    print(f"[link-forwarder] FloodWait {exc.seconds}s", flush=True)
                    await asyncio.sleep(exc.seconds + 3)
                except RPCError as exc:
                    print(f"[link-forwarder] FAILED {ids}: {exc}", flush=True)
                    break
            pending = []
            await asyncio.sleep(DELAY)

        async for m in client.iter_messages(source, min_id=start - 1, reverse=True):
            if isinstance(m, MessageService):
                continue
            if not (m.media or (m.message and m.message.strip())):
                continue
            # Skip single-photo posts (example: t.me/zsx12345/629).
            # Album photos (grouped_id) are still kept.
            if not getattr(m, "grouped_id", None) and isinstance(m.media, MessageMediaPhoto):
                print(f"[link-forwarder] skip single-photo post {m.id}", flush=True)
                continue
            gid = getattr(m, "grouped_id", None)
            if gid:
                if pending and getattr(pending[0], "grouped_id", None) != gid:
                    await flush()
                pending.append(m)
                if len(pending) >= 10:
                    await flush()
                continue
            await flush()
            pending = [m]
            await flush()
        await flush()
        print(f"[link-forwarder] DONE {title}: {sent} messages", flush=True)


async def _listen():
    if not SESSION:
        print("[link-forwarder] TG_SESSION not set - disabled", flush=True)
        return
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("[link-forwarder] session not authorised - disabled", flush=True)
        await client.disconnect()
        return
    me = await client.get_me()
    bot = await client.get_entity(BOT_USERNAME)
    print(f"[link-forwarder] user {me.id} -> bot {bot.id} ({BOT_USERNAME})", flush=True)
    fw = _Forwarder(client, bot)

    @client.on(events.NewMessage(chats=bot))
    async def on_message(event):
        text = event.message.message or ""
        if event.message.media and not text:
            return
        print(f"[link-forwarder] got: {text[:120]}", flush=True)
        await fw.submit(text)

    print("[link-forwarder] listening for post links ...", flush=True)
    await client.run_until_disconnected()


def start_link_forwarder():
    """Start the listener in a daemon thread (safe next to PTB polling)."""
    if not SESSION:
        print("[link-forwarder] TG_SESSION not set - disabled", flush=True)
        return
    threading.Thread(target=lambda: asyncio.run(_listen()),
                     name="link-forwarder", daemon=True).start()
