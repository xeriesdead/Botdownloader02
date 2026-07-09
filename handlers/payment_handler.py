from telegram.ext import CommandHandler
from telegram.constants import ParseMode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from modules.quota_service import DEFAULT_DAILY_QUOTA
from modules.channel_guard import require_member
from config import MAX_FILE_SIZE_MB, MAX_FILE_SIZE_MB_PREMIUM
from database.db import db


def setup(app):

    async def pay(update, context):
        if not await require_member(context.bot, update):
            return

        uid = update.effective_user.id

        text = (
            "💎 <b>Upgrade ke Premium</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

            "🆓 <b>FREE</b>\n"
            f"  ├ Quota harian  : <code>{DEFAULT_DAILY_QUOTA} per hari</code>\n"
            f"  ├ Batas ukuran  : <code>{MAX_FILE_SIZE_MB} MB per file</code>\n"
            "  └ Antrian       : Normal\n\n"

            "👑 <b>PREMIUM</b>\n"
            "  ├ Quota harian  : <code>∞ Unlimited</code>\n"
            f"  ├ Batas ukuran  : <code>{MAX_FILE_SIZE_MB_PREMIUM} MB (2 GB) per file</code>\n"
            "  └ Antrian       : Prioritas Tinggi\n\n"

            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📩 <b>Cara upgrade:</b>\n"
            "Hubungi admin dan kirimkan User ID kamu.\n\n"
            f"🆔 <b>User ID:</b> <code>{uid}</code>"
        )

        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def referral(update, context):
        if not await require_member(context.bot, update):
            return

        uid  = update.effective_user.id
        me   = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start={uid}"

        referrals = db.get_referrals(uid, limit=20)
        total_referrals = db.count_referrals(uid)

        info_text = (
            "🎁 <b>Program Referral</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Bagikan link referral Anda kepada rekan-rekan dan dapatkan bonus quota secara otomatis.\n\n"
    
            "✨ <b>Keuntungan Anda:</b>\n"
            "  • +3 bonus quota setiap kali teman bergabung\n"
            "  • Tidak ada batas jumlah referral\n"
            "  • Bonus quota masuk secara otomatis\n\n"

            f"👥 <b>Total referral Anda: {total_referrals}</b>\n"
        )

        if referrals:
            lines = []
            for i, r in enumerate(referrals, start=1):
                uname = r.get("username") or ""
                label = f"@{uname}" if uname else f"<code>{r['user_id']}</code>"
                joined = r.get("created_at")
                if hasattr(joined, "strftime"):
                    joined_str = joined.strftime("%d %b %Y")
                elif joined:
                    joined_str = str(joined)
                else:
                    joined_str = "-"
                lines.append(f"  {i}. {label} — {joined_str}")
            info_text += "\n" + "\n".join(lines) + "\n"
            if total_referrals > len(referrals):
                info_text += f"  ...dan {total_referrals - len(referrals)} lainnya\n"
            info_text += "\n"

        promo_caption = (
            "📥 <b>Bot Downloader — Unduh File Telegram dengan Mudah</b>\n\n"
            f"👇 Mulai sekarang: {link}"
        )

        await update.message.reply_text(info_text, parse_mode=ParseMode.HTML)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📥 Buka Bot", url=link),
        ]])

        banner_path = "assets/banner.png"
        try:
            with open(banner_path, "rb") as photo:
                await update.message.reply_photo(
                    photo=photo,
                    caption=promo_caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
        except Exception:
            await update.message.reply_text(
                promo_caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )

    app.add_handler(CommandHandler("pay",      pay))
    app.add_handler(CommandHandler("referral", referral))
