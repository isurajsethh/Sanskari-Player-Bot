import asyncio
import os
import shutil
import threading
import traceback
from typing import Dict, List, Optional

from flask import Flask
from PIL import Image, ImageDraw
from pyrogram import Client, enums, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pytgcalls import PyTgCalls, filters as flt
from pytgcalls.types import MediaStream, StreamEnded, Update
import yt_dlp


def get_env_int(name: str, default: Optional[int] = None) -> int:
    val = os.getenv(name, "").strip()
    if not val:
        if default is not None:
            return default
        raise RuntimeError(f"Missing required environment variable: {name}")
    return int(val)


def get_env_str(name: str, required: bool = True) -> str:
    val = os.getenv(name, "").strip()
    if not val and required:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


API_ID = get_env_int("API_ID")
API_HASH = get_env_str("API_HASH")
BOT_TOKEN = get_env_str("BOT_TOKEN")
STRING_SESSION = get_env_str("STRING_SESSION")
OWNER_ID = get_env_int("OWNER_ID", 8283878643)
PORT = get_env_int("PORT", 10000)

DOWNLOAD_DIR = "/tmp/sanskari_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------------- FLASK KEEP-ALIVE SERVER ----------------
flask_app = Flask("sanskari_music_keepalive")


@flask_app.route("/")
@flask_app.route("/health")
def home():
    return "Sanskari Music Bot is alive and running!", 200


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


# ---------------- PYROGRAM & PYTGCALLS CLIENTS ----------------
bot = Client(
    name="sanskari_music_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir="/tmp/sanskari_bot_session",
)

assistant = Client(
    name="sanskari_music_assistant",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=STRING_SESSION,
    in_memory=True,
)

call_py = PyTgCalls(assistant)

queues: Dict[int, List[dict]] = {}
current_track: Dict[int, Optional[dict]] = {}
paused_chats: Dict[int, bool] = {}
loop_chats: Dict[int, bool] = {}
control_msg_ids: Dict[int, int] = {}
auth_users: set[int] = set()


def log_info(msg: str):
    print(f"[SANSKARI] {msg}", flush=True)


def is_authorized(user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    return user_id == OWNER_ID or user_id in auth_users


def format_duration(seconds: Optional[int]) -> str:
    if not seconds or seconds <= 0:
        return "Live / Unknown"
    seconds = int(seconds)
    hrs = seconds // 3600
    mins = (seconds % 3600) // 60
    secs = seconds % 60
    if hrs > 0:
        return f"{hrs}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d} min"


def build_progress_bar(elapsed: int = 75, total: int = 186) -> str:
    if total <= 0:
        total = 186
    elapsed = max(0, min(elapsed, total))
    rem = elapsed - total
    total_slots = 14
    pos = int((elapsed / total) * (total_slots - 1))
    bar = "".join("●" if i == pos else "—" for i in range(total_slots))
    e_str = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
    r_sign = "-" if rem <= 0 else "+"
    r_abs = abs(rem)
    r_str = f"{r_sign}{r_abs // 60:02d}:{r_abs % 60:02d}"
    return f"<code>{e_str} | {bar} | {r_str}</code>"


def build_control_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    is_paused = paused_chats.get(chat_id, False)
    is_loop = loop_chats.get(chat_id, False)
    pause_btn = InlineKeyboardButton(
        "▶️" if is_paused else "⏸", callback_data=f"ctrl|pause|{chat_id}"
    )
    loop_btn = InlineKeyboardButton(
        "🔂" if is_loop else "🔁", callback_data=f"ctrl|loop|{chat_id}"
    )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏮", callback_data=f"ctrl|prev|{chat_id}"),
                pause_btn,
                loop_btn,
                InlineKeyboardButton("⏭", callback_data=f"ctrl|skip|{chat_id}"),
                InlineKeyboardButton("⏹", callback_data=f"ctrl|stop|{chat_id}"),
            ]
        ]
    )


def create_card_image(title: str, duration_str: str, requester: str, idx: int) -> str:
    card_path = os.path.join(DOWNLOAD_DIR, f"card_{idx}.jpg")
    width, height = 960, 480
    img = Image.new("RGB", (width, height), (28, 32, 42))
    draw = ImageDraw.Draw(img)

    draw.rectangle([24, 24, 468, height - 24], fill=(18, 24, 28))
    draw.rectangle([492, 24, width - 24, height - 24], fill=(42, 46, 54))

    bar_x = 75
    heights = [90, 160, 230, 140, 260, 190, 120, 210, 150, 95]
    for h in heights:
        top_y = (height // 2) - (h // 2)
        draw.rounded_rectangle(
            [bar_x, top_y, bar_x + 22, top_y + h],
            radius=10,
            fill=(0, 214, 143),
        )
        bar_x += 34

    draw.text((520, 65), "SANSKARI STREAM", fill=(245, 245, 245))
    short_title = title[:28] + ("..." if len(title) > 28 else "")
    draw.text((520, 110), short_title, fill=(180, 220, 255))
    draw.text((520, 150), f"Duration: {duration_str}", fill=(195, 200, 210))
    draw.text((520, 190), f"By: {requester[:24]}", fill=(195, 200, 210))

    draw.line([(520, 265), (900, 265)], fill=(120, 125, 135), width=6)
    draw.line([(520, 265), (670, 265)], fill=(0, 214, 143), width=6)
    draw.ellipse([658, 253, 682, 277], fill=(255, 255, 255))

    for cx in [585, 710, 835]:
        draw.ellipse([cx - 28, 315, cx + 28, 371], fill=(70, 76, 88))
    draw.polygon([(578, 330), (578, 358), (598, 344)], fill=(255, 255, 255))
    draw.rectangle([701, 332, 707, 356], fill=(255, 255, 255))
    draw.rectangle([713, 332, 719, 356], fill=(255, 255, 255))
    draw.polygon([(826, 330), (826, 358), (842, 344)], fill=(255, 255, 255))
    draw.rectangle([844, 330, 849, 358], fill=(255, 255, 255))

    img.save(card_path, "JPEG", quality=90)
    return card_path


def cleanup_chat_files(chat_id: int):
    prefix = f"stream_{chat_id}_"
    try:
        for fname in os.listdir(DOWNLOAD_DIR):
            if fname.startswith(prefix):
                try:
                    os.remove(os.path.join(DOWNLOAD_DIR, fname))
                except OSError:
                    pass
    except OSError:
        pass


def extract_media_info(query: str, chat_id: int, is_video: bool) -> dict:
    search_target = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"
    out_template = os.path.join(DOWNLOAD_DIR, f"stream_{chat_id}_%(id)s.%(ext)s")

    if is_video:
        ydl_opts = {
            "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best",
            "outtmpl": out_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "merge_output_format": "mp4",
        }
    else:
        ydl_opts = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": out_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(search_target, download=True)
        if "entries" in info and info["entries"]:
            info = info["entries"][0]

        file_path = ydl.prepare_filename(info)
        if not os.path.exists(file_path):
            base, _ = os.path.splitext(file_path)
            for ext in (".mp4", ".m4a", ".webm", ".mkv", ".mp3", ".opus"):
                candidate = base + ext
                if os.path.exists(candidate):
                    file_path = candidate
                    break

        title = info.get("title") or query
        duration_sec = info.get("duration") or 0
        return {
            "title": title,
            "duration_sec": duration_sec,
            "duration_str": format_duration(duration_sec),
            "file_path": file_path,
            "is_video": is_video,
        }


async def ensure_assistant_in_group(chat_id: int) -> bool:
    try:
        me_assist = await assistant.get_me()
        await assistant.get_chat_member(chat_id, me_assist.id)
        log_info(f"Assistant {me_assist.id} already in chat {chat_id}")
        return True
    except Exception as e:
        log_info(f"Assistant check failed in {chat_id}: {e}, trying invite join...")
        try:
            invite_link = await bot.export_chat_invite_link(chat_id)
            await assistant.join_chat(invite_link)
            log_info(f"Assistant joined chat {chat_id} via invite link")
            return True
        except Exception as e2:
            log_info(f"Assistant join failed in {chat_id}: {e2}")
            return False


async def start_stream_for_chat(chat_id: int, track: dict, idx: int = 0):
    current_track[chat_id] = track
    paused_chats[chat_id] = False

    if track["is_video"]:
        media_stream = MediaStream(
            track["file_path"],
            audio_parameters=MediaStream.AudioQuality.HIGH,
            video_parameters=MediaStream.VideoQuality.HD_720p,
        )
    else:
        media_stream = MediaStream(
            track["file_path"],
            audio_parameters=MediaStream.AudioQuality.HIGH,
            video_flags=MediaStream.Flags.IGNORE,
        )

    log_info(f"Starting call_py.play in chat {chat_id}: {track['title']}")
    await call_py.play(chat_id, media_stream)

    card_path = create_card_image(
        title=track["title"],
        duration_str=track["duration_str"],
        requester=track["requester"],
        idx=idx,
    )

    caption = (
        "🎵 <b>Started streaming</b>\n\n"
        f"▶️ <b>Title:</b> {track['title']}\n"
        f"⏱ <b>Duration:</b> {track['duration_str']}\n"
        f"👤 <b>Requested by:</b> {track['requester']}\n\n"
        f"{build_progress_bar(75, track['duration_sec'] or 186)}"
    )

    old_msg_id = control_msg_ids.get(chat_id)
    if old_msg_id:
        try:
            await bot.delete_messages(chat_id, old_msg_id)
        except Exception:
            pass

    sent = await bot.send_photo(
        chat_id=chat_id,
        photo=card_path,
        caption=caption,
        parse_mode=enums.ParseMode.HTML,
        reply_markup=build_control_keyboard(chat_id),
    )
    control_msg_ids[chat_id] = sent.id
    log_info(f"Sent streaming card msg_id={sent.id} in chat {chat_id}")

    try:
        if os.path.exists(card_path):
            os.remove(card_path)
    except OSError:
        pass


async def play_next_in_queue(chat_id: int):
    if loop_chats.get(chat_id) and current_track.get(chat_id):
        await start_stream_for_chat(chat_id, current_track[chat_id], idx=99)
        return

    q = queues.get(chat_id, [])
    if q:
        next_track = q.pop(0)
        await start_stream_for_chat(chat_id, next_track, idx=len(q) + 1)
    else:
        current_track[chat_id] = None
        paused_chats[chat_id] = False
        try:
            await call_py.leave_call(chat_id)
        except Exception:
            pass
        cleanup_chat_files(chat_id)


@call_py.on_update(flt.stream_end)
async def on_stream_end(_: PyTgCalls, update: Update):
    if isinstance(update, StreamEnded):
        log_info(f"Stream ended in chat {update.chat_id}")
        await play_next_in_queue(update.chat_id)


async def handle_play_command(message: Message, is_video: bool):
    user_id = message.from_user.id if message.from_user else None
    chat_id = message.chat.id
    log_info(f"Play command received: chat={chat_id}, user={user_id}, video={is_video}, text={message.text}")

    if message.chat.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
        await message.reply_text("Yeh command sirf group voice/video chat ke liye hai.")
        return

    parts = message.text.split(maxsplit=1) if message.text else []
    if len(parts) < 2 or not parts[1].strip():
        cmd = "/vdplay" if is_video else "/mplay"
        await message.reply_text(f"Kripya gaane ka naam ya link likhein. Example:\n`{cmd} Kesariya`")
        return

    query = parts[1].strip()
    status_msg = await message.reply_text("🔍 Stream nikaala ja raha hai...")

    try:
        joined = await ensure_assistant_in_group(chat_id)
        if not joined:
            await status_msg.edit_text(
                "Assistant ko group mein join karne mein dikkat aayi. Kripya assistant account ko group mein add karein."
            )
            return

        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, extract_media_info, query, chat_id, is_video)
        log_info(f"Extracted media: {info['title']} -> {info['file_path']}")

        requester = (
            f"@{message.from_user.username}"
            if message.from_user and message.from_user.username
            else (message.from_user.first_name if message.from_user else "Member")
        )

        track = {
            "title": info["title"],
            "duration_sec": info["duration_sec"],
            "duration_str": info["duration_str"],
            "file_path": info["file_path"],
            "is_video": is_video,
            "requester": requester,
        }

        try:
            await status_msg.delete()
        except Exception:
            pass

        if current_track.get(chat_id) is None:
            await start_stream_for_chat(chat_id, track, idx=1)
        else:
            queues.setdefault(chat_id, []).append(track)
            pos = len(queues[chat_id])
            await message.reply_text(
                f"➕ <b>Queue mein add hua (#{pos}):</b> {track['title']} ({track['duration_str']})",
                parse_mode=enums.ParseMode.HTML,
            )
    except Exception as e:
        log_info(f"ERROR in handle_play_command: {e}\n{traceback.format_exc()}")
        try:
            await status_msg.edit_text(f"❌ Error: <code>{e}</code>", parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass


@bot.on_message(filters.command(["mplay", "play"]))
async def mplay_cmd(_: Client, message: Message):
    await handle_play_command(message, is_video=False)


@bot.on_message(filters.command(["vdplay", "vplay"]))
async def vdplay_cmd(_: Client, message: Message):
    await handle_play_command(message, is_video=True)


@bot.on_message(filters.command("addauth"))
async def addauth_cmd(_: Client, message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply_text("Usage: `/addauth <user_id>`")
        return
    uid = int(parts[1])
    auth_users.add(uid)
    await message.reply_text(f"User `{uid}` ko authorize kar diya gaya hai.")


@bot.on_message(filters.command("delauth"))
async def delauth_cmd(_: Client, message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply_text("Usage: `/delauth <user_id>`")
        return
    uid = int(parts[1])
    auth_users.discard(uid)
    await message.reply_text(f"User `{uid}` ko auth list se hata diya gaya hai.")


@bot.on_message(filters.command("authlist"))
async def authlist_cmd(_: Client, message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    if not auth_users:
        await message.reply_text(f"Owner ID: `{OWNER_ID}`\nKoi extra authorized user nahi hai.")
    else:
        users_txt = "\n".join(f"• `{u}`" for u in sorted(auth_users))
        await message.reply_text(f"Owner ID: `{OWNER_ID}`\n\n**Authorized Users:**\n{users_txt}")


@bot.on_message(filters.command("clearall"))
async def clearall_cmd(_: Client, message: Message):
    user_id = message.from_user.id if message.from_user else None
    if not is_authorized(user_id):
        await message.reply_text("Sirf Owner ya Authorized ID hi control kar sakta hai!")
        return

    chat_id = message.chat.id
    queues[chat_id] = []
    current_track[chat_id] = None
    paused_chats[chat_id] = False
    loop_chats[chat_id] = False
    try:
        await call_py.leave_call(chat_id)
    except Exception:
        pass
    cleanup_chat_files(chat_id)
    await message.reply_text(
        "🧹 <b>Poori queue clear aur stream stop kar diya gaya hai.</b> Storage clean hai.",
        parse_mode=enums.ParseMode.HTML,
    )


@bot.on_callback_query(filters.regex(r"^ctrl\|"))
async def control_callback(_: Client, cq: CallbackQuery):
    user_id = cq.from_user.id if cq.from_user else None
    if not is_authorized(user_id):
        await cq.answer("Sirf Owner ya Authorized ID hi control kar sakta hai!", show_alert=True)
        return

    data_parts = cq.data.split("|")
    if len(data_parts) < 3:
        await cq.answer()
        return

    action = data_parts[1]
    chat_id = int(data_parts[2])

    if action == "pause":
        if not paused_chats.get(chat_id, False):
            try:
                await call_py.pause(chat_id)
            except Exception:
                pass
            paused_chats[chat_id] = True
            await cq.answer("Stream pause kar diya gaya.")
        else:
            try:
                await call_py.resume(chat_id)
            except Exception:
                pass
            paused_chats[chat_id] = False
            await cq.answer("Stream resume kar diya gaya.")

        try:
            await cq.message.edit_reply_markup(reply_markup=build_control_keyboard(chat_id))
        except Exception:
            pass

    elif action == "loop":
        new_loop = not loop_chats.get(chat_id, False)
        loop_chats[chat_id] = new_loop
        await cq.answer("Loop ON" if new_loop else "Loop OFF")
        try:
            await cq.message.edit_reply_markup(reply_markup=build_control_keyboard(chat_id))
        except Exception:
            pass

    elif action in ("skip", "prev"):
        await cq.answer("Next track par ja rahe hain...")
        await play_next_in_queue(chat_id)

    elif action == "stop":
        queues[chat_id] = []
        current_track[chat_id] = None
        paused_chats[chat_id] = False
        loop_chats[chat_id] = False
        try:
            await call_py.leave_call(chat_id)
        except Exception:
            pass
        cleanup_chat_files(chat_id)
        await cq.answer("Stream band kar diya gaya.")
        try:
            await cq.message.delete()
        except Exception:
            pass


# Fallback logger for any group/private text message so we can see if updates reach bot
@bot.on_message(filters.text & ~filters.command(["mplay", "play", "vdplay", "vplay", "addauth", "delauth", "authlist", "clearall"]))
async def debug_text_logger(_: Client, message: Message):
    uid = message.from_user.id if message.from_user else None
    log_info(f"Incoming text update: chat={message.chat.id} user={uid} text={message.text!r}")


async def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    shutil.rmtree("/tmp/sanskari_bot_session", ignore_errors=True)
    os.makedirs("/tmp/sanskari_bot_session", exist_ok=True)

    await bot.start()
    me_bot = await bot.get_me()
    log_info(f"Bot started as @{me_bot.username} (id={me_bot.id})")

    await assistant.start()
    me_as = await assistant.get_me()
    log_info(f"Assistant started as {me_as.first_name} (id={me_as.id})")

    await call_py.start()
    log_info("Bot + Assistant + PyTgCalls started successfully.")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())