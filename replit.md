# Overview

Telegram bot (Python) yang mem-forward/download file dari channel Telegram
(termasuk link Terabox), dengan sistem quota harian, referral, dan langganan
premium. Dibangun dengan `python-telegram-bot` (sisi bot) dan
`pyrofork`/Pyrogram (sisi user-session, untuk akses channel yang bot sendiri
tidak bisa akses), disimpan di PostgreSQL.

## Cara menjalankan

Ada dua entry point, untuk dua mode berbeda:

- **`main.py`** — mode polling (long-polling ke Telegram API). Proses harus
  hidup terus-menerus. Cocok untuk development lokal atau hosting always-on.
- **`webhook_server.py`** — mode webhook (serverless-ready). Menerima update
  Telegram lewat HTTP, dan 3 loop latar belakang (`cleanup`, `daily_reset`,
  `premium_expiry`) diganti jadi endpoint `/tasks/*` yang dipicu scheduler
  eksternal. Dipakai lewat `Dockerfile` untuk deploy ke Google Cloud Run —
  lihat `DEPLOY.md` untuk panduan lengkap (secrets yang dibutuhkan, cara
  daftarkan webhook, dan cara setup Cloud Scheduler).

Kedua mode butuh secrets berikut di environment: `BOT_TOKEN`, `API_ID`,
`API_HASH`, `DATABASE_URL`. Mode webhook tambahan butuh `WEBHOOK_SECRET` dan
`TASKS_SECRET`. Lihat `config.py` untuk daftar lengkap env var opsional.

## User preferences

- Bot ini akan dideploy ke Google Cloud Run (bukan Railway/Cloudflare
  Workers), karena user ingin hosting gratis dan bot punya dependency
  native (tgcrypto, pyrofork, psycopg2-binary) yang tidak bisa jalan di
  platform edge-serverless murni seperti Cloudflare Workers.
