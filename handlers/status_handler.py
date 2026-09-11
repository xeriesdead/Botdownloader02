from telegram.ext import CommandHandler
from telegram.constants import ParseMode

from database.db import db
from modules.quota_service import (
    QuotaService,
    DAILY_CLAIM_AMOUNT,
    MAX_DAILY_QUOTA,
)
from modules.premium_system import premium_info
from modules.session_manager import session_manager
from modules.channel_guard import require_member

_DC_NAMES = {
    1: "Miami, USA (DC1)",
    2: "Amsterdam, NL (DC2)",
    3: "Miami, USA (DC3)",
    4: "Amsterdam, NL (DC4)",
    5: "Singapore (DC5)",
}


def setup(app):
    async def status(update, context):
        if not await require_member(context.bot, update):
            return

        uid  = update.effective_user.id
        user = db.get_user(uid)

        if not user:
            return await update.message.reply_text("❌ Kamu belum terdaftar. Kirim /start dulu.")

        quota    = QuotaService.get_quota(uid)
        prem_str = premium_info(uid)

        is_unlim = quota.get("unlimited", False)
        total    = quota["total"]
        if is_unlim:
            bar = "█" * 20
        else:
            bar_fill = min(total, 20)
            bar      = "█" * bar_fill + "░" * (20 - bar_fill)

        # ── Info sesi ──────────────────────────────────────────────────────
        if user.get("session_string"):
            phone    = user.get("phone") or "-"
            login_at = user.get("login_at") or "-"

            dc_str = "-"
            try:
                uc = await session_manager.get(uid)
                if uc:
                    try:
                        dc_id = await uc.storage.dc_id()
                    except TypeError:
                        dc_id = uc.storage.dc_id
                    dc_str = _DC_NAMES.get(dc_id, f"DC{dc_id}")
            except Exception:
                pass

            session_block = (
                f"📱 Nomor HP  : <code>{phone}</code>\n"
                f"🕐 Login     : {login_at}\n"
                f"🌐 Datacenter: {dc_str}\n"
                f"🔑 Session   : ✅ Terhubung\n"
            )
        else:
            session_block = "🔑 Session   : ❌ Belum login\n"

        if is_unlim:
            quota_block = (
                f"<b>Quota</b>\n"
                f"💎 Plan    : <b>Premium — Unlimited</b>\n"
                f"<code>[{bar}]</code>\n\n"
                f"<i>Quota tidak terbatas, nikmati tanpa limit!</i>"
            )
        else:
            claim_line = (
                f"🎁 Daily claim : <b>tersedia (+{DAILY_CLAIM_AMOUNT})</b>"
                if quota.get("claim_available")
                else "✅ Daily claim : sudah diambil hari ini"
            )
            quota_block = (
                f"<b>Quota</b>\n"
                f"🎯 Harian  : <code>{quota['quota']}/{MAX_DAILY_QUOTA}</code>\n"
                f"🎁 Bonus   : <code>{quota['bonus']}</code>\n"
                f"📦 Total   : <code>{total}</code>\n"
                f"{claim_line}\n"
                f"<code>[{bar}]</code>\n\n"
                f"<i>Gunakan /claim setiap hari untuk +{DAILY_CLAIM_AMOUNT} quota.\n"
                f"Claim yang terlewat tidak menumpuk. Bonus referral tidak terbatas.</i>"
            )

        await update.message.reply_text(
            f"📊 <b>Status Akun</b>\n"
            f"{'─' * 28}\n"
            f"👤 Username  : @{user.get('username') or '-'}\n"
            f"{session_block}"
            f"💎 Premium   : {prem_str}\n\n"
            f"{quota_block}",
            parse_mode=ParseMode.HTML,
        )

    async def myquota(update, context):
        if not await require_member(context.bot, update):
            return

        uid  = update.effective_user.id
        user = db.get_user(uid)

        if not user:
            return await update.message.reply_text("❌ Kamu belum terdaftar. Kirim /start dulu.")

        quota    = QuotaService.get_quota(uid)
        prem_str = premium_info(uid)
        is_unlim = quota.get("unlimited", False)

        if is_unlim:
            bar   = "█" * 10
            total = "∞ Unlimited"
            detail = "<i>Quota tidak terbatas sebagai pengguna Premium.</i>"
        else:
            total    = quota["total"]
            bar_fill = min(total, 10)
            bar      = "█" * bar_fill + "░" * (10 - bar_fill)
            detail   = (
                f"📅 Harian  : <code>{quota['quota']}/{MAX_DAILY_QUOTA}</code>\n"
                f"🎁 Bonus   : <code>{quota['bonus']}</code>\n"
                + (
                    f"🎁 Daily claim tersedia: +{DAILY_CLAIM_AMOUNT}\n"
                    if quota.get("claim_available")
                    else "✅ Daily claim sudah diambil hari ini\n"
                )
                + "<i>Gunakan /claim setiap hari. Claim yang terlewat tidak menumpuk.</i>"
            )

        await update.message.reply_text(
            f"📦 <b>Quota Kamu</b>\n"
            f"{'─' * 24}\n"
            f"<code>[{bar}]</code>  <b>{total}</b>\n\n"
            f"{detail}\n\n"
            f"💎 Premium : {prem_str}",
            parse_mode=ParseMode.HTML,
        )

    async def claim(update, context):
        if not await require_member(context.bot, update):
            return

        uid = update.effective_user.id
        if not db.get_user(uid):
            return await update.message.reply_text(
                "❌ Kamu belum terdaftar. Kirim /start dulu."
            )

        result = QuotaService.claim_daily(uid)
        reason = result.get("reason")
        if result.get("claimed"):
            await update.message.reply_text(
                "🎁 <b>Daily claim berhasil!</b>\n\n"
                f"📦 Quota harian: <b>{result['quota']}/{MAX_DAILY_QUOTA}</b>\n"
                f"📦 Total tersedia: <b>{result['total']}</b>\n\n"
                "Kamu bisa claim lagi besok.",
                parse_mode=ParseMode.HTML,
            )
        elif reason == "already_claimed":
            await update.message.reply_text(
                "⏳ <b>Daily claim sudah diambil hari ini.</b>\n\n"
                "Claim berikutnya tersedia besok.",
                parse_mode=ParseMode.HTML,
            )
        elif reason == "premium":
            await update.message.reply_text(
                "💎 Akun Premium tidak memerlukan daily claim karena quota Unlimited."
            )
        else:
            await update.message.reply_text(
                "❌ Daily claim belum bisa diproses. Coba lagi nanti."
            )

    app.add_handler(CommandHandler("status",  status))
    app.add_handler(CommandHandler("myquota", myquota))
    app.add_handler(CommandHandler("claim", claim))
