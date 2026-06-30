from database.db import db
from logger import logger


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
    """Ambil aktivitas satu user. date format: 'YYYY-MM-DD'."""
    if date:
        return db.fetchall(
            "SELECT event_type, detail, created_at FROM activity_log "
            "WHERE user_id = ? AND date(created_at) = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, date, limit),
        )
    return db.fetchall(
        "SELECT event_type, detail, created_at FROM activity_log "
        "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    )


def get_recent_activity(date: str = None, limit: int = 30):
    """Ambil aktivitas terbaru dari semua user. date format: 'YYYY-MM-DD'."""
    if date:
        return db.fetchall(
            "SELECT a.user_id, u.username, a.event_type, a.detail, a.created_at "
            "FROM activity_log a LEFT JOIN users u ON a.user_id = u.user_id "
            "WHERE date(a.created_at) = ? "
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
    """Ambil user yang paling banyak download."""
    if date:
        return db.fetchall(
            "SELECT a.user_id, u.username, COUNT(*) as total "
            "FROM activity_log a LEFT JOIN users u ON a.user_id = u.user_id "
            "WHERE a.event_type = 'download' AND date(a.created_at) = ? "
            "GROUP BY a.user_id ORDER BY total DESC LIMIT ?",
            (date, limit),
        )
    return db.fetchall(
        "SELECT a.user_id, u.username, COUNT(*) as total "
        "FROM activity_log a LEFT JOIN users u ON a.user_id = u.user_id "
        "WHERE a.event_type = 'download' "
        "GROUP BY a.user_id ORDER BY total DESC LIMIT ?",
        (limit,),
    )
