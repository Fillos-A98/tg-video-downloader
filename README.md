# Telegram Video Downloader Bot

Python Telegram bot that downloads videos from URLs supported by `yt-dlp` (including YouTube and TikTok) in the best available quality and sends them back in Telegram.

## Arch Linux setup

```bash
sudo pacman -S --needed python python-pip ffmpeg
mkdir -p ~/tg-video-downloader
cd ~/tg-video-downloader
# Copy the project files here, then:
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
cp .env.example .env
nano .env
python bot.py
```

Create a bot with `@BotFather` and paste its token into `.env` as `BOT_TOKEN=...`.

For privacy, message the running bot `/id`, then set `ALLOWED_USER_IDS=YOUR_NUMERIC_ID` in `.env`. Restart the bot. If this setting is blank, anyone who finds the bot can use it.

## Notes

- `yt-dlp` supports many sites, but no downloader can guarantee every site will work as websites change.
- The bot downloads the best available video and audio streams and merges them with FFmpeg when needed.
- The hosted Telegram Bot API has a 50 MB bot upload limit in common deployments; this project defaults to 49 MB to leave headroom. Bigger files require a self-hosted Telegram Bot API server or a different delivery method.
- Use it only for content you have permission to download. It does not bypass DRM or private-account access controls.
- Keep `.env` private; never share your bot token.
