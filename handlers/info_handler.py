import time
from telegram.ext import CommandHandler
from telegram.constants import ParseMode
from telegram.error import BadRequest as TgBadRequest

from modules.session_manager import session_manager
from modules.link_parser import parse_telegram_link
from modules.channel_guard import require_member
from database.db import db
from logger import logger

_user_last: dict[int, float] = {}
RATE_LIMIT = 3.0


def _check_rate(uid: int) -> bool:
    now = time.time()
    if now - _user_last.get(uid, 0) < RATE_LIMIT:
        return False
    _user_last[uid] = now
    return True


def _check_logged_in(uid: int) -> bool:
    user = db.get_user(uid)
    return bool(user and user.get("session_string"))


def _fmt_size(size_bytes: int) -> str:
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"


def _fmt_duration(seconds) -> str:
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def setup(app):

    async def info(update, context):
        uid = update.effective_user.id

        if not await require_member(context.bot, update):
            return

        if not context.args:
            return await update.message.reply_text(
                "❌ <b>Format salah.</b>\n\n"
                "Contoh: <code>/info https://t.me/channel/123</code>\n\n"
                "💡 <i>Tampilkan metadata pesan sebelum download.</i>",
                parse_mode=ParseMode.HTML,
            )

        chat_src, msg_id = parse_telegram_link(context.args[0])
        if not chat_src:
            return await update.message.reply_text(
                "❌ <b>Link tidak valid.</b>\n\n"
                "Format yang didukung:\n"
                "• <code>https://t.me/username/123</code>\n"
                "• <code>https://t.me/c/1234567890/123</code>",
                parse_mode=ParseMode.HTML,
            )

        if not _check_logged_in(uid):
            return await update.message.reply_text(
                "❌ Kamu belum login.\n"
                "Gunakan /login untuk menghubungkan akun Telegram."
            )

        if not _check_rate(uid):
            return await update.message.reply_text("⏳ Terlalu cepat, tunggu sebentar.")

        wait_msg = await update.message.reply_text("🔍 Mengambil info pesan...")

        async def _edit(text: str):
            try:
                await wait_msg.edit_text(text, parse_mode=ParseMode.HTML,
                                         disable_web_page_preview=True)
            except TgBadRequest:
                pass

        try:
            uc = await session_manager.get(uid)
            if not uc:
                return await _edit("❌ Session tidak valid. Silakan /login ulang.")

            msg = await uc.get_messages(chat_src, msg_id)

            if not msg or msg.empty:
                return await _edit("❌ Pesan tidak ditemukan atau sudah dihapus.")

            lines: list[str] = []

            # ── Sumber ───────────────────────────────────────────────────
            chat_obj = getattr(msg, "chat", None)
            if chat_obj:
                raw_name  = getattr(chat_obj, "title", None) \
                            or getattr(chat_obj, "username", None) \
                            or str(getattr(chat_obj, "id", "?"))
                chat_name = _esc(raw_name)
                username  = getattr(chat_obj, "username", None)
                if username:
                    lines.append(
                        f"📢 <b>Sumber:</b> "
                        f"<a href='https://t.me/{username}'>{chat_name}</a>"
                    )
                else:
                    lines.append(f"📢 <b>Sumber:</b> {chat_name}")

            lines.append(f"🔢 <b>ID Pesan:</b> <code>{msg_id}</code>")

            msg_date = getattr(msg, "date", None)
            if msg_date:
                lines.append(
                    f"📅 <b>Tanggal:</b> {msg_date.strftime('%d %b %Y %H:%M')} UTC"
                )

            # ── Tipe & metadata media ─────────────────────────────────────
            photo      = getattr(msg, "photo",      None)
            video      = getattr(msg, "video",      None)
            audio      = getattr(msg, "audio",      None)
            document   = getattr(msg, "document",   None)
            voice      = getattr(msg, "voice",      None)
            video_note = getattr(msg, "video_note", None)
            animation  = getattr(msg, "animation",  None)
            sticker    = getattr(msg, "sticker",    None)
            text_msg   = getattr(msg, "text",       None)

            file_size_val = None

            if photo:
                lines.append("🖼 <b>Tipe:</b> Foto")
                w = getattr(photo, "width",     None)
                h = getattr(photo, "height",    None)
                s = getattr(photo, "file_size", None)
                if w and h:
                    lines.append(f"📐 <b>Resolusi:</b> {w} × {h} px")
                if s:
                    lines.append(f"💾 <b>Ukuran:</b> {_fmt_size(s)}")
                    file_size_val = s

            elif video:
                lines.append("🎬 <b>Tipe:</b> Video")
                w  = getattr(video, "width",     None)
                h  = getattr(video, "height",    None)
                d  = getattr(video, "duration",  None)
                s  = getattr(video, "file_size", None)
                fn = getattr(video, "file_name", None)
                mt = getattr(video, "mime_type", None)
                if w and h:
                    lines.append(f"📐 <b>Resolusi:</b> {w} × {h} px")
                if d:
                    lines.append(f"⏱ <b>Durasi:</b> {_fmt_duration(d)}")
                if s:
                    lines.append(f"💾 <b>Ukuran:</b> {_fmt_size(s)}")
                    file_size_val = s
                if fn:
                    lines.append(f"📄 <b>Nama file:</b> {_esc(fn)}")
                if mt:
                    lines.append(f"🔤 <b>Format:</b> {mt}")

            elif audio:
                lines.append("🎵 <b>Tipe:</b> Audio")
                d   = getattr(audio, "duration",  None)
                s   = getattr(audio, "file_size", None)
                tit = getattr(audio, "title",     None)
                per = getattr(audio, "performer", None)
                mt  = getattr(audio, "mime_type", None)
                if d:
                    lines.append(f"⏱ <b>Durasi:</b> {_fmt_duration(d)}")
                if s:
                    lines.append(f"💾 <b>Ukuran:</b> {_fmt_size(s)}")
                    file_size_val = s
                if tit:
                    lines.append(f"🎶 <b>Judul:</b> {_esc(tit)}")
                if per:
                    lines.append(f"🎤 <b>Artis:</b> {_esc(per)}")
                if mt:
                    lines.append(f"🔤 <b>Format:</b> {mt}")

            elif document:
                lines.append("📎 <b>Tipe:</b> Dokumen / File")
                fn = getattr(document, "file_name", None)
                s  = getattr(document, "file_size", None)
                mt = getattr(document, "mime_type", None)
                if fn:
                    lines.append(f"📄 <b>Nama file:</b> {_esc(fn)}")
                if s:
                    lines.append(f"💾 <b>Ukuran:</b> {_fmt_size(s)}")
                    file_size_val = s
                if mt:
                    lines.append(f"🔤 <b>MIME:</b> {mt}")

            elif voice:
                lines.append("🎙 <b>Tipe:</b> Pesan Suara")
                d = getattr(voice, "duration",  None)
                s = getattr(voice, "file_size", None)
                if d:
                    lines.append(f"⏱ <b>Durasi:</b> {_fmt_duration(d)}")
                if s:
                    lines.append(f"💾 <b>Ukuran:</b> {_fmt_size(s)}")
                    file_size_val = s

            elif video_note:
                lines.append("⭕ <b>Tipe:</b> Video Note")
                d = getattr(video_note, "duration",  None)
                s = getattr(video_note, "file_size", None)
                if d:
                    lines.append(f"⏱ <b>Durasi:</b> {_fmt_duration(d)}")
                if s:
                    lines.append(f"💾 <b>Ukuran:</b> {_fmt_size(s)}")
                    file_size_val = s

            elif animation:
                lines.append("🎞 <b>Tipe:</b> GIF / Animasi")
                w = getattr(animation, "width",     None)
                h = getattr(animation, "height",    None)
                d = getattr(animation, "duration",  None)
                s = getattr(animation, "file_size", None)
                if w and h:
                    lines.append(f"📐 <b>Resolusi:</b> {w} × {h} px")
                if d:
                    lines.append(f"⏱ <b>Durasi:</b> {_fmt_duration(d)}")
                if s:
                    lines.append(f"💾 <b>Ukuran:</b> {_fmt_size(s)}")
                    file_size_val = s

            elif sticker:
                lines.append("🎭 <b>Tipe:</b> Stiker")
                sn = getattr(sticker, "set_name",  None)
                s  = getattr(sticker, "file_size", None)
                if sn:
                    lines.append(f"📦 <b>Pack:</b> {_esc(sn)}")
                if s:
                    lines.append(f"💾 <b>Ukuran:</b> {_fmt_size(s)}")

            elif text_msg:
                lines.append("💬 <b>Tipe:</b> Teks")

            else:
                lines.append("❓ <b>Tipe:</b> Tidak diketahui")

            # ── Album ─────────────────────────────────────────────────────
            media_group_id = getattr(msg, "media_group_id", None)
            if media_group_id:
                try:
                    album_msgs = await uc.get_media_group(chat_src, msg_id)
                    lines.append(f"📸 <b>Album:</b> {len(album_msgs)} media dalam grup ini")
                except Exception:
                    lines.append("📸 <b>Album:</b> Ya (grup media)")

            # ── Proteksi forward ──────────────────────────────────────────
            protected = getattr(msg, "has_protected_content", False) or (
                chat_obj and getattr(chat_obj, "has_protected_content", False)
            )
            if protected:
                lines.append("🔒 <b>Proteksi:</b> Forward dinonaktifkan (akan di-download ulang)")

            # ── Caption / teks preview ────────────────────────────────────
            caption_text = getattr(msg, "caption", None) or (
                text_msg if not getattr(msg, "media", None) else None
            )
            if caption_text:
                preview = _esc(caption_text[:200])
                if len(caption_text) > 200:
                    preview += "…"
                lines.append(f"\n📝 <b>Caption:</b>\n{preview}")

            # ── Peringatan ukuran besar ───────────────────────────────────
            if file_size_val and file_size_val > 50 * 1024 * 1024:
                lines.append(
                    "\n⚠️ <i>File >50 MB — jika channel bersifat private, "
                    "akan dikirim ke Saved Messages secara otomatis.</i>"
                )

            await _edit("\n".join(lines))

        except Exception as e:
            logger.error(f"info error uid={uid}: {e}", exc_info=True)
            await _edit(f"❌ Terjadi kesalahan: {_esc(str(e))}")

    app.add_handler(CommandHandler("info", info))
