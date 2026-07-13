# Deploy ke Google Cloud Run (mode webhook / serverless)

Dokumen ini menjelaskan cara deploy bot ini ke **Google Cloud Run**, target
serverless gratis yang realistis untuk stack ini (lihat catatan platform di
bawah). Entry point untuk mode ini adalah `webhook_server.py` + `Dockerfile`
(bukan `main.py`, yang tetap ada untuk mode polling/lokal).

## Kenapa Cloud Run, bukan Cloudflare Workers / Vercel Edge?

Bot ini memakai dependency native (compiled C extension) yang butuh runtime
Python asli dan koneksi TCP biasa:

- `tgcrypto` — C extension untuk enkripsi MTProto (Pyrogram/pyrofork).
- `pyrofork` — butuh koneksi TCP langsung ke server Telegram (MTProto), bukan HTTP.
- `psycopg2-binary` — driver PostgreSQL native.

Platform edge-serverless murni (Cloudflare Workers, Vercel Edge Functions)
hanya mendukung Python murni tanpa C extension dan tidak mengizinkan koneksi
TCP keluar bebas — jadi tidak akan bisa menjalankan bot ini apa pun caranya.

**Cloud Run** menjalankan container Docker biasa (bukan function), jadi semua
dependency di atas tetap berjalan tanpa perubahan, sambil tetap
scale-to-zero dan gratis pada penggunaan rendah (free tier bulanan Cloud Run
cukup besar untuk bot pribadi/skala kecil).

## 1. Kumpulkan secrets

Siapkan nilai-nilai berikut (jangan commit ke git):

| Variable | Wajib | Keterangan |
|---|---|---|
| `BOT_TOKEN` | ya | Token bot dari @BotFather |
| `API_ID`, `API_HASH` | ya | Dari https://my.telegram.org (untuk Pyrogram/pyrofork) |
| `DATABASE_URL` | ya | Connection string PostgreSQL |
| `WEBHOOK_SECRET` | ya | String acak panjang, jadi bagian URL webhook (`/webhook/<WEBHOOK_SECRET>`) |
| `TASKS_SECRET` | ya | String acak lain, header rahasia untuk endpoint `/tasks/*` |
| `ADMIN_IDS` | opsional | ID Telegram admin, pisahkan koma |
| `REQUIRED_CHANNEL` | opsional | Channel wajib join |
| `MAX_FILE_SIZE_MB`, `MAX_FILE_SIZE_MB_PREMIUM`, `QUOTA_WARN_THRESHOLD` | opsional | Sudah ada default di `config.py` |

## 2. Build & deploy image

```bash
gcloud run deploy telegram-bot \
  --source . \
  --region asia-southeast2 \
  --allow-unauthenticated \
  --port 8080 \
  --set-env-vars "BOT_TOKEN=...,API_ID=...,API_HASH=...,DATABASE_URL=...,WEBHOOK_SECRET=...,TASKS_SECRET=..."
```

`--allow-unauthenticated` diperlukan karena Telegram harus bisa memanggil
endpoint webhook tanpa header auth Google — keamanan endpoint ini justru
dijaga oleh `WEBHOOK_SECRET` di path URL.

Setelah deploy, catat URL layanan yang diberikan, misal:
`https://telegram-bot-xxxxx-as.a.run.app`

## 3. Daftarkan webhook ke Telegram

```bash
curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  -d "url=https://<CLOUD_RUN_URL>/webhook/<WEBHOOK_SECRET>"
```

Verifikasi dengan:

```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"
```

## 4. Jadwalkan Cloud Scheduler untuk 3 tugas periodik

Tiga loop latar belakang di mode polling (`cleanup`, `daily_reset`,
`premium_expiry`) diganti jadi endpoint HTTP yang harus dipicu dari luar.
Buat 3 job Cloud Scheduler (free tier: 3 job pertama gratis):

| Endpoint | Jadwal yang disarankan | Method |
|---|---|---|
| `/tasks/cleanup` | setiap 6 jam (`0 */6 * * *`) | POST |
| `/tasks/daily-reset` | setiap 15 menit (`*/15 * * * *`) — idempotent, aman dipanggil sering | POST |
| `/tasks/premium-expiry` | setiap jam (`0 * * * *`) | POST |

Setiap job wajib menyertakan header `X-Tasks-Secret: <TASKS_SECRET>`. Contoh:

```bash
gcloud scheduler jobs create http bot-cleanup \
  --schedule="0 */6 * * *" \
  --uri="https://<CLOUD_RUN_URL>/tasks/cleanup" \
  --http-method=POST \
  --headers="X-Tasks-Secret=<TASKS_SECRET>"
```

Ulangi pola yang sama untuk `bot-daily-reset` (`/tasks/daily-reset`) dan
`bot-premium-expiry` (`/tasks/premium-expiry`).

## 5. Verifikasi

- `curl https://<CLOUD_RUN_URL>/healthz` harus balas `{"status": "ok"}`.
- Kirim pesan `/start` ke bot di Telegram — harus dibalas oleh bot.
- Cek log Cloud Run untuk baris `Bot started (webhook mode): @...`.

## Catatan penting: antrian download (`queue_manager`)

`queue_manager` memproses job download lewat worker in-memory yang berjalan
selama proses/container hidup — ini berbeda dari 3 loop terjadwal di atas
dan **tidak** dipecah jadi endpoint terpisah pada task ini. Di Cloud Run,
worker ini hanya aktif selama instance sedang warm (menerima traffic). Jika
traffic sepi dan Cloud Run men-scale ke 0 di tengah proses download, job yang
sedang antre bisa hilang. Untuk keandalan penuh, pertimbangkan:

- Set `--min-instances=1` di Cloud Run (mengorbankan sebagian gratis-nya scale-to-zero), atau
- Aktifkan "CPU always allocated" agar worker tetap jalan di antara request.

Ini trade-off yang disengaja untuk task ini — konversi queue menjadi model
per-request penuh adalah perubahan arsitektur terpisah, di luar cakupan
task ini.

---

# Alternatif: Deploy ke Railway

`webhook_server.py` + `Dockerfile` yang sama juga bisa langsung dipakai di
Railway — Railway mendeteksi `Dockerfile` secara otomatis dari repo GitHub.
Perbedaan dari Cloud Run cuma di 2 hal: cara set env var, dan cara
menjadwalkan 3 tugas periodik (Railway tidak punya Cloud Scheduler bawaan).

## 1. File yang perlu ada di repo GitHub kamu

Ini semua file yang ditambahkan/diubah untuk mode webhook — pastikan semuanya
ter-commit ke repo:

- **Baru:** `webhook_server.py`, `Dockerfile`, `DEPLOY.md`
- **Diubah:** `modules/cleanup.py`, `modules/daily_reset_notifier.py`, `modules/premium_expiry.py`
- **Tidak berubah, tapi tetap wajib ada:** `main.py`, `config.py`, `logger.py`, `requirements.txt`, seluruh isi `handlers/`, `modules/`, `database/`

`main.py` (mode polling lama) sengaja dibiarkan apa adanya dan tidak dipakai
lagi di Railway — cukup biarkan ada di repo, tidak perlu dihapus.

## 2. Deploy di Railway

1. Push semua file di atas ke repo GitHub kamu.
2. Di Railway: **New Project → Deploy from GitHub repo**, pilih repo ini.
   Railway otomatis mendeteksi `Dockerfile` dan menjalankan `CMD ["python", "webhook_server.py"]`.
3. Di tab **Variables**, tambahkan semua secrets yang sama seperti di atas:
   `BOT_TOKEN`, `API_ID`, `API_HASH`, `DATABASE_URL`, `WEBHOOK_SECRET`, `TASKS_SECRET` (+ opsional lain).
4. Railway otomatis memberi domain publik (tab **Settings → Networking →
   Generate Domain**). Catat URL-nya, misal `https://xxxx.up.railway.app`.
5. Pastikan **PORT** — Railway mengisi env var `PORT` otomatis; `webhook_server.py`
   sudah membaca `PORT` dari environment, jadi tidak perlu diubah manual.

## 3. Daftarkan webhook (sama seperti Cloud Run)

```bash
curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  -d "url=https://<RAILWAY_DOMAIN>/webhook/<WEBHOOK_SECRET>"
```

## 4. Jadwalkan 3 tugas periodik tanpa Cloud Scheduler

Railway tidak punya scheduler bawaan yang gratis. Pakai layanan cron gratis
eksternal, misalnya **cron-job.org** (gratis, tanpa kartu kredit) untuk
memanggil 3 endpoint berikut secara berkala dengan header
`X-Tasks-Secret: <TASKS_SECRET>`:

| Endpoint | Jadwal disarankan |
|---|---|
| `POST https://<RAILWAY_DOMAIN>/tasks/cleanup` | setiap 6 jam |
| `POST https://<RAILWAY_DOMAIN>/tasks/daily-reset` | setiap 15 menit |
| `POST https://<RAILWAY_DOMAIN>/tasks/premium-expiry` | setiap jam |

cron-job.org mendukung custom header di form pembuatan job, jadi tinggal
isi URL, method POST, jadwal, dan header di atas.

## 5. Catatan biaya Railway

Railway tidak benar-benar "serverless" (scale-to-zero) untuk service
container biasa seperti ini — service akan tetap berjalan terus selama
project aktif, dan dikenai biaya berdasarkan pemakaian CPU/RAM/jam setelah
kredit gratis bulanan habis. Ini bukan keterbatasan dari kode bot, melainkan
model harga Railway. Jika target utamanya benar-benar $0 tanpa batas waktu,
Google Cloud Run (bagian atas dokumen ini) lebih cocok karena betul-betul
scale-to-zero saat tidak ada traffic.
