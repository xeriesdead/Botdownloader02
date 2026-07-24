import time
import asyncio
import os
from html import escape
from telegram.ext import CommandHandler, CallbackQueryHandler
from telegram.constants import ParseMode
from telegram.error import BadRequest as TgBadRequest, TimedOut, NetworkError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from modules.queue_manager import queue_manager
from modules.session_manager import session_manager
from modules.link_parser import parse_telegram_link, is_public_chat
from modules.social_downloader import (
    cleanup_download,
    download_public_media,
    is_social_link,
)
from modules.quota_service import QuotaService
from modules.safe_forward import SafeForward, check_channel_access
from modules.channel_guard import require_member
from modules.activity_log import log as activity_log
from database.db import db
from logger import logger
from config import QUOTA_WARN_THRESHOLD

_user_last:   dict[int, float]        = {}
_user_locks:  dict[int, asyncio.Lock] = {}
_bulk_cancel: dict[int, bool]         = {}
# Simpan data retry per user: uid → (channel, [failed_ids])
_retry_store: dict[int, tuple[str, list[int]]] = {}

RATE_LIMIT = 2.0
BULK_MAX   = 50

_HELP_TEXT = (
    "❌ <b>Format salah.</b>\n\n"
    "<b>Ambil 1 media (atau album):</b>\n"
    "<code>/get https://t.me/channel/123</code>\n\n"
    "<b>Ambil banyak pesan sekaligus:</b>\n"
    "<code>/get https://t.me/channel/10 https://t.me/channel/20</code>\n"
    "<code>/get https://t.me/c/1234567890/10 https://t.me/c/1234567890/20</code>\n\n"
    "<i>• Link album otomatis dikirim sebagai album utuh.\n"
    f"• Maksimal {BULK_MAX} pesan per request.</i>"
)

_LINK_INVALID_TEXT = (
    "❌ <b>Link tidak valid.</b>\n\n"
    "Format yang didukung:\n"
    "• <code>https://t.me/username/123</code>\n"
    "• <code>https://t.me/c/1234567890/123</code>"
)


def _get_lock(uid: int) -> asyncio.Lock:
    if uid not in _user_locks:
        _user_locks[uid] = asyncio.Lock()
    return _user_locks[uid]


def _check_rate(uid: int) -> bool:
    now = time.time()
    if now - _user_last.get(uid, 0) < RATE_LIMIT:
        return False
    _user_last[uid] = now
    return True


def _check_logged_in(uid: int) -> bool:
    user = db.get_user(uid)
    return bool(user and user.get("session_string"))


def _requires_user_login(chat) -> bool:
    return not is_public_chat(chat)


def _social_file_kind(path: str) -> str:
    return "photo" if os.path.splitext(path)[1].lower() in {
        ".jpg", ".jpeg", ".png", ".webp", ".gif",
    } else "video"


async def _quota_warn(bot, chat_id: int, uid: int):
    if QuotaService.should_warn(uid):
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ <b>Perhatian:</b> Quota kamu tinggal {QUOTA_WARN_THRESHOLD} lagi!\n"
                "Gunakan /referral untuk mendapatkan bonus quota gratis, "
                "atau /pay untuk upgrade ke Premium."
            ),
            parse_mode=ParseMode.HTML,
        )


def setup(app):

    # ── /canceldownload ───────────────────────────────────────────────────
    async def cancel_download(update, context):
        if not await require_member(context.bot, update):
            return

        uid = update.effective_user.id
        if _bulk_cancel.get(uid):
            return await update.message.reply_text("⏳ Sudah dalam proses pembatalan...")
        if uid not in _user_locks or not _user_locks[uid].locked():
            return await update.message.reply_text(
                "ℹ️ Tidak ada proses download yang sedang berjalan."
            )
        _bulk_cancel[uid] = True
        await update.message.reply_text(
            "🛑 Pembatalan dikirim. Download akan berhenti setelah pesan saat ini selesai."
        )

    async def social_get_cmd(update, context, url: str):
        uid = update.effective_user.id
        chat_id = update.effective_chat.id
        bot = context.bot

        if not QuotaService.use_quota(uid):
            return await update.message.reply_text(
                "❌ <b>Quota habis!</b>\n\n"
                "• Gunakan /referral untuk bonus quota gratis.\n"
                "• Atau /pay untuk upgrade ke Premium (quota Unlimited).",
                parse_mode=ParseMode.HTML,
            )

        is_prem = QuotaService.is_premium(uid)
        if not queue_manager.can_add(is_prem):
            QuotaService.add_quota(uid, 1)
            return await update.message.reply_text("❌ Server sedang sibuk, coba lagi nanti.")

        pmsg = await update.message.reply_text(
            "🔍 Mengambil media sosial...\n"
            "⏳ Link publik saja — tidak perlu login akun sosial."
        )

        async def edit(text: str):
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=pmsg.message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
            except TgBadRequest:
                pass

        async def social_job():
            work_dir = None
            quota_refunded = False

            def refund_quota():
                nonlocal quota_refunded
                if not quota_refunded:
                    QuotaService.add_quota(uid, 1)
                    quota_refunded = True

            try:
                async with _get_lock(uid):
                    _bulk_cancel[uid] = False
                    await edit("🔍 Menganalisis link media sosial...")
                    title, files, work_dir = await download_public_media(url, uid)

                    if _bulk_cancel.get(uid):
                        refund_quota()
                        _bulk_cancel[uid] = False
                        await edit("🛑 Download dibatalkan.")
                        return

                    max_size = 50 * 1024 * 1024
                    oversized = [path for path in files if os.path.getsize(path) > max_size]
                    if oversized:
                        refund_quota()
                        await edit(
                            "❌ File terlalu besar untuk dikirim langsung oleh bot.\n"
                            "Coba gunakan video dengan kualitas lebih rendah."
                        )
                        return

                    total_files = len(files)
                    await edit(f"📤 Mengirim <b>{total_files}</b> media ke chat...")
                    sent = 0
                    failed = 0
                    # Timeout eksplisit untuk upload file ke Telegram.
                    # Default python-telegram-bot terlalu pendek untuk video.
                    _SEND_WRITE_TIMEOUT = 300   # 5 menit untuk upload
                    _SEND_READ_TIMEOUT  = 120   # 2 menit untuk respons
                    _TRANSIENT_ERRORS   = (TgBadRequest, TimedOut, NetworkError)

                    for idx, path in enumerate(files, 1):
                        if _bulk_cancel.get(uid):
                            refund_quota()
                            _bulk_cancel[uid] = False
                            await edit(
                                f"🛑 Download dibatalkan.\n"
                                f"✅ Media terkirim: <b>{sent}</b>"
                            )
                            return

                        await edit(
                            f"📤 Mengirim media <b>{idx}/{total_files}</b>..."
                        )
                        filename = os.path.basename(path)
                        caption = f"📥 <b>{escape(title)}</b>\n<i>via Social Downloader</i>"
                        try:
                            with open(path, "rb") as media:
                                if _social_file_kind(path) == "photo":
                                    try:
                                        await bot.send_photo(
                                            chat_id=chat_id, photo=media,
                                            caption=caption, parse_mode=ParseMode.HTML,
                                            write_timeout=_SEND_WRITE_TIMEOUT,
                                            read_timeout=_SEND_READ_TIMEOUT,
                                        )
                                    except _TRANSIENT_ERRORS:
                                        media.seek(0)
                                        await bot.send_document(
                                            chat_id=chat_id, document=media,
                                            filename=filename, caption=caption,
                                            parse_mode=ParseMode.HTML,
                                            write_timeout=_SEND_WRITE_TIMEOUT,
                                            read_timeout=_SEND_READ_TIMEOUT,
                                        )
                                else:
                                    try:
                                        await bot.send_video(
                                            chat_id=chat_id, video=media,
                                            caption=caption, parse_mode=ParseMode.HTML,
                                            supports_streaming=True,
                                            write_timeout=_SEND_WRITE_TIMEOUT,
                                            read_timeout=_SEND_READ_TIMEOUT,
                                        )
                                    except _TRANSIENT_ERRORS:
                                        # send_video gagal (format/timeout) → coba dokumen
                                        media.seek(0)
                                        await bot.send_document(
                                            chat_id=chat_id, document=media,
                                            filename=filename, caption=caption,
                                            parse_mode=ParseMode.HTML,
                                            write_timeout=_SEND_WRITE_TIMEOUT,
                                            read_timeout=_SEND_READ_TIMEOUT,
                                        )
                            sent += 1
                        except Exception as exc:
                            failed += 1
                            logger.warning(
                                "[social] send failed uid=%s file=%s: %s",
                                uid, filename, exc,
                            )
                            # Update pesan agar tidak tampak stuck
                            await edit(
                                f"⚠️ Gagal mengirim item {idx}/{total_files}, melanjutkan..."
                            )

                    if not sent:
                        refund_quota()
                        await edit(
                            "❌ Semua media gagal dikirim ke chat.\n\n"
                            "Kemungkinan penyebab: format video tidak didukung Telegram "
                            "atau file terlalu besar. Quota dikembalikan."
                        )
                        return

                    activity_log(uid, "social_download", title[:180])
                    quota = QuotaService.get_quota(uid)
                    quota_display = "∞ Unlimited" if quota.get("unlimited") else str(quota["total"])
                    status = (
                        f"✅ <b>Selesai!</b>\n"
                        f"📦 Media terkirim: <b>{sent}</b>"
                        + (f"\n⚠️ Gagal dikirim: <b>{failed}</b>" if failed else "")
                        + f"\n📊 Sisa quota: <b>{quota_display}</b>"
                    )
                    await edit(status)
                    await _quota_warn(bot, chat_id, uid)
            except Exception as exc:
                logger.error("[social] download uid=%s: %s", uid, exc, exc_info=True)
                refund_quota()
                await edit(f"❌ Gagal mendownload media sosial: {exc}")
            finally:
                if work_dir:
                    cleanup_download(work_dir)

        pos = queue_manager.add_job(social_job, is_prem, uid)
        if pos == 0:
            QuotaService.add_quota(uid, 1)
            await edit("❌ Server sedang sibuk, coba lagi nanti.")
            return

        quota = QuotaService.get_quota(uid)
        quota_display = "∞ Unlimited" if quota.get("unlimited") else str(quota["total"])
        if pos > 1:
            await edit(
                f"📋 <b>Masuk antrian!</b>\n"
                f"Posisi kamu: <b>ke-{pos}</b>\n"
                f"📦 Sisa quota: <b>{quota_display}</b>"
            )
        else:
            await edit(
                f"⏳ <b>Download dimulai...</b>\n"
                f"📦 Sisa quota: <b>{quota_display}</b>"
            )

    # ── /get — fungsi tunggal untuk single & bulk ─────────────────────────
    async def get_cmd(update, context):
        uid     = update.effective_user.id
        chat_id = update.effective_chat.id
        bot     = context.bot

        if not await require_member(bot, update):
            return

        if not _check_rate(uid):
            return await update.message.reply_text("⏳ Terlalu cepat, tunggu sebentar.")

        args = context.args or []

        # ── Tidak ada argumen ─────────────────────────────────────────────
        if not args:
            return await update.message.reply_text(_HELP_TEXT, parse_mode=ParseMode.HTML)

        if len(args) == 1 and is_social_link(args[0]):
            return await social_get_cmd(update, context, args[0])

        # ── Satu link → mode single ───────────────────────────────────────
        if len(args) == 1:
            chat, msg_id = parse_telegram_link(args[0])
            if not chat:
                return await update.message.reply_text(
                    _LINK_INVALID_TEXT, parse_mode=ParseMode.HTML
                )

            if _requires_user_login(chat) and not _check_logged_in(uid):
                return await update.message.reply_text(
                    "❌ Kamu belum login.\nGunakan /login untuk menghubungkan akun Telegram."
                )

            # ── Pre-flight: cek akses channel SEBELUM potong quota ────────
            uc_check = await session_manager.get_for_chat(uid, chat)
            if uc_check:
                ok_access, err_access = await check_channel_access(uc_check, chat)
                if not ok_access:
                    return await update.message.reply_text(err_access, parse_mode=ParseMode.HTML)

            if not QuotaService.use_quota(uid):
                return await update.message.reply_text(
                    "❌ <b>Quota habis!</b>\n\n"
                    "• Gunakan /referral untuk bonus quota gratis.\n"
                    "• Atau /pay untuk upgrade ke Premium (quota Unlimited).",
                    parse_mode=ParseMode.HTML,
                )

            is_prem = QuotaService.is_premium(uid)
            lock    = _get_lock(uid)

            if not queue_manager.can_add(is_prem):
                QuotaService.add_quota(uid, 1)
                return await update.message.reply_text("❌ Server sedang sibuk, coba lagi nanti.")

            q          = QuotaService.get_quota(uid)
            quota_disp = "∞ Unlimited" if q.get("unlimited") else str(q["total"])

            # Kirim pesan sementara dulu, lalu update dengan posisi nyata setelah job masuk
            pmsg    = await update.message.reply_text("⏳ Memproses...", parse_mode=ParseMode.HTML)
            pmsg_id = pmsg.message_id

            async def _edit_s(text: str, html: bool = False):
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=pmsg_id,
                        text=text,
                        parse_mode=ParseMode.HTML if html else None,
                    )
                except TgBadRequest:
                    pass

            async def single_job():
                try:
                    await _edit_s(
                        f"⏳ Sedang mengunduh...\n📦 Sisa quota: <b>{quota_disp}</b>",
                        html=True,
                    )

                    async def _progress(text: str):
                        await _edit_s(text, html=True)

                    async with lock:
                        uc = await session_manager.get_for_chat(uid, chat)
                        if not uc:
                            QuotaService.add_quota(uid, 1)
                            message = (
                                "❌ Bot tidak bisa mengakses channel publik ini."
                                if is_public_chat(chat)
                                else "❌ Session tidak valid. Silakan /login ulang."
                            )
                            await _edit_s(message)
                            return
                        ok, reason = await SafeForward.run(
                            uc, bot, chat_id, chat, msg_id,
                            on_progress=_progress,
                            is_premium=is_prem,
                        )
                        if not ok:
                            QuotaService.add_quota(uid, 1)
                            await _edit_s(f"❌ Gagal: {reason}")
                        else:
                            activity_log(uid, "download", f"{chat}/{msg_id}")
                            q_after    = QuotaService.get_quota(uid)
                            qd         = "∞ Unlimited" if q_after.get("unlimited") else str(q_after["total"])
                            await _edit_s(
                                f"✅ Terkirim!\n📦 Sisa quota: <b>{qd}</b>",
                                html=True,
                            )
                            await _quota_warn(bot, chat_id, uid)
                except Exception as e:
                    logger.error(f"get single job error uid={uid}: {e}", exc_info=True)
                    QuotaService.add_quota(uid, 1)
                    await _edit_s(f"❌ Terjadi kesalahan tak terduga: {e}")

            pos = queue_manager.add_job(single_job, is_prem, uid)
            if pos == 0:
                # Race condition: antrian penuh setelah can_add lolos
                QuotaService.add_quota(uid, 1)
                await _edit_s("❌ Server sedang sibuk, coba lagi nanti.")
                return

            if pos > 1:
                await _edit_s(
                    f"📋 <b>Masuk antrian!</b>\n"
                    f"Posisi kamu: <b>ke-{pos}</b> dalam antrian\n"
                    f"⏳ Download akan dimulai setelah giliran tiba.\n\n"
                    f"📦 Sisa quota: <b>{quota_disp}</b>",
                    html=True,
                )
            else:
                await _edit_s(
                    f"⏳ Giliran kamu berikutnya!\n📦 Sisa quota: <b>{quota_disp}</b>",
                    html=True,
                )
            return

        # ── Dua link → mode bulk ──────────────────────────────────────────
        if len(args) == 2:
            chat_a, msg_a = parse_telegram_link(args[0])
            chat_b, msg_b = parse_telegram_link(args[1])

            if not chat_a or not chat_b:
                return await update.message.reply_text(
                    _LINK_INVALID_TEXT, parse_mode=ParseMode.HTML
                )

            if chat_a != chat_b:
                return await update.message.reply_text(
                    "❌ Kedua link harus dari channel yang sama."
                )

            if msg_a > msg_b:
                msg_a, msg_b = msg_b, msg_a

            count = msg_b - msg_a + 1
            if count > BULK_MAX:
                return await update.message.reply_text(
                    f"❌ Maksimal <b>{BULK_MAX}</b> pesan per request.\n"
                    f"Range kamu: {count} pesan.",
                    parse_mode=ParseMode.HTML,
                )

            if _requires_user_login(chat_a) and not _check_logged_in(uid):
                return await update.message.reply_text(
                    "❌ Kamu belum login.\nGunakan /login untuk menghubungkan akun Telegram."
                )

            # ── Pre-flight: cek akses channel SEBELUM potong quota ────────
            uc_check = await session_manager.get_for_chat(uid, chat_a)
            if uc_check:
                ok_access, err_access = await check_channel_access(uc_check, chat_a)
                if not ok_access:
                    return await update.message.reply_text(err_access, parse_mode=ParseMode.HTML)

            quota = QuotaService.get_quota(uid)
            if not quota.get("unlimited") and quota["total"] < count:
                return await update.message.reply_text(
                    f"❌ <b>Quota tidak cukup.</b>\n\n"
                    f"Dibutuhkan: {count}\nTersedia: {quota['total']}\n\n"
                    "Gunakan /referral untuk bonus quota, atau /pay untuk Premium.",
                    parse_mode=ParseMode.HTML,
                )

            for _ in range(count):
                QuotaService.use_quota(uid)

            is_prem = QuotaService.is_premium(uid)
            lock    = _get_lock(uid)
            _bulk_cancel[uid] = False

            if not queue_manager.can_add(is_prem):
                QuotaService.add_quota(uid, count)
                return await update.message.reply_text("❌ Server sedang sibuk, coba lagi nanti.")

            q          = QuotaService.get_quota(uid)
            quota_disp = "∞ Unlimited" if q.get("unlimited") else str(q["total"])

            pmsg    = await update.message.reply_text("⏳ Memproses...", parse_mode=ParseMode.HTML)
            pmsg_id = pmsg.message_id

            async def _edit_b(text: str):
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=pmsg_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                    )
                except TgBadRequest:
                    pass

            async def bulk_job():
                try:
                    await _edit_b(
                        f"⏳ <b>Mengunduh {count} pesan... (0/{count})</b>\n"
                        f"📦 Sisa quota: {quota_disp}\n\n"
                        "<i>Ketik /canceldownload untuk membatalkan.</i>"
                    )
                    async with lock:
                        uc = await session_manager.get_for_chat(uid, chat_a)
                        if not uc:
                            QuotaService.add_quota(uid, count)
                            message = (
                                "❌ Bot tidak bisa mengakses channel publik ini."
                                if is_public_chat(chat_a)
                                else "❌ Session tidak valid. Silakan /login ulang."
                            )
                            await _edit_b(message)
                            return

                        # Fetch semua pesan sekaligus
                        all_ids  = list(range(msg_a, msg_b + 1))
                        all_msgs = []
                        for i in range(0, len(all_ids), 200):
                            chunk   = all_ids[i : i + 200]
                            fetched = await uc.get_messages(chat_a, chunk)
                            if isinstance(fetched, list):
                                all_msgs.extend(fetched)
                            elif fetched:
                                all_msgs.append(fetched)

                        all_msgs = sorted(
                            [m for m in all_msgs if m and not m.empty],
                            key=lambda m: m.id,
                        )

                        missing = count - len(all_msgs)
                        if missing > 0:
                            QuotaService.add_quota(uid, missing)

                        total          = len(all_msgs)
                        success        = 0
                        failed         = 0
                        failed_ids: list[int] = []
                        cancelled      = False
                        seen_group_ids: set = set()
                        last_edit      = 0.0
                        bulk_start     = time.monotonic()

                        for msg in all_msgs:
                            if _bulk_cancel.get(uid):
                                cancelled = True
                                remaining = total - success - failed
                                QuotaService.add_quota(uid, remaining)
                                _bulk_cancel[uid] = False
                                break

                            if msg.media_group_id:
                                if msg.media_group_id in seen_group_ids:
                                    QuotaService.add_quota(uid, 1)
                                    continue
                                seen_group_ids.add(msg.media_group_id)
                                ok, reason = await SafeForward.run_album(
                                    uc, bot, chat_id, chat_a, msg.id
                                )
                            else:
                                ok, reason = await SafeForward.run(
                                    uc, bot, chat_id, chat_a, msg.id,
                                    is_premium=is_prem,
                                )

                            if ok:
                                success += 1
                            else:
                                failed += 1
                                failed_ids.append(msg.id)
                                logger.info(f"get bulk skip msg {msg.id}: {reason}")

                            now = time.monotonic()
                            if now - last_edit >= 3.0:
                                done = success + failed
                                await _edit_b(
                                    f"⏳ <b>Mengunduh... ({done}/{total})</b>\n"
                                    f"✅ {success}  ❌ {failed}\n\n"
                                    "<i>Ketik /canceldownload untuk membatalkan.</i>"
                                )
                                last_edit = now

                        if failed:
                            QuotaService.add_quota(uid, failed)
                        if success:
                            activity_log(uid, "download_bulk", f"{chat_a} x{success}")

                        # ── Hitung durasi ──────────────────────────────────
                        elapsed_s  = time.monotonic() - bulk_start
                        if elapsed_s < 60:
                            dur_str = f"{int(elapsed_s)} detik"
                        else:
                            dur_str = f"{elapsed_s / 60:.1f} menit"

                        # ── Susun ringkasan ────────────────────────────────
                        status_line  = "🛑 Dibatalkan" if cancelled else "✅ Selesai"
                        skipped      = total - success - failed
                        skipped_line = (
                            f"⏭ Dilewati  : {skipped} (dibatalkan)\n"
                            if cancelled and skipped > 0 else ""
                        )
                        q_fin  = QuotaService.get_quota(uid)
                        qd_fin = "∞ Unlimited" if q_fin.get("unlimited") else str(q_fin["total"])

                        summary = (
                            f"{status_line}!\n"
                            f"{'─' * 20}\n"
                            f"✅ Berhasil  : {success}\n"
                            f"❌ Gagal     : {failed}\n"
                            f"{skipped_line}"
                            f"⏱ Durasi    : {dur_str}\n"
                            f"📦 Sisa quota: {qd_fin}"
                        )

                        # Sertakan ID pesan yang gagal agar user bisa retry
                        if failed_ids:
                            MAX_SHOW = 5
                            shown    = failed_ids[:MAX_SHOW]
                            ids_str  = ", ".join(f"<code>{i}</code>" for i in shown)
                            extra    = f" (+{len(failed_ids) - MAX_SHOW} lainnya)" if len(failed_ids) > MAX_SHOW else ""
                            summary += f"\n\n⚠️ <b>Pesan gagal:</b> {ids_str}{extra}"

                        # Edit pesan progress → ringkasan final
                        await _edit_b(summary)

                        # Kirim notifikasi terpisah agar ringkasan mudah ditemukan
                        notif_icon = "🛑" if cancelled else ("⚠️" if failed else "✅")
                        notif_text = (
                            f"{notif_icon} <b>Download selesai</b> — "
                            f"{success} berhasil"
                            + (f", {failed} gagal" if failed else "")
                            + (f", {skipped} dilewati" if cancelled and skipped > 0 else "")
                            + f" — {dur_str}"
                        )

                        # Tambah tombol Retry jika ada pesan yang gagal
                        notif_markup = None
                        if failed_ids:
                            _retry_store[uid] = (chat_a, failed_ids)
                            notif_markup = InlineKeyboardMarkup([[
                                InlineKeyboardButton(
                                    f"🔄 Retry Gagal ({len(failed_ids)} file)",
                                    callback_data=f"retry:{uid}",
                                )
                            ]])

                        await bot.send_message(
                            chat_id, notif_text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=notif_markup,
                        )

                        if not cancelled:
                            await _quota_warn(bot, chat_id, uid)
                except Exception as e:
                    logger.error(f"get bulk job error uid={uid}: {e}", exc_info=True)
                    QuotaService.add_quota(uid, count)
                    await _edit_b(f"❌ Terjadi kesalahan tak terduga: {e}")

            pos = queue_manager.add_job(bulk_job, is_prem, uid)
            if pos == 0:
                QuotaService.add_quota(uid, count)
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=pmsg_id,
                        text="❌ Server sedang sibuk, coba lagi nanti.",
                    )
                except TgBadRequest:
                    pass
                return

            if pos > 1:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=pmsg_id,
                        text=(
                            f"📋 <b>Masuk antrian!</b>\n"
                            f"Posisi kamu: <b>ke-{pos}</b> dalam antrian\n"
                            f"⏳ {count} pesan akan diunduh setelah giliran tiba.\n\n"
                            f"📦 Sisa quota: <b>{quota_disp}</b>"
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                except TgBadRequest:
                    pass
            else:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=pmsg_id,
                        text=(
                            f"⏳ <b>Mengunduh {count} pesan... (0/{count})</b>\n"
                            f"📦 Sisa quota: {quota_disp}\n\n"
                            "<i>Ketik /canceldownload untuk membatalkan.</i>"
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                except TgBadRequest:
                    pass
            return

        # ── Lebih dari 2 argumen ──────────────────────────────────────────
        await update.message.reply_text(_HELP_TEXT, parse_mode=ParseMode.HTML)

    # ── Callback: tombol Retry Gagal ─────────────────────────────────────
    async def retry_handler(update, context):
        query = update.callback_query
        await query.answer()

        data = query.data or ""
        if not data.startswith("retry:"):
            return

        uid = int(data.split(":", 1)[1])
        caller_uid = query.from_user.id

        # Hanya pemilik request yang boleh retry
        if caller_uid != uid:
            await query.answer("❌ Tombol ini bukan milikmu.", show_alert=True)
            return

        retry_data = _retry_store.pop(uid, None)
        if not retry_data:
            await query.edit_message_reply_markup(reply_markup=None)
            await context.bot.send_message(
                query.message.chat_id,
                "⚠️ Data retry sudah kedaluwarsa. Jalankan ulang perintah /get.",
            )
            return

        channel, ids_to_retry = retry_data
        n = len(ids_to_retry)

        # Hapus tombol dari pesan notifikasi lama
        await query.edit_message_reply_markup(reply_markup=None)

        # Cek quota
        quota = QuotaService.get_quota(uid)
        if not quota.get("unlimited") and quota["total"] < n:
            await context.bot.send_message(
                query.message.chat_id,
                f"❌ <b>Quota tidak cukup untuk retry.</b>\n\n"
                f"Dibutuhkan: {n}\nTersedia: {quota['total']}",
                parse_mode=ParseMode.HTML,
            )
            return

        for _ in range(n):
            QuotaService.use_quota(uid)

        is_prem = QuotaService.is_premium(uid)
        lock    = _get_lock(uid)
        chat_id = query.message.chat_id

        if not queue_manager.can_add(is_prem):
            QuotaService.add_quota(uid, n)
            await context.bot.send_message(chat_id, "❌ Server sedang sibuk, coba lagi nanti.")
            return

        pmsg    = await context.bot.send_message(
            chat_id,
            f"🔄 <b>Retry {n} pesan gagal... (0/{n})</b>\n\n"
            "<i>Ketik /canceldownload untuk membatalkan.</i>",
            parse_mode=ParseMode.HTML,
        )
        pmsg_id = pmsg.message_id

        async def _edit_r(text: str):
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=pmsg_id,
                    text=text, parse_mode=ParseMode.HTML,
                )
            except TgBadRequest:
                pass

        async def retry_job():
            try:
                async with lock:
                    uc = await session_manager.get_for_chat(uid, channel)
                    if not uc:
                        QuotaService.add_quota(uid, n)
                        message = (
                            "❌ Bot tidak bisa mengakses channel publik ini."
                            if is_public_chat(channel)
                            else "❌ Session tidak valid. Silakan /login ulang."
                        )
                        await _edit_r(message)
                        return

                    success        = 0
                    failed         = 0
                    new_failed_ids: list[int] = []
                    last_edit      = 0.0
                    retry_start    = time.monotonic()
                    _bulk_cancel[uid] = False

                    for msg_id in ids_to_retry:
                        if _bulk_cancel.get(uid):
                            remaining = n - success - failed
                            QuotaService.add_quota(uid, remaining)
                            _bulk_cancel[uid] = False
                            break

                        ok, reason = await SafeForward.run(uc, context.bot, chat_id, channel, msg_id, is_premium=is_prem)
                        if ok:
                            success += 1
                        else:
                            failed += 1
                            new_failed_ids.append(msg_id)
                            logger.info(f"retry skip msg {msg_id}: {reason}")

                        now = time.monotonic()
                        if now - last_edit >= 3.0:
                            done = success + failed
                            await _edit_r(
                                f"🔄 <b>Retry... ({done}/{n})</b>\n"
                                f"✅ {success}  ❌ {failed}\n\n"
                                "<i>Ketik /canceldownload untuk membatalkan.</i>"
                            )
                            last_edit = now

                    if failed:
                        QuotaService.add_quota(uid, failed)

                    elapsed_s = time.monotonic() - retry_start
                    dur_str   = f"{int(elapsed_s)} detik" if elapsed_s < 60 else f"{elapsed_s / 60:.1f} menit"
                    q_fin     = QuotaService.get_quota(uid)
                    qd_fin    = "∞ Unlimited" if q_fin.get("unlimited") else str(q_fin["total"])

                    summary = (
                        f"{'✅ Retry selesai' if not failed else '⚠️ Retry selesai'}!\n"
                        f"{'─' * 20}\n"
                        f"✅ Berhasil  : {success}\n"
                        f"❌ Gagal     : {failed}\n"
                        f"⏱ Durasi    : {dur_str}\n"
                        f"📦 Sisa quota: {qd_fin}"
                    )
                    if new_failed_ids:
                        MAX_SHOW = 5
                        shown    = new_failed_ids[:MAX_SHOW]
                        ids_str  = ", ".join(f"<code>{i}</code>" for i in shown)
                        extra    = f" (+{len(new_failed_ids) - MAX_SHOW} lainnya)" if len(new_failed_ids) > MAX_SHOW else ""
                        summary += f"\n\n⚠️ <b>Masih gagal:</b> {ids_str}{extra}"

                    await _edit_r(summary)

                    # Notifikasi ringkas + tombol retry lagi jika masih ada yang gagal
                    notif_icon   = "⚠️" if failed else "✅"
                    notif_text   = (
                        f"{notif_icon} <b>Retry selesai</b> — "
                        f"{success} berhasil"
                        + (f", {failed} masih gagal" if failed else "")
                        + f" — {dur_str}"
                    )
                    notif_markup = None
                    if new_failed_ids:
                        _retry_store[uid] = (channel, new_failed_ids)
                        notif_markup = InlineKeyboardMarkup([[
                            InlineKeyboardButton(
                                f"🔄 Retry Lagi ({len(new_failed_ids)} file)",
                                callback_data=f"retry:{uid}",
                            )
                        ]])
                    await context.bot.send_message(
                        chat_id, notif_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=notif_markup,
                    )
                    await _quota_warn(context.bot, chat_id, uid)
            except Exception as e:
                logger.error(f"retry job error uid={uid}: {e}", exc_info=True)
                QuotaService.add_quota(uid, n)
                await _edit_r(f"❌ Terjadi kesalahan tak terduga: {e}")

        pos_r = queue_manager.add_job(retry_job, is_prem, uid)
        if pos_r == 0:
            QuotaService.add_quota(uid, n)
            await context.bot.send_message(
                chat_id, "❌ Server sedang sibuk, coba lagi nanti."
            )
            return

    app.add_handler(CommandHandler("canceldownload", cancel_download))
    app.add_handler(CommandHandler("get",            get_cmd))
    app.add_handler(CommandHandler("single",         get_cmd))   # alias
    app.add_handler(CommandHandler("bulk",           get_cmd))   # alias
    app.add_handler(CallbackQueryHandler(retry_handler, pattern=r"^retry:\d+$"))
