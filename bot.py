import asyncio
import glob
import os
import shutil
from threading import Thread
from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioQuality, VideoQuality, MediaStream
from pytgcalls.types.stream import StreamEnded
import yt_dlp

# ---------------- CONFIG (FROM RENDER ENV VARS) ----------------
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
STRING_SESSION = os.environ.get("STRING_SESSION", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "8283878643"))
PORT = int(os.environ.get("PORT", "10000"))

TEMP_DIR = "/tmp/sanskari_downloads"
os.makedirs(TEMP_DIR, exist_ok=True)

# ---------------- KEEP-ALIVE SERVER FOR UPTIMEROBOT ----------------
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Sanskari Music Bot is running 24/7!", 200

def run_web():
    web_app.run(host="0.0.0.0", port=PORT)

# ---------------- CLIENTS & STATE ----------------
bot = Client(
    "sanskari_music_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir="/tmp",
)

assistant = Client(
    "sanskari_assistant",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=STRING_SESSION,
    workdir="/tmp",
)

call_py = PyTgCalls(assistant)

# chat_id -> list of track dicts
queues: dict[int, list[dict]] = {}
loop_mode: dict[int, bool] = {}
paused_chats: set[int] = set()
auth_users: set[int] = {OWNER_ID}


def is_authorized(user_id: int | None) -> bool:
    return bool(user_id and (user_id == OWNER_ID or user_id in auth_users))


def cleanup_storage():
    """Mahine bhar 24/7 chalne par bhi storage na bhare — temp files turant saaf."""
    for folder in [TEMP_DIR, "/tmp"]:
        if not os.path.exists(folder):
            continue
        for item in os.listdir(folder):
            if "sanskari_music_bot" in item or "sanskari_assistant" in item:
                continue
            path = os.path.join(folder, item)
            try:
                if os.path.isfile(path) or os.path.islink(path):
                    os.remove(path)
                elif os.path.isdir(path) and item == "sanskari_downloads":
                    shutil.rmtree(path, ignore_errors=True)
                    os.makedirs(TEMP_DIR, exist_ok=True)
            except Exception:
                pass
    for pattern in ["*.mp3", "*.mp4", "*.webm", "*.m4a", "*.part", "*.ytdl"]:
        for f in glob.glob(pattern):
            try:
                os.remove(f)
            except Exception:
                pass


def extract_stream(query: str, is_video: bool) -> dict | None:
    ydl_opts = {
        "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best" if is_video else "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
    }
    search_target = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_target, download=False)
            if "entries" in info and info["entries"]:
                info = info["entries"][0]
            url = info.get("url")
            title = info.get("title", "Unknown Track")
            duration = info.get("duration", 0)
            if not url:
                return None
            return {
                "title": title,
                "url": url,
                "duration": duration,
                "is_video": is_video,
            }
    except Exception:
        return None


def control_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏸ Pause", callback_data="ctrl_pause"),
                InlineKeyboardButton("▶️ Resume", callback_data="ctrl_resume"),
                InlineKeyboardButton("⏭ Skip", callback_data="ctrl_skip"),
            ],
            [
                InlineKeyboardButton("⏹ Stop", callback_data="ctrl_stop"),
                InlineKeyboardButton("🔁 Loop", callback_data="ctrl_loop"),
                InlineKeyboardButton("🗑 Clear All", callback_data="ctrl_clear"),
            ],
        ]
    )


async def start_track(chat_id: int, track: dict):
    media = MediaStream(
        track["url"],
        audio_parameters=AudioQuality.HIGH,
        video_parameters=VideoQuality.HD_720p if track["is_video"] else None,
        video_flags=MediaStream.Flags.IGNORE if not track["is_video"] else MediaStream.Flags.AUTO_DETECT,
    )
    await call_py.play(chat_id, media)
    paused_chats.discard(chat_id)
    mode_label = "Video + Audio" if track["is_video"] else "Music Audio"
    await bot.send_message(
        chat_id,
        f"**Now Playing ({mode_label}):**\n🎵 `{track['title']}`\n\n*Controls sirf Owner aur Authorized IDs ke liye hain.*",
        reply_markup=control_keyboard(),
    )
    cleanup_storage()


async def play_next_or_stop(chat_id: int):
    cleanup_storage()
    q = queues.get(chat_id, [])
    if loop_mode.get(chat_id) and q:
        await start_track(chat_id, q[0])
        return
    if q:
        q.pop(0)
    if q:
        await start_track(chat_id, q[0])
    else:
        queues[chat_id] = []
        paused_chats.discard(chat_id)
        try:
            await call_py.leave_call(chat_id)
        except Exception:
            pass
        cleanup_storage()


@call_py.on_update()
async def on_stream_end(_, update):
    if isinstance(update, StreamEnded):
        await play_next_or_stop(update.chat_id)


async def handle_play_cmd(message, is_video: bool):
    if not message.from_user:
        return
    if len(message.command) < 2 and not message.reply_to_message:
        cmd_name = "/vdplay" if is_video else "/mplay"
        await message.reply_text(f"Usage: `{cmd_name} <song name ya YouTube link>`")
        return

    query = " ".join(message.command[1:]).strip()
    status_msg = await message.reply_text("🔍 Stream nikaala ja raha hai...")

    track = await asyncio.to_thread(extract_stream, query, is_video)
    if not track:
        await status_msg.edit_text("❌ Stream nahi mil paya. Dusra naam ya link try karein.")
        cleanup_storage()
        return

    chat_id = message.chat.id
    queues.setdefault(chat_id, [])

    if any(item["title"].lower() == track["title"].lower() for item in queues[chat_id]):
        await status_msg.edit_text("⚠️ Yeh track pehle se queue ya stream mein hai.")
        return

    queues[chat_id].append(track)
    await status_msg.delete()

    if len(queues[chat_id]) == 1:
        try:
            await start_track(chat_id, track)
        except Exception as e:
            queues[chat_id].clear()
            cleanup_storage()
            await message.reply_text(f"❌ Call join/play error: `{e}`")
    else:
        await message.reply_text(
            f"➕ **Queue mein add hua (#{len(queues[chat_id])}):** `{track['title']}`"
        )


@bot.on_message(filters.command("mplay") & filters.group)
async def mplay_cmd(_, message):
    await handle_play_cmd(message, is_video=False)


@bot.on_message(filters.command("vdplay") & filters.group)
async def vdplay_cmd(_, message):
    await handle_play_cmd(message, is_video=True)


@bot.on_message(filters.command("addauth") & filters.user(OWNER_ID))
async def addauth_cmd(_, message):
    if len(message.command) < 2 or not message.command[1].isdigit():
        await message.reply_text("Usage: `/addauth <numeric_user_id>`")
        return
    uid = int(message.command[1])
    auth_users.add(uid)
    await message.reply_text(f"✅ User ID `{uid}` ko control permission de di gayi hai.")


@bot.on_message(filters.command("delauth") & filters.user(OWNER_ID))
async def delauth_cmd(_, message):
    if len(message.command) < 2 or not message.command[1].isdigit():
        await message.reply_text("Usage: `/delauth <numeric_user_id>`")
        return
    uid = int(message.command[1])
    if uid == OWNER_ID:
        await message.reply_text("⚠️ Owner ID ko remove nahi kiya ja sakta.")
        return
    auth_users.discard(uid)
    await message.reply_text(f"🗑 User ID `{uid}` ki control permission hata di gayi hai.")


@bot.on_message(filters.command("authlist"))
async def authlist_cmd(_, message):
    if not message.from_user or not is_authorized(message.from_user.id):
        return
    ids_text = "\n".join(f"• `{uid}`" for uid in sorted(auth_users))
    await message.reply_text(f"**Authorized Control IDs:**\n{ids_text}")


@bot.on_message(filters.command("clearall") & filters.group)
async def clearall_cmd(_, message):
    if not message.from_user or not is_authorized(message.from_user.id):
        return
    chat_id = message.chat.id
    queues[chat_id] = []
    loop_mode[chat_id] = False
    paused_chats.discard(chat_id)
    try:
        await call_py.leave_call(chat_id)
    except Exception:
        pass
    cleanup_storage()
    await message.reply_text("🧹 **Poori queue clear aur stream stop kar diya gaya hai.** Storage clean hai.")


@bot.on_callback_query(filters.regex(r"^ctrl_"))
async def control_buttons(_, query: CallbackQuery):
    if not query.from_user or not is_authorized(query.from_user.id):
        await query.answer("Sirf Owner ya Authorized ID hi control kar sakta hai!", show_alert=True)
        return

    chat_id = query.message.chat.id
    action = query.data

    if action == "ctrl_pause":
        try:
            await call_py.pause(chat_id)
            paused_chats.add(chat_id)
            await query.answer("⏸ Stream paused")
        except Exception:
            await query.answer("Pause nahi ho saka", show_alert=True)

    elif action == "ctrl_resume":
        try:
            await call_py.resume(chat_id)
            paused_chats.discard(chat_id)
            await query.answer("▶️ Stream resumed")
        except Exception:
            await query.answer("Resume nahi ho saka", show_alert=True)

    elif action == "ctrl_skip":
        await query.answer("⏭ Skipping...")
        await play_next_or_stop(chat_id)

    elif action == "ctrl_stop":
        queues[chat_id] = []
        loop_mode[chat_id] = False
        try:
            await call_py.leave_call(chat_id)
        except Exception:
            pass
        cleanup_storage()
        await query.answer("⏹ Stream stopped")
        await query.message.edit_text("⏹ **Stream band kar diya gaya aur storage clean kar diya gaya.**")

    elif action == "ctrl_loop":
        current = loop_mode.get(chat_id, False)
        loop_mode[chat_id] = not current
        await query.answer(f"🔁 Loop {'ON' if not current else 'OFF'}")

    elif action == "ctrl_clear":
        queues[chat_id] = []
        loop_mode[chat_id] = False
        try:
            await call_py.leave_call(chat_id)
        except Exception:
            pass
        cleanup_storage()
        await query.answer("🗑 Queue cleared")
        await query.message.edit_text("🧹 **Queue saaf aur stream reset ho gaya.**")


async def main():
    cleanup_storage()
    Thread(target=run_web, daemon=True).start()
    await bot.start()
    await assistant.start()
    await call_py.start()
    print("Bot + Assistant + PyTgCalls started successfully.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
    