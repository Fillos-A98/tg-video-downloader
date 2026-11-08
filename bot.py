import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from dotenv import load_dotenv
from telegram import InputMediaVideo, Update
from telegram.constants import ChatAction
from telegram.ext import Application, ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USERS = {
    int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads")).resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_VIDEO_BYTES = 2_000_000_000  # 2 GB
MAX_LINKS_PER_MESSAGE = 5
RATE_LIMIT_REQUESTS = 3
RATE_LIMIT_WINDOW = 30
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# In-memory anti-spam state. A restart clears the short-lived counters.
request_timestamps: dict[int, deque[float]] = defaultdict(deque)
active_users: set[int] = set()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("tg-video-downloader")


def authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and (not ALLOWED_USERS or user.id in ALLOWED_USERS))


def extract_urls(text: str) -> list[str]:
    urls = []
    seen = set()
    for match in URL_RE.findall(text or ""):
        url = match.rstrip(".,!?)]}>")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        host = parsed.hostname.lower()
        if host in {"localhost", "localhost.localdomain"} or ".local" in host:
            continue
        if url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def is_soundcloud_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return (
        host == "soundcloud.com"
        or host.endswith(".soundcloud.com")
        or host == "snd.sc"
        or host == "music.youtube.com"
    )


def download_video(url: str, folder: str) -> tuple[Path, str]:
    audio_only = is_soundcloud_url(url)
    opts = {
        "format": "bestaudio/best" if audio_only else "bestvideo*+bestaudio/best",
        "outtmpl": str(Path(folder) / "%(title).150B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "merge_output_format": "mp4",
        "restrictfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 20,
        "max_filesize": MAX_VIDEO_BYTES,
    }
    if audio_only:
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "0",
        }]

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = str(info.get("title") or info.get("id") or "download")
        video_id = str(info.get("id") or "")
        if audio_only:
            candidates = list(Path(folder).glob(f"*{video_id}*.mp3"))
            if candidates:
                filename = max(candidates, key=lambda p: p.stat().st_size)
            else:
                filename = Path(ydl.prepare_filename(info))
        else:
            filename = Path(ydl.prepare_filename(info))

        if not filename.exists():
            candidates = [
                p for p in Path(folder).glob(f"*{video_id}*")
                if p.is_file() and not p.name.endswith((".part", ".ytdl"))
            ]
            if not candidates:
                raise FileNotFoundError("yt-dlp completed but the output file was not found")
            filename = max(candidates, key=lambda p: p.stat().st_size)

        if filename.stat().st_size > MAX_VIDEO_BYTES:
            filename.unlink(missing_ok=True)
            raise ValueError("The downloaded file exceeds the 2 GB limit")
        return filename, title


async def video_dimensions(file_path: Path) -> tuple[int | None, int | None]:
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0",
        str(file_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        log.warning("ffprobe failed for %s: %s", file_path.name, stderr.decode(errors="replace").strip())
        return None, None
    try:
        width, height = stdout.decode().strip().split("x", 1)
        return int(width), int(height)
    except (ValueError, TypeError):
        return None, None


async def send_video_album(message, videos: list[tuple[Path, str]], context: ContextTypes.DEFAULT_TYPE) -> None:
    # Telegram accepts 2–10 items per media group. Keep this chunking for future larger batches.
    for offset in range(0, len(videos), 10):
        batch = videos[offset:offset + 10]
        if len(batch) == 1:
            path, title = batch[0]
            with path.open("rb") as video:
                width, height = await video_dimensions(path)
                await message.reply_video(
                    video=video,
                    width=width,
                    height=height,
                    caption=title[:900],
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                    pool_timeout=30,
                )
            continue

        handles = []
        media = []
        try:
            for path, title in batch:
                handle = path.open("rb")
                handles.append(handle)
                width, height = await video_dimensions(path)
                media.append(InputMediaVideo(
                    media=handle,
                    width=width,
                    height=height,
                    caption=title[:900],
                    supports_streaming=True,
                ))
            await context.bot.send_media_group(
                chat_id=message.chat_id,
                media=media,
                reply_to_message_id=message.message_id,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=30,
            )
        finally:
            for handle in handles:
                handle.close()


def check_rate_limit(user_id: int) -> tuple[bool, int]:
    now = time.monotonic()
    timestamps = request_timestamps[user_id]
    while timestamps and now - timestamps[0] >= RATE_LIMIT_WINDOW:
        timestamps.popleft()
    if len(timestamps) >= RATE_LIMIT_REQUESTS:
        retry_after = max(1, int(RATE_LIMIT_WINDOW - (now - timestamps[0])))
        return False, retry_after
    timestamps.append(now)
    return True, 0


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return await update.effective_message.reply_text("You are not authorized to use this bot.")
    await update.effective_message.reply_text(
        "Send up to 5 video links in one message from YouTube, TikTok, or any other site supported by yt-dlp.\n\n"
        "Videos will be sent together as a Telegram album. SoundCloud links are converted to MP3.\n"
        "Maximum video size: 2 GB per file."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if authorized(update):
        await update.effective_message.reply_text(
            "Send up to 5 video URLs in one message. I’ll download them and send the videos as one album.\n"
            "Limits: 2 GB per video, 3 requests per 30 seconds, and one active download per user.\n"
            "Use /id to view your Telegram user ID."
        )


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user:
        await update.effective_message.reply_text(f"Your Telegram user ID: {update.effective_user.id}")


async def on_added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.my_chat_member
    if not change:
        return

    old_status = change.old_chat_member.status
    new_status = change.new_chat_member.status

    if old_status in {"left", "kicked"} and new_status in {"member", "administrator"}:
        await context.bot.send_message(
            chat_id=change.chat.id,
            text=(
                "Hey! Reply to a message containing video links and mention "
                "@elfilosxyzbot, and I’ll send the files here.\n\n"
                "SoundCloud is supported too — audio is converted to MP3.\n"
                "/help — for instructions."
            ),
        )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not (message.text or message.caption):
        return

    # In groups, only react to an explicit @bot mention in a reply to a link message.
    url_message = message
    if message.chat.type in {"group", "supergroup"}:
        command_text = message.text or message.caption or ""
        bot_username = context.bot.username
        if (
            not message.reply_to_message
            or not bot_username
            or f"@{bot_username}".casefold() not in command_text.casefold()
        ):
            return
        url_message = message.reply_to_message

    if not authorized(update):
        await message.reply_text("You are not authorized to use this bot.")
        return

    urls = extract_urls(url_message.text or url_message.caption or "")
    if not urls:
        await message.reply_text("I couldn’t find any valid HTTP(S) links in the message.")
        return
    if len(urls) > MAX_LINKS_PER_MESSAGE:
        await message.reply_text(f"Maximum {MAX_LINKS_PER_MESSAGE} links per message. Send fewer links.")
        return

    user_id = update.effective_user.id
    if user_id in active_users:
        await message.reply_text("You already have a download in progress. Wait until it finishes.")
        return

    allowed, retry_after = check_rate_limit(user_id)
    if not allowed:
        await message.reply_text(f"Anti-spam: too many requests. Try again in {retry_after} seconds.")
        return

    active_users.add(user_id)
    status = await message.reply_text(
        f"Received {len(urls)} link(s). Downloading…"
    )
    workdir = tempfile.mkdtemp(prefix="tgdl-", dir=DOWNLOAD_DIR)
    videos: list[tuple[Path, str]] = []
    audios: list[tuple[Path, str]] = []
    failures: list[str] = []

    try:
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

        for index, url in enumerate(urls, start=1):
            try:
                file_path, title = await asyncio.to_thread(download_video, url, workdir)
                if is_soundcloud_url(url):
                    audios.append((file_path, title))
                else:
                    videos.append((file_path, title))
                await status.edit_text(f"Downloaded {index}/{len(urls)}: {title[:100]}")
            except Exception as exc:
                log.warning("Download failed for %s: %s", url, exc, exc_info=True)
                if "2 GB limit" in str(exc):
                    failures.append(f"{index}. File exceeds 2 GB")
                else:
                    failures.append(f"{index}. Could not download this link")

        if videos:
            await status.edit_text(f"Downloaded {len(videos)} video(s). Sending album…")
            await send_video_album(message, videos, context)

        # Telegram audio albums only accept audio files; keep SoundCloud MP3s separate
        # rather than mixing them into a video album.
        for path, title in audios:
            with path.open("rb") as audio:
                await message.reply_audio(
                    audio=audio,
                    title=title[:64],
                    filename=path.name,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                    pool_timeout=30,
                )

        if failures:
            summary = "Some links could not be processed:\n" + "\n".join(failures)
            await status.edit_text(summary)
        else:
            await status.delete()
    except Exception as exc:
        log.exception("Processing failed: %s", exc)
        try:
            await status.edit_text("Could not send the downloaded files through the Telegram API.")
        except Exception:
            pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        active_users.discard(user_id)


def main() -> None:
    if not TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Copy .env.example to .env and set the token.")
    app = (
        Application.builder()
        .token(TOKEN)
        .base_url("http://127.0.0.1:8081/bot")
        .base_file_url("http://127.0.0.1:8081/file/bot")
        .local_mode(True)
        .build()
    )
    app.add_handler(ChatMemberHandler(on_added_to_group, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(MessageHandler(filters.ALL, handle_url))
    log.info("Bot is starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
