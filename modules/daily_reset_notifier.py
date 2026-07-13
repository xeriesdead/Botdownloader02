import asyncio
from datetime import date, datetime, timedelta, timezone

from database.db import db
from modules.quota_service import DEFAULT_DAILY_QUOTA
from logger import logger

# Delay antar pesan agar tidak kena rate limit Telegram (detik)
_SEND_DELAY = 0.05


def _seconds_until_midnight() -> float:
    """Hitung detik tersisa hingga tengah malam (00:00 UTC)."""
    now = datetime.now(tz=timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now).total_seconds()


async def _run_reset_and_notify(bot) -> tuple[int, int]:
    """
    Reset quota semua user non-premium yang belum di-reset hari ini,
    lalu kirim notifikasi ke masing-masing user.
    Return (jumlah_reset, jumlah_notif_terkirim).
    """
    today = str(date.today())
    users_to_reset = db.fetchall(
        "SELECT user_id FROM users "
        "WHERE premium = 0 AND banned = 0 "
        "AND (last_reset IS NULL OR last_reset != ?)",
        (today,),
    )

    if not users_to_reset:
        return 0, 0

    total   = len(users_to_reset)
    notified = 0

    logger.info(f"[daily_reset] Mereset quota {total} user...")

    for row in users_to_reset:
        uid = row["user_id"]
        db.reset_daily_quota(uid, DEFAULT_DAILY_QUOTA)
        try:
            await bot.send_message(
                chat_id=uid,
                text=(
                    "🔄 <b>Quota harianmu sudah di-reset!</b>\n\n"
                    f"📦 Quota tersedia: <b>{DEFAULT_DAILY_QUOTA}</b>\n"
                    "Siap untuk download hari ini.\n\n"
                    "Gunakan /get untuk mulai download, "
                    "atau /referral untuk tambah bonus quota gratis."
                ),
                parse_mode="HTML",
            )
            notified += 1
        except Exception:
            pass
        await asyncio.sleep(_SEND_DELAY)

    return total, notified


async def run_daily_reset_once(bot) -> tuple[int, int]:
    """
    Jalankan satu kali pass reset quota harian + notifikasi.
    Idempotent: user yang `last_reset` sudah hari ini otomatis di-skip,
    jadi aman dipanggil berkali-kali oleh scheduler eksternal (mis. setiap
    beberapa menit) di mode webhook/serverless, bukan cuma tepat tengah malam.
    """
    total, notified = await _run_reset_and_notify(bot)
    if total:
        logger.info(
            f"[daily_reset] Selesai — {total} user di-reset, "
            f"{notified} notifikasi terkirim"
        )
    return total, notified


async def run_daily_reset_loop(bot):
    """Mode polling: loop in-memory yang tidur hingga tengah malam, lalu reset & notifikasi."""
    wait = _seconds_until_midnight()
    h = int(wait // 3600)
    m = int((wait % 3600) // 60)
    logger.info(f"[daily_reset] Notifikasi reset dijadwalkan dalam {h}j {m}m (tengah malam UTC)")

    while True:
        await asyncio.sleep(_seconds_until_midnight())
        try:
            await run_daily_reset_once(bot)
        except Exception as e:
            logger.error(f"[daily_reset] Error saat reset harian: {e}")
        # Tunggu 70 detik sebelum loop berikutnya agar tidak trigger dua kali di menit yang sama
        await asyncio.sleep(70)
