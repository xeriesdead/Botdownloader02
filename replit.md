# Telegram Bot

A Python Telegram bot with file forwarding, premium subscriptions, user quota management, and admin controls.

## Stack
- **Runtime:** Python 3
- **Telegram library:** `python-telegram-bot` + `pyrofork` (Pyrogram fork) + `tgcrypto`
- **Database:** PostgreSQL via `psycopg2`
- **Media:** `yt-dlp`, `gallery-dl`, `pytubefix`

## Run modes
- **Polling** (`main.py`) — simpler, suitable for Replit
- **Webhook** (`webhook_server.py`) — for Railway / Cloud Run (see `DEPLOY.md`)

## Required secrets
| Variable | Description |
|---|---|
| `BOT_TOKEN` | From @BotFather |
| `API_ID` | From https://my.telegram.org |
| `API_HASH` | From https://my.telegram.org |
| `DATABASE_URL` | PostgreSQL connection string |
| `ADMIN_IDS` | Comma-separated Telegram user IDs |

## Optional secrets
| Variable | Default | Description |
|---|---|---|
| `REQUIRED_CHANNEL` | — | Username/ID of channel users must join |
| `MAX_FILE_SIZE_MB` | 1024 | File size limit for regular users (MB) |
| `MAX_FILE_SIZE_MB_PREMIUM` | 2048 | File size limit for premium users (MB) |
| `QUOTA_WARN_THRESHOLD` | 2 | Remaining quota that triggers a warning |
| `YOUTUBE_COOKIES` | — | Netscape-format cookies for yt-dlp |
| `WEBHOOK_SECRET` | — | Webhook path secret (webhook mode only) |
| `TASKS_SECRET` | — | Header secret for scheduled task endpoints |

## How to run on Replit (polling mode)
1. Add all required secrets via Replit Secrets
2. Install dependencies: `pip install -r requirements.txt`
3. Run: `python main.py`

## Workflow: Replit → GitHub → Railway

Setiap perubahan kode dikerjakan di Replit, lalu di-push ke GitHub (`xeriesdead/Botdownloader02`). Railway otomatis mendeteksi Dockerfile dan redeploy dari branch `main`.

**Langkah per perubahan:**
1. Edit kode di Replit
2. Agent push ke GitHub (branch `main`)
3. Railway redeploy otomatis

Mode yang dipakai di Railway: **webhook** (`webhook_server.py` + `Dockerfile`).

## User preferences
