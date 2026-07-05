from telegram.ext import CommandHandler, MessageHandler, filters
from telegram.constants import ParseMode
from pyrogram import Client as PyroClient
from pyrogram.errors import (
    PhoneNumberInvalid, PhoneCodeInvalid, PhoneCodeExpired,
    SessionPasswordNeeded, PasswordHashInvalid, FloodWait,
)
from config import API_ID, API_HASH
from database.db import db
from modules.session_manager import session_manager
from modules.channel_guard import require_member
from logger import logger

# State machine per user
# state: None | "wait_phone" | "wait_code" | "wait_password"
_state:       dict[int, str]    = {}
_tmp_client:  dict[int, object] = {}
_tmp_phone:   dict[int, str]    = {}
_tmp_hash:    dict[int, str]    = {}


def _clear(uid: int):
    for d in (_state, _tmp_client, _tmp_phone, _tmp_hash):
        d.pop(uid, None)


def setup(app):

    # ── /login ──────────────────────────────────────────────────────────
    async def login(update, context):
        if not await require_member(context.bot, update):
            return

        uid  = update.effective_user.id
        user = db.get_user(uid)

        if user and user.get("banned"):
            return await update.message.reply_text("🚫 Akun kamu telah dibanned dari bot ini.")

        if user and user.get("session_string"):
            return await update.message.reply_text(
                "⚠️ Akun sudah terhubung.\n"
                "Gunakan /logout dulu sebelum login ulang."
            )

        _state[uid] = "wait_phone"
        await update.message.reply_text(
            "📱 <b>Login via OTP</b>\n\n"
            "Kirimkan nomor HP kamu (format internasional):\n"
            "Contoh: <code>+628123456789</code>\n\n"
            "<i>Ketik /cancel untuk membatalkan.</i>",
            parse_mode=ParseMode.HTML,
        )

    # ── /logout ─────────────────────────────────────────────────────────
    async def logout(update, context):
        if not await require_member(context.bot, update):
            return

        uid = update.effective_user.id
        _clear(uid)
        await session_manager.close(uid)
        db.update("UPDATE users SET session_string = NULL, phone = NULL, login_at = NULL WHERE user_id = ?", (uid,))
        await update.message.reply_text("✅ Akun berhasil diputus. Gunakan /login untuk terhubung kembali.")

    # ── /cancel ─────────────────────────────────────────────────────────
    async def cancel(update, context):
        if not await require_member(context.bot, update):
            return

        uid = update.effective_user.id
        if uid in _state:
            _clear(uid)
            await update.message.reply_text("❌ Proses login dibatalkan.")
        else:
            await update.message.reply_text("ℹ️ Tidak ada proses yang berjalan saat ini.")

    # ── Handler teks (state machine) ────────────────────────────────────
    async def text_handler(update, context):
        uid  = update.effective_user.id
        text = update.message.text.strip()
        st   = _state.get(uid)

        if not st:
            # Beri petunjuk jika input terlihat seperti nomor HP atau kode OTP
            stripped = text.replace(" ", "").replace("-", "")
            is_phone = text.startswith("+") and stripped[1:].isdigit() and len(stripped) > 6
            is_code  = stripped.isdigit() and 4 <= len(stripped) <= 8
            if is_phone or is_code:
                await update.message.reply_text(
                    "ℹ️ Tidak ada proses login yang aktif.\n"
                    "Gunakan /login untuk memulai."
                )
            return

        if not await require_member(context.bot, update):
            _clear(uid)
            return

        # ── Terima nomor HP ──────────────────────────────────────────────
        if st == "wait_phone":
            phone = text if text.startswith("+") else f"+{text}"
            tmp   = None
            try:
                tmp = PyroClient(
                    name=f"otp_{uid}",
                    api_id=API_ID,
                    api_hash=API_HASH,
                    in_memory=True,
                    device_model="BOT Downloader",
                    app_version="1.0",
                    system_version="BOT Downloader Server",
                )
                await tmp.connect()
                sent = await tmp.send_code(phone)

                _tmp_client[uid] = tmp
                _tmp_phone[uid]  = phone
                _tmp_hash[uid]   = sent.phone_code_hash
                _state[uid]      = "wait_code"

                await update.message.reply_text(
                    f"✅ Kode OTP dikirim ke <code>{phone}</code>.\n\n"
                    "Masukkan kode yang kamu terima di Telegram:\n"
                    "<i>(pisahkan dengan spasi jika perlu, contoh: 1 2 3 4 5)</i>\n\n"
                    "<i>Ketik /cancel untuk membatalkan.</i>",
                    parse_mode=ParseMode.HTML,
                )

            except PhoneNumberInvalid:
                if tmp: await tmp.disconnect()
                _clear(uid)
                await update.message.reply_text(
                    "❌ Nomor HP tidak valid.\n"
                    "Pastikan format benar, contoh: <code>+628123456789</code>",
                    parse_mode=ParseMode.HTML,
                )
            except FloodWait as e:
                if tmp: await tmp.disconnect()
                _clear(uid)
                await update.message.reply_text(f"⏳ Terlalu banyak percobaan. Coba lagi dalam {e.value} detik.")
            except Exception as e:
                if tmp: await tmp.disconnect()
                _clear(uid)
                logger.error(f"send_code error uid {uid}: {e}")
                await update.message.reply_text("❌ Gagal mengirim kode. Coba lagi dengan /login.")

        # ── Terima kode OTP ──────────────────────────────────────────────
        elif st == "wait_code":
            code = text.replace(" ", "").replace("-", "")
            tmp  = _tmp_client.get(uid)
            if not tmp:
                _clear(uid)
                return await update.message.reply_text("❌ Sesi login hilang. Mulai ulang dengan /login.")

            try:
                await tmp.sign_in(
                    phone_number=_tmp_phone[uid],
                    phone_code_hash=_tmp_hash[uid],
                    phone_code=code,
                )
                await _save_session(uid, tmp, update.message)

            except SessionPasswordNeeded:
                _state[uid] = "wait_password"
                await update.message.reply_text(
                    "🔐 Akun ini menggunakan <b>Two-Step Verification (2FA)</b>.\n\n"
                    "Masukkan password 2FA kamu:",
                    parse_mode=ParseMode.HTML,
                )
            except PhoneCodeInvalid:
                await update.message.reply_text("❌ Kode salah. Coba masukkan lagi.")
            except PhoneCodeExpired:
                if tmp: await tmp.disconnect()
                _clear(uid)
                await update.message.reply_text("❌ Kode sudah kedaluwarsa. Mulai ulang dengan /login.")
            except Exception as e:
                if tmp: await tmp.disconnect()
                _clear(uid)
                logger.error(f"sign_in error uid {uid}: {e}")
                await update.message.reply_text("❌ Gagal login. Coba lagi dengan /login.")

        # ── Terima password 2FA ──────────────────────────────────────────
        elif st == "wait_password":
            tmp = _tmp_client.get(uid)
            if not tmp:
                _clear(uid)
                return await update.message.reply_text("❌ Sesi login hilang. Mulai ulang dengan /login.")

            try:
                await tmp.check_password(text)
                await _save_session(uid, tmp, update.message)
            except PasswordHashInvalid:
                await update.message.reply_text("❌ Password salah. Coba lagi.")
            except Exception as e:
                if tmp: await tmp.disconnect()
                _clear(uid)
                logger.error(f"check_password error uid {uid}: {e}")
                await update.message.reply_text("❌ Gagal verifikasi password. Coba ulang dengan /login.")

    app.add_handler(CommandHandler("login",  login))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))


async def _save_session(uid: int, tmp_client, message):
    """Simpan session string ke DB dan bersihkan state."""
    try:
        session_str = await tmp_client.export_session_string()
        me = await tmp_client.get_me()
        await tmp_client.disconnect()

        db.update(
            "UPDATE users SET session_string = ?, phone = ?, login_at = NOW()::TEXT WHERE user_id = ?",
            (session_str, _tmp_phone.get(uid, ""), uid),
        )
        _clear(uid)

        await message.reply_text(
            f"✅ <b>Login Berhasil!</b>\n\n"
            f"👤 Nama     : {me.first_name or ''} {me.last_name or ''}\n"
            f"📱 Username : @{me.username or '-'}\n\n"
            "Sekarang kamu bisa mulai download dengan:\n"
            "<code>/get &lt;link&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        _clear(uid)
        logger.error(f"save_session error uid {uid}: {e}")
        await message.reply_text("❌ Gagal menyimpan sesi. Coba ulang dengan /login.")
