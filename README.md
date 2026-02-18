# Telegram Video Note Bot

Converts incoming videos to Telegram video notes ("circles").

## Requirements
- Python 3.9+
- ffmpeg available in PATH
- yt-dlp and httpx (installed via requirements)

## Setup
- Install deps: `pip install -r requirements.txt`
- Set token: edit `main.py` and put your token into `BOT_TOKEN`
- Run: `python main.py`

## TikTok photo links (external API)
This bot uses an external API for TikTok photo slideshows and audio (no cookies).
1) Create an Apify account and get an API token.
2) Put the token into `APIFY_TOKEN` in `main.py`.
