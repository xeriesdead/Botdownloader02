from database.db import db
from logger import logger

# Event type yang dianggap sebagai "download berhasil"
_DOWNLOAD_EVENTS = ("download", "social_download", "download_bulk")


def log(user_id: int, event_type: str, detail: str = ""):
    """Simpan satu baris aktivitas ke tabel activity_log."""
    try:
        db.execute(
            "INSERT INTO activity_log (user_id, event_type, detail) VALUES (?, ?, ?)",
            (user_id, event_type, detail or ""),
        )
        logger.debug(f"[activity] uid={user_id} event={event_type} detail={detail}")
    except Exception as e:
        logger.error(f"[activity] Gagal menyimpan log uid={user_id} event={event_type}: {e}")


def get_user_activity(user_id: int, date: str = None, limit: int = 20):
    """Ambil aktivitas satu user. date format: 'YYYY-MM-DD' (WIB)."""
    if date:
        return db.fetchall(
            "SELECT event_type, detail, created_at FROM activity_log "
            "WHERE user_id = ? "
            "AND date((created_at::timestamptz) AT TIME ZONE 'Asia/Jakarta') = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, date, limit),
        )
    return db.fetchall(
        "SELECT event_type, detail, created_at FROM activity_log "
        "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    )


def get_recent_activity(date: str = None, limit: int = 30):
    """Ambil aktivitas terbaru dari semua user. date format: 'YYYY-MM-DD' (WIB)."""
    if date:
        return db.fetchall(
            "SELECT a.user_id, u.username, a.event_type, a.detail, a.created_at "
            "FROM activity_log a LEFT JOIN users u ON a.user_id = u.user_id "
            "WHERE date((a.created_at::timestamptz) AT TIME ZONE 'Asia/Jakarta') = ? "
            "ORDER BY a.created_at DESC LIMIT ?",
            (date, limit),
        )
    return db.fetchall(
        "SELECT a.user_id, u.username, a.event_type, a.detail, a.created_at "
        "FROM activity_log a LEFT JOIN users u ON a.user_id = u.user_id "
        "ORDER BY a.created_at DESC LIMIT ?",
        (limit,),
    )


def get_top_downloaders(date: str = None, limit: int = 10):
    """Ambil user yang paling banyak download (semua tipe: Telegram, sosmed, bulk).
    date format: 'YYYY-MM-DD' (WIB).
    """
    placeholders = ", ".join(["%s"] * len(_DOWNLOAD_EVENTS))
    if date:
        return db.fetchall(
            f"SELECT a.user_id, u.username, COUNT(*) as total "
            f"FROM activity_log a LEFT JOIN users u ON a.user_id = u.user_id "
            f"WHERE a.event_type IN ({placeholders}) "
            f"AND date((a.created_at::timestamptz) AT TIME ZONE 'Asia/Jakarta') = %s "
            f"GROUP BY a.user_id, u.username ORDER BY total DESC LIMIT %s",
            (*_DOWNLOAD_EVENTS, date, limit),
        )
    return db.fetchall(
        f"SELECT a.user_id, u.username, COUNT(*) as total "
        f"FROM activity_log a LEFT JOIN users u ON a.user_id = u.user_id "
        f"WHERE a.event_type IN ({placeholders}) "
        f"GROUP BY a.user_id, u.username ORDER BY total DESC LIMIT %s",
        (*_DOWNLOAD_EVENTS, limit),
    )
