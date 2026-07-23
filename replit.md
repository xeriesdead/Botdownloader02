# Overview

Telegram bot (Python) yang mem-forward/download file dari channel Telegram
(termasuk link Terabox), dengan sistem quota harian, referral, dan langganan
premium. Dibangun dengan `python-telegram-bot` (sisi bot) dan
`pyrofork`/Pyrogram (sisi user-session, untuk akses channel yang bot sendiri
tidak bisa akses), disimpan di PostgreSQL.

## Cara menjalankan

Ada dua entry point, untuk dua mode berbeda:

- **`main.py`** — mode polling (long-polling ke Telegram API). Proses harus
  hidup terus-menerus. Ini adalah mode produksi yang dipakai di Railway.
- **`webhook_server.py`** — mode webhook (serverless-ready). Menerima update
  Telegram lewat HTTP, dan 3 loop latar belakang (`cleanup`, `daily_reset`,
  `premium_expiry`) diganti jadi endpoint `/tasks/*` yang dipicu scheduler
  eksternal. Dipakai lewat `Dockerfile` untuk deploy ke Google Cloud Run —
  lihat `DEPLOY.md` untuk panduan lengkap (secrets yang dibutuhkan, cara
  daftarkan webhook, dan cara setup Cloud Scheduler).

Mode polling membutuhkan secrets berikut di environment: `BOT_TOKEN`, `API_ID`,
`API_HASH`, `DATABASE_URL`. `WEBHOOK_SECRET` dan `TASKS_SECRET` tidak diperlukan
untuk mode polling. Lihat `config.py` untuk daftar lengkap env var opsional.

## Download media sosial

Perintah `/get` juga menerima link publik dari YouTube, TikTok, Instagram,
Facebook, X/Twitter, dan Threads. Video serta foto/carousel didukung tanpa
meminta login ke akun media sosial:

```text
/get https://www.youtube.com/watch?v=...
/get https://www.instagram.com/p/...
```

Downloader menggunakan `yt-dlp` dan `ffmpeg`. Konten private, konten yang
memerlukan login, atau link yang sudah kedaluwarsa dapat ditolak oleh platform
asal. Quota tetap dipotong satu kali per permintaan dan dikembalikan jika
download gagal atau dibatalkan.

## User preferences

- Bot ini dideploy ke Railway dalam mode polling always-on karena bot perlu
  proses yang terus berjalan dan memiliki dependency native (tgcrypto,
  pyrofork, psycopg2-binary).
