import os
import time

from telegram.ext import CommandHandler
from telegram.constants import ParseMode

from modules.terabox import (
    is_terabox_link, get_terabox_files, get_download_url,
    download_file, get_bduss,
)
from modules.quota_service import QuotaService
from modules.queue_manager import queue_manager
from modules.activity_log import log as activity_log
from modules.channel_guard import require_member
from database.db import db
from logger import logger
from config import MAX_FILE_SIZE_BYTES, ADMIN_IDS

_DOWNLOADS_DIR   = "downloads"
_PROGRESS_INTERVAL = 3.0
_MAX_TG_DIRECT   = 50 * 1024 * 1024


def _fmt_size(b: int) -> str:
    if b >= 1024 ** 3:
        return f"{b / 1024**3:.1f} GB"
    if b >= 1024 ** 2:
        return f"{b / 1024**2:.1f} MB"
    if b >= 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b} B"


def _check_logged_in(uid: int) -> bool:
    user = db.get_user(uid)
    return bool(user and user.get("session_string"))


def setup(app):

    # ── /terabox <link> ──────────────────────────────────────────────────
    async def terabox_cmd(update, context):
        uid     = update.effective_user.id
        chat_id = update.effective_chat.id
        bot     = context.bot

        if not await require_member(bot, update):
            return

        if not context.args:
            return await update.message.reply_text(
                "📦 <b>Terabox Downloader</b>\n\n"
                "Contoh:\n"
                "<code>/terabox https://1024terabox.com/s/XXXXX</code>\n\n"
                "<i>Mendukung: 1024terabox.com, terabox.com, teraboxapp.com, "
                "4funbox.com, nephobox.com, dll.</i>",
                parse_mode=ParseMode.HTML,
            )

        url = context.args[0].strip()
        if not is_terabox_link(url):
            return await update.message.reply_text(
                "❌ Link bukan dari Terabox yang didukung.\n"
                "Contoh: <code>https://1024terabox.com/s/XXXXX</code>",
                parse_mode=ParseMode.HTML,
            )

        if not get_bduss():
            return await update.message.reply_text(
                "⚙️ <b>Fitur Terabox sedang dalam pengembangan</b>\n\n"
                "Fitur ini belum tersedia saat ini. Pantau terus update bot!\n"
                "Hubungi admin untuk informasi lebih lanjut.",
                parse_mode=ParseMode.HTML,
            )

        if not QuotaService.use_quota(uid):
            return await update.message.reply_text(
                "❌ <b>Quota habis!</b>\n\n"
                "• Gunakan /referral untuk bonus quota gratis.\n"
                "• Atau /pay untuk upgrade ke Premium.",
                parse_mode=ParseMode.HTML,
            )

        is_prem = QuotaService.is_premium(uid)
        if not queue_manager.can_add(is_prem):
            QuotaService.add_quota(uid, 1)
            return await update.message.reply_text("❌ Server sedang sibuk, coba lagi nanti.")

        pmsg = await update.message.reply_text(
            "🔍 Mengambil info file dari Terabox...",
        )

        async def edit(text: str, html: bool = True):
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=pmsg.message_id,
                    text=text,
                    parse_mode=ParseMode.HTML if html else None,
                )
            except Exception:
                pass

        # ── Ambil info file ────────────────────────────────────────────────
        try:
            files, surl = await get_terabox_files(url)
        except ValueError as e:
            QuotaService.add_quota(uid, 1)
            return await edit(f"❌ {e}")
        except Exception as e:
            logger.error(f"[terabox] get_files uid={uid}: {e}", exc_info=True)
            QuotaService.add_quota(uid, 1)
            return await edit("❌ Gagal mengambil info file. Coba lagi nanti.")

        downloadable = [f for f in files if not f["is_dir"]]
        if not downloadable:
            QuotaService.add_quota(uid, 1)
            return await edit("❌ Tidak ada file yang bisa didownload dari link ini.")

        file_info = downloadable[0]
        fname     = file_info["filename"]
        fsize     = file_info["size"]

        if fsize > MAX_FILE_SIZE_BYTES:
            QuotaService.add_quota(uid, 1)
            return await edit(
                f"❌ File terlalu besar: <b>{_fmt_size(fsize)}</b>\n"
                f"Batas maksimal: <b>{_fmt_size(MAX_FILE_SIZE_BYTES)}</b>"
            )

        # ── Dapatkan download URL ──────────────────────────────────────────
        await edit(
            f"🔗 Mendapatkan link download...\n"
            f"📄 <b>{fname}</b>\n"
            f"📦 {_fmt_size(fsize) if fsize else '?'}"
        )

        try:
            dlink = await get_download_url(file_info)
        except PermissionError as e:
            QuotaService.add_quota(uid, 1)
            return await edit(f"⚙️ {e}")
        except ValueError as e:
            QuotaService.add_quota(uid, 1)
            return await edit(f"❌ {e}")
        except Exception as e:
            logger.error(f"[terabox] get_dl_url uid={uid}: {e}", exc_info=True)
            QuotaService.add_quota(uid, 1)
            return await edit("❌ Gagal mendapatkan link download. Coba lagi nanti.")

        # ── Bungkus download + kirim ke dalam antrian ─────────────────────
        safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in fname)
        dest = os.path.join(_DOWNLOADS_DIR, f"tb_{uid}_{int(time.time())}_{safe}")

        async def terabox_job():
            last_edit_t = [time.monotonic()]

            async def on_progress(downloaded: int, total: int):
                if time.monotonic() - last_edit_t[0] < _PROGRESS_INTERVAL:
                    return
                last_edit_t[0] = time.monotonic()
                if total:
                    pct = downloaded / total * 100
                    bar = "█" * int(pct // 10) + "░" * (10 - int(pct // 10))
                    txt = (f"⬇️ <b>Mengunduh...</b>\n📄 {fname}\n"
                           f"[{bar}] {pct:.1f}%\n"
                           f"{_fmt_size(downloaded)} / {_fmt_size(total)}")
                else:
                    txt = (f"⬇️ <b>Mengunduh...</b>\n📄 {fname}\n"
                           f"{_fmt_size(downloaded)} terunduh...")
                await edit(txt)

            await edit(
                f"⬇️ <b>Mengunduh dari Terabox...</b>\n"
                f"📄 {fname}\n"
                f"📦 {_fmt_size(fsize) if fsize else '?'}"
            )

            try:
                await download_file(dlink, dest, on_progress=on_progress)
            except Exception as e:
                logger.error(f"[terabox] download uid={uid}: {e}", exc_info=True)
                QuotaService.add_quota(uid, 1)
                if os.path.exists(dest):
                    os.remove(dest)
                await edit(f"❌ Gagal mengunduh file: {e}")
                return

            actual_size = os.path.getsize(dest)
            await edit(f"📤 Mengirim <b>{fname}</b> ke chat...")

            try:
                caption = (f"📦 <b>{fname}</b>\n"
                           f"📁 {_fmt_size(actual_size)}\n\n"
                           f"<i>via @BOT_downloader_bot</i>")
                if actual_size <= _MAX_TG_DIRECT:
                    with open(dest, "rb") as f:
                        await bot.send_document(
                            chat_id=chat_id, document=f,
                            filename=fname, caption=caption,
                            parse_mode=ParseMode.HTML,
                        )
                else:
                    if not _check_logged_in(uid):
                        QuotaService.add_quota(uid, 1)
                        os.remove(dest)
                        await edit(
                            f"⚠️ File besar ({_fmt_size(actual_size)}) — kamu perlu /login "
                            "agar bot bisa kirim ke Saved Messages."
                        )
                        return
                    from modules.session_manager import session_manager
                    uc = await session_manager.get(uid)
                    if not uc:
                        QuotaService.add_quota(uid, 1)
                        os.remove(dest)
                        await edit("❌ Session tidak valid. Silakan /login ulang.")
                        return
                    await uc.send_document("me", dest, caption=caption)
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(f"✅ <b>{fname}</b> ({_fmt_size(actual_size)}) dikirim ke "
                              "<b>Saved Messages</b> kamu karena ukurannya melebihi 50 MB."),
                        parse_mode=ParseMode.HTML,
                    )

                activity_log(uid, "terabox_download", fname)
                q   = QuotaService.get_quota(uid)
                qd  = "∞ Unlimited" if q.get("unlimited") else str(q["total"])
                await edit(f"✅ <b>Selesai!</b>\n📄 {fname}\n📦 Sisa quota: <b>{qd}</b>")

            except Exception as e:
                logger.error(f"[terabox] send uid={uid}: {e}", exc_info=True)
                QuotaService.add_quota(uid, 1)
                await edit(f"❌ Gagal mengirim file ke Telegram: {e}")
            finally:
                try:
                    os.remove(dest)
                except Exception:
                    pass

        # ── Masukkan ke antrian & tampilkan posisi ─────────────────────────
        pos = queue_manager.add_job(terabox_job, is_prem, uid)
        if pos == 0:
            QuotaService.add_quota(uid, 1)
            await edit("❌ Server sedang sibuk, coba lagi nanti.")
            return

        q          = QuotaService.get_quota(uid)
        quota_disp = "∞ Unlimited" if q.get("unlimited") else str(q["total"])

        if pos > 1:
            await edit(
                f"📋 <b>Masuk antrian!</b>\n"
                f"Posisi kamu: <b>ke-{pos}</b> dalam antrian\n"
                f"⏳ Download akan dimulai setelah giliran tiba.\n\n"
                f"📄 {fname}  •  📦 {_fmt_size(fsize) if fsize else '?'}\n"
                f"Sisa quota: <b>{quota_disp}</b>"
            )
        else:
            await edit(
                f"⏳ Giliran kamu berikutnya!\n"
                f"📄 {fname}  •  📦 {_fmt_size(fsize) if fsize else '?'}\n"
                f"Sisa quota: <b>{quota_disp}</b>"
            )

    # ── /setterabox [cookie] (admin only) ───────────────────────────────
    async def set_terabox(update, context):
        uid = update.effective_user.id
        if uid not in ADMIN_IDS:
            return await update.message.reply_text("❌ Kamu bukan admin.")

        if not context.args:
            has_cookie = bool(get_bduss())
            status_line = "✅ <b>Sudah terkonfigurasi</b>" if has_cookie else "❌ <b>Belum dikonfigurasi</b>"
            return await update.message.reply_text(
                f"⚙️ <b>Konfigurasi Terabox</b>\n"
                f"Status: {status_line}\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "📋 <b>Cara setup — PC/Laptop:</b>\n\n"
                "1️⃣ Buka <a href=\"https://www.terabox.com\">terabox.com</a>, pastikan sudah login\n"
                "2️⃣ Tekan <b>F12</b> → tab <b>Application</b> → <b>Cookies</b> → pilih <code>terabox.com</code>\n"
                "3️⃣ Cari baris <code>ndus</code> → copy kolom <b>Value</b>-nya\n"
                "4️⃣ Kirim: <code>/setterabox NILAI_ndus</code>\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "📱 <b>Cara setup — Android (Kiwi Browser):</b>\n\n"
                "1️⃣ Install <b>Kiwi Browser</b> → login ke terabox.com\n"
                "2️⃣ Tap <b>⋮ → DevTools</b> → tab <b>Aplikasi</b> → <b>Cookie</b>\n"
                "3️⃣ Pilih domain <code>terabox.com</code>, filter dengan kata <code>ndus</code>\n"
                "4️⃣ Tap baris <code>ndus</code> → copy nilainya\n"
                "5️⃣ Kirim: <code>/setterabox NILAI_ndus</code>\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "💡 <i>Cookie <code>ndus</code> adalah cookie sesi Terabox internasional. "
                "Pesan kamu akan dihapus otomatis setelah disimpan.</i>\n\n"
                "🗑 Untuk hapus: <code>/setterabox hapus</code>",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

        val = " ".join(context.args).strip()

        if val.lower() == "hapus":
            db.config_delete("terabox_bduss")
            return await update.message.reply_text("🗑 Cookie Terabox berhasil dihapus.")

        # Jika user paste nilai mentah tanpa nama cookie, simpan apa adanya
        # Jika ada format "ndus=nilai" atau "BDUSS=nilai", simpan lengkap biar header-nya benar
        if len(val) < 20:
            return await update.message.reply_text(
                "❌ Cookie terlalu pendek. Pastikan kamu copy nilai <b>ndus</b> yang benar (minimal 20 karakter).\n\n"
                "Ketik /setterabox tanpa argumen untuk panduan lengkap.",
                parse_mode=ParseMode.HTML,
            )

        db.config_set("terabox_bduss", val)

        # Hapus pesan admin agar cookie tidak terekspos di chat
        try:
            await update.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ <b>Cookie Terabox berhasil disimpan!</b>\n\n"
                 "User sekarang bisa download dari Terabox dengan <code>/terabox &lt;link&gt;</code>.\n\n"
                 "<i>Pesan yang berisi cookie sudah dihapus otomatis untuk keamanan.</i>",
            parse_mode=ParseMode.HTML,
        )

    app.add_handler(CommandHandler("terabox",    terabox_cmd))
    app.add_handler(CommandHandler("setterabox", set_terabox))
