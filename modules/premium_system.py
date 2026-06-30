from datetime import datetime, timezone, timedelta
from database.db import db
from modules.activity_log import log as activity_log


def is_premium(uid: int) -> bool:
    user = db.get_user(uid)
    if not user:
        return False
    if user.get("premium") == 1:
        until = user.get("premium_until")
        if not until:
            return True
        try:
            return datetime.fromisoformat(until) > datetime.now(tz=timezone.utc)
        except Exception:
            return True
    return False


def set_premium(uid: int, days: int = 30):
    until = (datetime.now(tz=timezone.utc) + timedelta(days=days)).isoformat()
    db.update(
        "UPDATE users SET premium = 1, premium_until = ? WHERE user_id = ?",
        (until, uid),
    )
    activity_log(uid, "premium_granted", f"{days} hari")


def remove_premium(uid: int):
    db.update(
        "UPDATE users SET premium = 0, premium_until = NULL WHERE user_id = ?",
        (uid,),
    )
    activity_log(uid, "premium_removed", "dicabut admin")


def premium_info(uid: int) -> str:
    user = db.get_user(uid)
    if not user or not user.get("premium"):
        return "❌ Tidak aktif"
    until = user.get("premium_until")
    if not until:
        return "✅ Aktif (permanen)"
    try:
        dt = datetime.fromisoformat(until)
        if dt > datetime.now(tz=timezone.utc):
            return f"✅ Aktif hingga {dt.strftime('%d %b %Y')}"
        return "❌ Kedaluwarsa"
    except Exception:
        return "✅ Aktif"
        