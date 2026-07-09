from database.db import db

REFERRAL_BONUS = 3


def process(uid: int, ref: int):
    """
    Proses referral baru. Return referrer_id (int) jika berhasil dicatat
    (referral baru & valid), atau None jika tidak ada yang terjadi
    (self-referral, user sudah punya referrer, atau referrer tidak ada).
    """
    if uid == ref:
        return None
    user = db.get_user(uid)
    if not user or user.get("referrer_id"):
        return None
    referrer = db.get_user(ref)
    if not referrer:
        return None

    # Update atomik & idempoten: hanya berhasil kalau referrer_id masih NULL
    # saat ini (mencegah double-credit dari race condition /start ganda).
    rows_affected = db.execute(
        "UPDATE users SET referrer_id = ? WHERE user_id = ? AND referrer_id IS NULL",
        (ref, uid),
    )
    if not rows_affected:
        return None

    db.execute(
        "UPDATE users SET bonus_quota = bonus_quota + ? WHERE user_id = ?",
        (REFERRAL_BONUS, ref),
    )
    return ref