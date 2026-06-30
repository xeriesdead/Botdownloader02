from database.db import db
from config import QUOTA_WARN_THRESHOLD

DEFAULT_DAILY_QUOTA = 5


class QuotaService:

    @staticmethod
    def reset_if_needed(user_id: int):
        """Reset quota harian jika sudah berganti hari. Premium tidak perlu reset."""
        if db.is_premium(user_id):
            return
        user = db.get_user(user_id)
        if not user:
            return
        from datetime import date
        today = str(date.today())
        if user.get("last_reset") != today:
            db.reset_daily_quota(user_id, DEFAULT_DAILY_QUOTA)

    @staticmethod
    def use_quota(user_id: int) -> bool:
        """
        Potong quota atomic.
        - Premium: selalu True (unlimited), tidak dipotong.
        - Free: bonus_quota (referral) dipakai dulu, lalu quota harian.
        """
        if db.is_premium(user_id):
            return True

        QuotaService.reset_if_needed(user_id)

        rows = db.execute(
            "UPDATE users SET bonus_quota = bonus_quota - 1 "
            "WHERE user_id = ? AND bonus_quota > 0",
            (user_id,),
        )
        if rows > 0:
            return True

        rows = db.execute(
            "UPDATE users SET quota = quota - 1 "
            "WHERE user_id = ? AND quota > 0",
            (user_id,),
        )
        return rows > 0

    @staticmethod
    def get_quota(user_id: int) -> dict:
        """
        Kembalikan info quota.
        - Premium: unlimited=True, nilai quota tidak relevan.
        - Free: quota harian + bonus referral.
        """
        if db.is_premium(user_id):
            return {"quota": -1, "bonus": 0, "total": -1, "unlimited": True}
        QuotaService.reset_if_needed(user_id)
        user = db.get_user(user_id)
        if not user:
            return {"quota": 0, "bonus": 0, "total": 0, "unlimited": False}
        q = user.get("quota") or 0
        b = user.get("bonus_quota") or 0
        return {"quota": q, "bonus": b, "total": q + b, "unlimited": False}

    @staticmethod
    def add_bonus(user_id: int, amount: int):
        """Tambah bonus quota dari referral — selalu bisa stack, tidak ada batas."""
        db.execute(
            "UPDATE users SET bonus_quota = bonus_quota + ? WHERE user_id = ?",
            (amount, user_id),
        )

    @staticmethod
    def add_quota(user_id: int, amount: int):
        """
        Kembalikan/tambah quota harian.
        - Premium: tidak perlu (unlimited).
        - Free: di-cap di DEFAULT_DAILY_QUOTA agar tidak bisa stack.
        """
        if db.is_premium(user_id):
            return
        db.execute(
            "UPDATE users SET quota = MIN(quota + ?, ?) WHERE user_id = ?",
            (amount, DEFAULT_DAILY_QUOTA, user_id),
        )

    @staticmethod
    def is_premium(user_id: int) -> bool:
        return db.is_premium(user_id)

    @staticmethod
    def should_warn(user_id: int) -> bool:
        """True jika total quota tepat di ambang batas peringatan. Premium tidak pernah warn."""
        if db.is_premium(user_id):
            return False
        q = QuotaService.get_quota(user_id)
        return q["total"] == QUOTA_WARN_THRESHOLD
