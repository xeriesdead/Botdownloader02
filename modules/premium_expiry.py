import asyncio
from datetime import datetime, timezone

from database.db import db
from modules.activity_log import log as activity_log
from logger import logger

_CHECK_INTERVAL_HOURS = 1
_SEND_DELAY = 0.05


def _find_expired_premium() -> list[int]:
    """Ambil semua user_id yang premium-nya sudah kadaluwarsa (premium=1 tapi premium_until sudah lewat)."""
    now = datetime.now(tz=timezone.utc).isoformat()
    rows = db.fetchall(
        "SELECT user_id FROM users "
        "WHERE premium = 1 AND premium_until IS NOT NULL AND premium_until != '' "
        "AND premium_until <= ?",
        (now,),
    )
    return [row["user_id"] for row in rows]


def _revoke_premium(user_id: int):
    """Set premium=0 untuk user yang sudah kadaluwarsa."""
    db.execute(
        "UPDATE users SET premium = 0 WHERE user_id = ?",
        (user_id,),
    )
    activity_log(user_id, "premium_expired", "kadaluwarsa otomatis")


async def _notify_expired(bot, user_id: int):
    """Kirim notifikasi ke user bahwa premium-nya sudah kadaluwarsa."""
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "💎 <b>Premium kamu telah berakhir</b>\n\n"
                "Akun kamu kini kembali ke paket <b>Free</b> dengan quota harian terbatas.\n\n"
                "Gunakan /referral untuk mendapatkan bonus quota gratis, "
                "atau hubungi admin untuk perpanjang Premium."
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass


async def run_premium_expiry_once(bot) -> int:
    """
    Jalankan satu kali pass cek & revoke premium kadaluwarsa.
    Aman dipanggil berkali-kali (idempotent per user, karena user yang sudah
    di-revoke tidak lagi match query `premium = 1`) oleh scheduler eksternal
    di mode webhook/serverless.
    """
    expired_ids = _find_expired_premium()
    if not expired_ids:
        return 0

    logger.info(f"[premium_expiry] Ditemukan {len(expired_ids)} premium kadaluwarsa, merevoking...")
    for uid in expired_ids:
        _revoke_premium(uid)
        await _notify_expired(bot, uid)
        await asyncio.sleep(_SEND_DELAY)

    logger.info(f"[premium_expiry] Selesai — {len(expired_ids)} akun premium di-revoke")
    return len(expired_ids)


async def run_premium_expiry_loop(bot):
    """Mode polling: loop in-memory yang jalan setiap jam."""
    logger.info(f"[premium_expiry] Auto-revoke premium dimulai (interval: setiap {_CHECK_INTERVAL_HOURS} jam)")
    while True:
        await asyncio.sleep(_CHECK_INTERVAL_HOURS * 3600)
        try:
            await run_premium_expiry_once(bot)
        except Exception as e:
            logger.error(f"[premium_expiry] Error: {e}")
