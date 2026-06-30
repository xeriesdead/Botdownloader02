from database.db import db

REFERRAL_BONUS = 3


def process(uid: int, ref: int):
    if uid == ref:
        return
    user = db.get_user(uid)
    if not user or user.get("referrer_id"):
        return
    db.update("UPDATE users SET referrer_id = ? WHERE user_id = ?", (ref, uid))
    db.execute(
        "UPDATE users SET bonus_quota = bonus_quota + ? WHERE user_id = ?",
        (REFERRAL_BONUS, ref),
    )
    