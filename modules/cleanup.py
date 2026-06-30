import asyncio
import os
import time

from logger import logger

DOWNLOADS_DIR = "downloads"
DOWNLOADS_MAX_AGE_HOURS = 2
LOG_DIR = "logs"
LOG_MAX_AGE_DAYS = 7
CLEANUP_INTERVAL_HOURS = 6


def _cleanup_downloads() -> int:
    removed = 0
    if not os.path.isdir(DOWNLOADS_DIR):
        return 0
    cutoff = time.time() - DOWNLOADS_MAX_AGE_HOURS * 3600
    for fname in os.listdir(DOWNLOADS_DIR):
        fpath = os.path.join(DOWNLOADS_DIR, fname)
        try:
            if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
                removed += 1
        except Exception as e:
            logger.warning(f"[cleanup] Gagal hapus {fpath}: {e}")
    return removed


def _cleanup_old_logs() -> int:
    removed = 0
    if not os.path.isdir(LOG_DIR):
        return 0
    cutoff = time.time() - LOG_MAX_AGE_DAYS * 86400
    for fname in os.listdir(LOG_DIR):
        fpath = os.path.join(LOG_DIR, fname)
        try:
            if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
                removed += 1
        except Exception as e:
            logger.warning(f"[cleanup] Gagal hapus log {fpath}: {e}")
    return removed


async def run_cleanup_loop():
    logger.info("[cleanup] Auto-cleanup dimulai (interval: setiap 6 jam)")
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_HOURS * 3600)
        try:
            dl = _cleanup_downloads()
            lg = _cleanup_old_logs()
            if dl or lg:
                logger.info(f"[cleanup] Selesai — downloads dihapus: {dl}, log lama dihapus: {lg}")
        except Exception as e:
            logger.error(f"[cleanup] Error saat cleanup: {e}")
