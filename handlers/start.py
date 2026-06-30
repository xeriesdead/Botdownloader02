from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, CallbackQueryHandler
from telegram.constants import ParseMode

from database.db import db
from modules.referral_system import process as process_referral
from modules.channel_guard import is_member, _channel_display, _channel_url
from config import REQUIRED_CHANNEL
from logger import logger

BOT_COMMANDS = [
    BotCommand("start",           "Mulai bot"),
    BotCommand("login",           "Hubungkan akun Telegram via OTP"),
    BotCommand("logout",          "Putuskan akun Telegram"),
    BotCommand("get",             "Download media (single / bulk / album)"),
    BotCommand("info",            "Lihat info media sebelum download"),
    BotCommand("status",          "Lihat quota dan status akun"),
    BotCommand("referral",        "Dapatkan link referral & bonus quota"),
    BotCommand("pay",             "Info upgrade ke Premium"),
    BotCommand("help",            "Panduan penggunaan bot"),
]

WELCOME_TEXT = (
    "Bot ini membantu kamu mengunduh media dari channel &amp; grup Telegram.\n\n"
    "📌 <b>Cara Pakai:</b>\n"
    "1. /login — hubungkan akun Telegram kamu\n"
    "2. <code>/get &lt;link&gt;</code> — download 1 media atau album\n"
    "3. <code>/get &lt;link_awal&gt; &lt;link_akhir&gt;</code> — download banyak media\n\n"
    "Mendukung channel &amp; grup <b>public</b> maupun <b>private</b>.\n\n"
    "Ketik /help untuk panduan lengkap."
)


def _join_keyboard() -> InlineKeyboardMarkup:
    """Keyboard dengan tombol Join dan tombol Verifikasi."""
    ch  = _channel_display()
    url = _channel_url()
    rows = []
    if url:
        rows.append([InlineKeyboardButton(f"📢 Join {ch}", url=url)])
    rows.append([InlineKeyboardButton("✅ Saya Sudah Join", callback_data="check_join")])
    return InlineKeyboardMarkup(rows)


def setup(app):

    # ── /start ────────────────────────────────────────────────────────────
    async def start(update, context):
        try:
            user     = update.effective_user
            uid      = user.id
            username = user.username or ""
            logger.info(f"[/start] uid={uid} username={username}")

            db.create_user(uid, username)

            if context.args:
                try:
                    process_referral(uid, int(context.args[0]))
                except (ValueError, TypeError):
                    pass

            try:
                await context.bot.set_my_commands(BOT_COMMANDS)
            except Exception:
                pass

            if REQUIRED_CHANNEL and not await is_member(context.bot, uid):
                ch = _channel_display()
                await update.message.reply_text(
                    f"👋 <b>Halo, {user.first_name}!</b>\n\n"
                    "Sebelum menggunakan bot ini, kamu wajib bergabung ke channel kami.\n\n"
                    f"📢 Channel: <b>{ch}</b>\n\n"
                    "1️⃣ Tekan <b>Join</b> untuk bergabung\n"
                    "2️⃣ Tekan <b>Saya Sudah Join</b> untuk verifikasi",
                    parse_mode=ParseMode.HTML,
                    reply_markup=_join_keyboard(),
                    disable_web_page_preview=True,
                )
                return

            await update.message.reply_text(
                f"👋 <b>Halo, {user.first_name}!</b>\n\n"
                "Selamat datang di <b>Media Downloader Bot</b>.\n"
                "Bot ini membantu kamu mengunduh media dari channel &amp; grup Telegram.\n\n"
                "📌 <b>Cara Pakai:</b>\n"
                "1. /login — hubungkan akun Telegram kamu\n"
                "2. <code>/get &lt;link&gt;</code> — download 1 media atau album\n"
                "3. <code>/get &lt;link_awal&gt; &lt;link_akhir&gt;</code> — download banyak media\n\n"
                "Mendukung channel &amp; grup <b>public</b> maupun <b>private</b>.\n\n"
                "Ketik /help untuk panduan lengkap.",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error(f"[/start] ERROR uid={update.effective_user.id}: {e}", exc_info=True)
            try:
                await update.message.reply_text("❌ Terjadi kesalahan. Coba lagi.")
            except Exception:
                pass

    # ── Callback: tombol "✅ Saya Sudah Join" ────────────────────────────
    async def check_join_callback(update, context):
        query = update.callback_query
        uid   = query.from_user.id

        if REQUIRED_CHANNEL and not await is_member(context.bot, uid):
            ch = _channel_display()
            await query.answer(
                f"❌ Kamu belum join {ch}. Join dulu lalu tekan tombol ini lagi.",
                show_alert=True,
            )
            return

        await query.answer()

        welcome = (
            f"✅ <b>Verifikasi berhasil! Halo, {query.from_user.first_name}!</b>\n\n"
            + WELCOME_TEXT
        )
        try:
            await query.edit_message_text(
                welcome,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
        except Exception:
            await context.bot.send_message(
                chat_id=uid,
                text=welcome,
                parse_mode=ParseMode.HTML,
            )

        logger.info(f"[check_join] uid={uid} verified and welcomed")

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
