import asyncio
from datetime import date, datetime, timedelta, timezone

from database.db import db
from modules.quota_service import DAILY_CLAIM_AMOUNT, MAX_DAILY_QUOTA
from logger import logger

# Delay antar pesan agar tidak kena rate limit Telegram (detik)
_SEND_DELAY = 0.05


def _seconds_until_midnight() -> float:
    """Hitung detik tersisa hingga tengah malam (00:00 UTC)."""
    now = datetime.now(tz=timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now).total_seconds()


async def _run_claim_notify(bot) -> tuple[int, int]:
    """Kirim pengingat claim ke user free; quota tidak bertambah otomatis."""
    today = str(date.today())
    users_to_notify = db.fetchall(
        "SELECT user_id FROM users "
        "WHERE premium = 0 AND banned = 0 "
        "AND (daily_claim_date IS NULL OR daily_claim_date != ?)",
        (today,),
    )

    if not users_to_notify:
        return 0, 0

    total   = len(users_to_notify)
    notified = 0

    logger.info(f"[daily_claim] Mengirim pengingat claim ke {total} user...")

    for row in users_to_notify:
        uid = row["user_id"]
        try:
            await bot.send_message(
                chat_id=uid,
                text=(
                    "🎁 <b>Daily claim tersedia!</b>\n\n"
                    f"Claim sekarang untuk menambah <b>{DAILY_CLAIM_AMOUNT} quota</b> "
                    f"(maksimal tersimpan <b>{MAX_DAILY_QUOTA}</b>).\n"
                    "Claim yang terlewat tidak menumpuk ke hari berikutnya.\n\n"
                    "Gunakan /claim untuk mengambil quota hari ini."
                ),
                parse_mode="HTML",
            )
            notified += 1
        except Exception:
            pass
        await asyncio.sleep(_SEND_DELAY)

    return total, notified


async def run_daily_claim_once(bot) -> tuple[int, int]:
    """Kirim pengingat daily claim; aman dipanggil berulang pada tanggal yang sama."""
    total, notified = await _run_claim_notify(bot)
    if total:
        logger.info(
            f"[daily_claim] Selesai — {total} user diproses, "
            f"{notified} notifikasi terkirim"
        )
    return total, notified


async def run_daily_claim_loop(bot):
    """Mode polling: kirim pengingat claim setiap pergantian hari UTC."""
    wait = _seconds_until_midnight()
    h = int(wait // 3600)
    m = int((wait % 3600) // 60)
    logger.info(f"[daily_claim] Pengingat dijadwalkan dalam {h}j {m}m (tengah malam UTC)")

    while True:
        await asyncio.sleep(_seconds_until_midnight())
        try:
            await run_daily_claim_once(bot)
        except Exception as e:
            logger.error(f"[daily_claim] Error saat mengirim pengingat: {e}")
        # Tunggu 70 detik sebelum loop berikutnya agar tidak trigger dua kali di menit yang sama
        await asyncio.sleep(70)
