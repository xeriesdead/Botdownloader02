from database.db import db
from config import QUOTA_WARN_THRESHOLD
from datetime import date

DAILY_CLAIM_AMOUNT = 3
MAX_DAILY_QUOTA = 15
DEFAULT_DAILY_QUOTA = 0


class QuotaService:

    @staticmethod
    def reset_if_needed(user_id: int):
        """Kompatibilitas lama; quota kini hanya bertambah lewat /claim."""
        return

    @staticmethod
    def use_quota(user_id: int) -> bool:
        """
        Potong quota atomic.
        - Premium: selalu True (unlimited), tidak dipotong.
        - Free: bonus_quota (referral) dipakai dulu, lalu quota harian.
        """
        if db.is_premium(user_id):
            return True

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
        user = db.get_user(user_id)
        if not user:
            return {"quota": 0, "bonus": 0, "total": 0, "unlimited": False}
        q = user.get("quota") or 0
        b = user.get("bonus_quota") or 0
        today = date.today().isoformat()
        return {
            "quota": q,
            "bonus": b,
            "total": q + b,
            "unlimited": False,
            "claim_available": user.get("daily_claim_date") != today,
        }

    @staticmethod
    def claim_daily(user_id: int) -> dict:
        """Ambil bonus harian +3 sekali per tanggal, tanpa catch-up hari terlewat."""
        if db.is_premium(user_id):
            return {"claimed": False, "reason": "premium"}

        today = date.today().isoformat()
        row = db.claim_daily_quota(
            user_id,
            DAILY_CLAIM_AMOUNT,
            MAX_DAILY_QUOTA,
            today,
        )
        if row:
            quota = row.get("quota") or 0
            bonus = row.get("bonus_quota") or 0
            return {
                "claimed": True,
                "quota": quota,
                "bonus": bonus,
                "total": quota + bonus,
            }

        user = db.get_user(user_id)
        if not user:
            return {"claimed": False, "reason": "not_registered"}
        if user.get("daily_claim_date") == today:
            return {"claimed": False, "reason": "already_claimed"}
        return {"claimed": False, "reason": "unavailable"}

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
        - Free: di-cap di MAX_DAILY_QUOTA agar refund tidak menumpuk tanpa batas.
        """
        if db.is_premium(user_id):
            return
        db.execute(
            "UPDATE users SET quota = MIN(quota + ?, ?) WHERE user_id = ?",
            (amount, MAX_DAILY_QUOTA, user_id),
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
