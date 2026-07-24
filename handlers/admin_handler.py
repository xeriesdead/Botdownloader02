import asyncio
from datetime import date

from telegram.ext import CommandHandler
from telegram.constants import ParseMode

from config import ADMIN_IDS
from database.db import db
from modules.premium_system import set_premium, remove_premium, premium_info
from modules.quota_service import QuotaService
from modules.activity_log import get_user_activity, get_recent_activity, get_top_downloaders


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def admin_only(func):
    async def wrapper(update, context):
        if not is_admin(update.effective_user.id):
            return await update.message.reply_text("❌ Kamu bukan admin.")
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


def setup(app):

    # ── /addpremium <user_id> [hari] ─────────────────────────────────────
    @admin_only
    async def add_premium(update, context):
        if not context.args:
            return await update.message.reply_text(
                "Contoh: <code>/addpremium 123456789 30</code>",
                parse_mode=ParseMode.HTML,
            )
        try:
            uid  = int(context.args[0])
            days = int(context.args[1]) if len(context.args) > 1 else 30
        except ValueError:
            return await update.message.reply_text("❌ Format salah.")

        if not db.get_user(uid):
            return await update.message.reply_text("❌ User tidak ditemukan di database.")

        set_premium(uid, days)

        notif_status = ""
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    f"🎉 <b>Selamat! Kamu mendapatkan akses Premium!</b>\n"
                    f"{'─' * 28}\n"
                    f"⏳ Durasi   : <b>{days} hari</b>\n"
                    f"📅 Status   : {premium_info(uid)}\n\n"
                    f"Nikmati fitur premium tanpa batas quota. "
                    f"Terima kasih telah menggunakan bot ini! 🚀"
                ),
                parse_mode=ParseMode.HTML,
            )
            notif_status = "✅ Notifikasi terkirim ke user."
        except Exception as e:
            err = str(e).lower()
            if "blocked" in err or "deactivated" in err:
                notif_status = "⚠️ Notifikasi gagal — user memblokir bot."
            else:
                notif_status = f"⚠️ Notifikasi gagal: {e}"

        await update.message.reply_text(
            f"✅ User <code>{uid}</code> dijadikan premium selama <b>{days} hari</b>.\n"
            f"Status: {premium_info(uid)}\n"
            f"{notif_status}",
            parse_mode=ParseMode.HTML,
        )

    # ── /removepremium <user_id> ─────────────────────────────────────────
    @admin_only
    async def rm_premium(update, context):
        if not context.args:
            return await update.message.reply_text(
                "Contoh: <code>/removepremium 123456789</code>",
                parse_mode=ParseMode.HTML,
            )
        try:
            uid = int(context.args[0])
        except ValueError:
            return await update.message.reply_text("❌ Format salah.")

        remove_premium(uid)
        await update.message.reply_text(
            f"✅ Premium user <code>{uid}</code> telah dihapus.",
            parse_mode=ParseMode.HTML,
        )

    # ── /addquota <user_id> <jumlah> ─────────────────────────────────────
    @admin_only
    async def add_quota(update, context):
        if not context.args or len(context.args) < 2:
            return await update.message.reply_text(
                "Contoh: <code>/addquota 123456789 10</code>",
                parse_mode=ParseMode.HTML,
            )
        try:
            uid    = int(context.args[0])
            amount = int(context.args[1])
        except ValueError:
            return await update.message.reply_text("❌ Format salah.")

        if not db.get_user(uid):
            return await update.message.reply_text("❌ User tidak ditemukan.")

        QuotaService.add_bonus(uid, amount)
        q = QuotaService.get_quota(uid)
        total_str = "∞ Unlimited" if q.get("unlimited") else str(q["total"])
        await update.message.reply_text(
            f"✅ Ditambahkan <b>{amount} bonus quota</b> ke user <code>{uid}</code>.\n"
            f"<i>(Bonus quota bisa stack, tidak terikat batas harian)</i>\n"
            f"Total sekarang: <b>{total_str}</b>",
            parse_mode=ParseMode.HTML,
        )

    # ── /userinfo <user_id> ──────────────────────────────────────────────
    @admin_only
    async def user_info(update, context):
        if not context.args:
            return await update.message.reply_text(
                "Contoh: <code>/userinfo 123456789</code>",
                parse_mode=ParseMode.HTML,
            )
        try:
            uid = int(context.args[0])
        except ValueError:
            return await update.message.reply_text("❌ Format salah.")

        user = db.get_user(uid)
        if not user:
            return await update.message.reply_text("❌ User tidak ditemukan.")

        q      = QuotaService.get_quota(uid)
        banned = "🚫 Ya" if user.get("banned") else "✅ Tidak"
        if q.get("unlimited"):
            quota_str = "∞ Unlimited (Premium)"
        else:
            quota_str = f"{q['quota']} harian + {q['bonus']} bonus = <b>{q['total']}</b>"
        await update.message.reply_text(
            f"👤 <b>Info User <code>{uid}</code></b>\n"
            f"{'─' * 26}\n"
            f"Username  : @{user.get('username') or '-'}\n"
            f"Phone     : {user.get('phone') or '-'}\n"
            f"Session   : {'✅' if user.get('session_string') else '❌'}\n"
            f"Premium   : {premium_info(uid)}\n"
            f"Quota     : {quota_str}\n"
            f"Referrer  : {user.get('referrer_id') or '-'}\n"
            f"Banned    : {banned}",
            parse_mode=ParseMode.HTML,
        )

    # ── /stats ───────────────────────────────────────────────────────────
    @admin_only
    async def stats(update, context):
        from datetime import date
        today = str(date.today())

        total        = len(db.get_all_users())
        login_ct     = len(db.fetchall("SELECT user_id FROM users WHERE session_string IS NOT NULL"))
        nologin_ct   = total - login_ct
        prem_aktif   = len(db.fetchall(
            "SELECT user_id FROM users WHERE premium = 1 AND (premium_until IS NULL OR premium_until > NOW()::TEXT)"
        ))
        prem_expired = len(db.fetchall(
            "SELECT user_id FROM users WHERE premium = 1 AND premium_until IS NOT NULL AND premium_until <= NOW()::TEXT"
        ))
        free_ct      = total - prem_aktif - prem_expired
        banned_ct    = len(db.fetchall("SELECT user_id FROM users WHERE banned = 1"))
        referral_ct  = len(db.fetchall("SELECT user_id FROM users WHERE referrer_id IS NOT NULL"))
        bonus_ct     = len(db.fetchall("SELECT user_id FROM users WHERE bonus_quota > 0"))
        active_today = len(db.fetchall(
            "SELECT user_id FROM users WHERE last_reset = ?", (today,)
        ))

        await update.message.reply_text(
            f"📊 <b>Statistik Bot</b>\n"
            f"{'─' * 28}\n"
            f"👥 Total user terdaftar : <b>{total}</b>\n"
            f"🔑 Sudah /login         : <b>{login_ct}</b>\n"
            f"🔓 Belum /login         : <b>{nologin_ct}</b>\n"
            f"{'─' * 28}\n"
            f"💎 Premium aktif        : <b>{prem_aktif}</b>\n"
            f"⌛ Premium kedaluwarsa  : <b>{prem_expired}</b>\n"
            f"🆓 User gratis          : <b>{free_ct}</b>\n"
            f"🚫 Dibanned             : <b>{banned_ct}</b>\n"
            f"{'─' * 28}\n"
            f"🔗 Dari referral        : <b>{referral_ct}</b>\n"
            f"🎁 Punya bonus quota    : <b>{bonus_ct}</b>\n"
            f"📅 Aktif hari ini       : <b>{active_today}</b>",
            parse_mode=ParseMode.HTML,
        )

    # ── /loginlist [halaman] ─────────────────────────────────────────────
    @admin_only
    async def login_list(update, context):
        PAGE_SIZE = 20
        try:
            page = int(context.args[0]) if context.args else 1
        except ValueError:
            page = 1

        users = db.fetchall(
            "SELECT user_id, username, phone FROM users "
            "WHERE session_string IS NOT NULL ORDER BY user_id ASC"
        )
        total = len(users)
        if total == 0:
            return await update.message.reply_text("📭 Belum ada user yang login.")

        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(page, total_pages))
        start = (page - 1) * PAGE_SIZE
        chunk = users[start:start + PAGE_SIZE]

        lines = []
        for i, u in enumerate(chunk, start=start + 1):
            uname = f"@{u['username']}" if u.get("username") else "-"
            phone = u.get("phone") or "-"
            lines.append(f"{i}. <code>{u['user_id']}</code>  {uname}  <i>{phone}</i>")

        await update.message.reply_text(
            f"🔑 <b>User yang sudah Login</b>  [{page}/{total_pages}]\n"
            f"Total: <b>{total}</b> user\n"
            f"{'─' * 30}\n"
            + "\n".join(lines) +
            (f"\n\n<i>Halaman berikutnya: /loginlist {page + 1}</i>" if page < total_pages else ""),
            parse_mode=ParseMode.HTML,
        )

    # ── /ban <user_id> ───────────────────────────────────────────────────
    @admin_only
    async def ban_user(update, context):
        if not context.args:
            return await update.message.reply_text(
                "Contoh: <code>/ban 123456789</code>",
                parse_mode=ParseMode.HTML,
            )
        try:
            uid = int(context.args[0])
        except ValueError:
            return await update.message.reply_text("❌ Format salah.")

        if uid in ADMIN_IDS:
            return await update.message.reply_text("❌ Tidak bisa ban sesama admin.")

        if not db.get_user(uid):
            return await update.message.reply_text("❌ User tidak ditemukan.")

        db.update("UPDATE users SET banned = 1 WHERE user_id = ?", (uid,))
        await update.message.reply_text(
            f"🚫 User <code>{uid}</code> telah <b>dibanned</b>.\n"
            "User tidak bisa menggunakan bot sampai di-unban.",
            parse_mode=ParseMode.HTML,
        )

    # ── /unban <user_id> ─────────────────────────────────────────────────
    @admin_only
    async def unban_user(update, context):
        if not context.args:
            return await update.message.reply_text(
                "Contoh: <code>/unban 123456789</code>",
                parse_mode=ParseMode.HTML,
            )
        try:
            uid = int(context.args[0])
        except ValueError:
            return await update.message.reply_text("❌ Format salah.")

        db.update("UPDATE users SET banned = 0 WHERE user_id = ?", (uid,))
        await update.message.reply_text(
            f"✅ User <code>{uid}</code> telah di-<b>unban</b>.",
            parse_mode=ParseMode.HTML,
        )

    # ── /broadcast <pesan> ───────────────────────────────────────────────
    @admin_only
    async def broadcast(update, context):
        if not context.args:
            return await update.message.reply_text(
                "Contoh: <code>/broadcast Halo semua! Bot baru diupdate.</code>",
                parse_mode=ParseMode.HTML,
            )

        text      = update.message.text.split(None, 1)[1]
        all_users = db.get_all_users()
        total     = len(all_users)

        status_msg = await update.message.reply_text(
            f"📢 Mengirim broadcast ke <b>{total}</b> user...",
            parse_mode=ParseMode.HTML,
        )

        sent = failed = blocked = 0

        for user in all_users:
            uid = user["user_id"]
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"📢 <b>Pesan dari Admin</b>\n\n{text}",
                    parse_mode=ParseMode.HTML,
                )
                sent += 1
            except Exception as e:
                err = str(e).lower()
                if "blocked" in err or "deactivated" in err:
                    blocked += 1
                else:
                    failed += 1
            await asyncio.sleep(0.05)

        await status_msg.edit_text(
            f"📢 <b>Broadcast Selesai</b>\n"
            f"{'─' * 22}\n"
            f"✅ Terkirim  : <b>{sent}</b>\n"
            f"🚫 Diblokir  : <b>{blocked}</b>\n"
            f"❌ Error     : <b>{failed}</b>\n"
            f"👥 Total     : <b>{total}</b>",
            parse_mode=ParseMode.HTML,
        )

    # ── /bulkpremium <hari> <user_id1> <user_id2> ... ────────────────────
    @admin_only
    async def bulk_premium(update, context):
        if not context.args or len(context.args) < 2:
            return await update.message.reply_text(
                "Contoh: <code>/bulkpremium 30 123456789 987654321 111222333</code>\n"
                "<i>Argumen pertama = jumlah hari, sisanya = user_id</i>",
                parse_mode=ParseMode.HTML,
            )
        try:
            days = int(context.args[0])
            uids = [int(x) for x in context.args[1:]]
        except ValueError:
            return await update.message.reply_text("❌ Format salah. Pastikan hari dan user_id berupa angka.")

        status_msg = await update.message.reply_text(
            f"⏳ Memproses <b>{len(uids)}</b> user...",
            parse_mode=ParseMode.HTML,
        )

        success = []
        not_found = []
        notif_failed = []

        for uid in uids:
            if not db.get_user(uid):
                not_found.append(uid)
                continue
            set_premium(uid, days)
            success.append(uid)
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=(
                        f"🎉 <b>Selamat! Kamu mendapatkan akses Premium!</b>\n"
                        f"{'─' * 28}\n"
                        f"⏳ Durasi   : <b>{days} hari</b>\n"
                        f"📅 Status   : {premium_info(uid)}\n\n"
                        f"Nikmati fitur premium tanpa batas quota. "
                        f"Terima kasih telah menggunakan bot ini! 🚀"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                notif_failed.append(uid)
            await asyncio.sleep(0.05)

        lines = [
            f"✅ <b>Bulk Premium Selesai</b>\n{'─' * 26}",
            f"💎 Berhasil         : <b>{len(success)}</b> user",
            f"❌ Tidak ditemukan  : <b>{len(not_found)}</b> user",
            f"⚠️ Notif gagal      : <b>{len(notif_failed)}</b> user",
            f"⏳ Durasi           : <b>{days} hari</b>",
        ]
        if not_found:
            nf_str = ", ".join(f"<code>{x}</code>" for x in not_found)
            lines.append(f"\nTidak ditemukan: {nf_str}")
        if notif_failed:
            nf2_str = ", ".join(f"<code>{x}</code>" for x in notif_failed)
            lines.append(f"Notif gagal: {nf2_str}")

        await status_msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)

    # ── /bulkaddquota <jumlah> <user_id1> <user_id2> ... ─────────────────
    @admin_only
    async def bulk_add_quota(update, context):
        if not context.args or len(context.args) < 2:
            return await update.message.reply_text(
                "Contoh: <code>/bulkaddquota 10 123456789 987654321 111222333</code>\n"
                "<i>Argumen pertama = jumlah quota, sisanya = user_id</i>",
                parse_mode=ParseMode.HTML,
            )
        try:
            amount = int(context.args[0])
            uids   = [int(x) for x in context.args[1:]]
        except ValueError:
            return await update.message.reply_text("❌ Format salah. Pastikan jumlah dan user_id berupa angka.")

        status_msg = await update.message.reply_text(
            f"⏳ Memproses <b>{len(uids)}</b> user...",
            parse_mode=ParseMode.HTML,
        )

        success   = []
        not_found = []

        for uid in uids:
            if not db.get_user(uid):
                not_found.append(uid)
                continue
            QuotaService.add_bonus(uid, amount)
            success.append(uid)
            try:
                q = QuotaService.get_quota(uid)
                total_str = "∞ Unlimited" if q.get("unlimited") else str(q["total"])
                await context.bot.send_message(
                    chat_id=uid,
                    text=(
                        f"🎁 <b>Kamu mendapat bonus quota!</b>\n"
                        f"{'─' * 26}\n"
                        f"➕ Ditambahkan  : <b>{amount} quota</b>\n"
                        f"📦 Total quota  : <b>{total_str}</b>\n\n"
                        f"Gunakan /get untuk mulai download sekarang!"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            await asyncio.sleep(0.05)

        lines = [
            f"✅ <b>Bulk Quota Selesai</b>\n{'─' * 26}",
            f"🎁 Berhasil         : <b>{len(success)}</b> user",
            f"❌ Tidak ditemukan  : <b>{len(not_found)}</b> user",
            f"➕ Quota ditambahkan: <b>+{amount}</b> per user",
        ]
        if not_found:
            nf_str = ", ".join(f"<code>{x}</code>" for x in not_found)
            lines.append(f"\nTidak ditemukan: {nf_str}")

        await status_msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)

    # ── /activity <user_id> [YYYY-MM-DD] ─────────────────────────────────
    @admin_only
    async def activity(update, context):
        if not context.args:
            return await update.message.reply_text(
                "Contoh:\n"
                "• <code>/activity 123456789</code> — hari ini\n"
                "• <code>/activity 123456789 2026-05-29</code> — tanggal tertentu",
                parse_mode=ParseMode.HTML,
            )
        try:
            uid = int(context.args[0])
        except ValueError:
            return await update.message.reply_text("❌ user_id harus berupa angka.")

        filter_date = context.args[1] if len(context.args) > 1 else str(date.today())
        user = db.get_user(uid)
        uname = f"@{user['username']}" if user and user.get("username") else str(uid)

        rows = get_user_activity(uid, date=filter_date)
        if not rows:
            return await update.message.reply_text(
                f"📭 Tidak ada aktivitas untuk <code>{uid}</code> pada <b>{filter_date}</b>.\n\n"
                f"<i>Catatan: Fitur activity log mulai mencatat sejak bot di-deploy ulang hari ini. "
                f"Aktivitas sebelum itu tidak tersimpan.</i>",
                parse_mode=ParseMode.HTML,
            )

        _EVENT_ICON = {
            "download":       "📥",
            "download_bulk":  "📦",
            "premium_granted":"💎",
            "premium_expired":"⌛",
            "premium_removed":"🗑",
        }
        lines = [f"📋 <b>Aktivitas {uname}</b> — <i>{filter_date}</i>\n{'─' * 30}"]
        for r in rows:
            icon  = _EVENT_ICON.get(r["event_type"], "•")
            waktu = r["created_at"][11:16]  # HH:MM
            detail = f" — <code>{r['detail']}</code>" if r["detail"] else ""
            lines.append(f"{icon} <b>{waktu}</b> {r['event_type']}{detail}")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    # ── /recentactivity [YYYY-MM-DD] ─────────────────────────────────────
    @admin_only
    async def recent_activity(update, context):
        filter_date = context.args[0] if context.args else str(date.today())
        rows = get_recent_activity(date=filter_date, limit=30)
        if not rows:
            return await update.message.reply_text(
                f"📭 Tidak ada aktivitas pada <b>{filter_date}</b>.\n\n"
                f"<i>Catatan: Fitur activity log mulai mencatat sejak bot di-deploy ulang hari ini. "
                f"Aktivitas sebelum itu tidak tersimpan.</i>",
                parse_mode=ParseMode.HTML,
            )

        _EVENT_ICON = {
            "download":       "📥",
            "download_bulk":  "📦",
            "premium_granted":"💎",
            "premium_expired":"⌛",
            "premium_removed":"🗑",
        }
        lines = [f"📋 <b>Aktivitas Terbaru</b> — <i>{filter_date}</i>\n{'─' * 30}"]
        for r in rows:
            icon   = _EVENT_ICON.get(r["event_type"], "•")
            waktu  = r["created_at"][11:16]
            uname  = f"@{r['username']}" if r.get("username") else str(r["user_id"])
            detail = f" <code>{r['detail']}</code>" if r["detail"] else ""
            lines.append(f"{icon} <b>{waktu}</b> {uname} — {r['event_type']}{detail}")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    # ── /topdownloaders [YYYY-MM-DD] ─────────────────────────────────────
    @admin_only
    async def top_downloaders(update, context):
        filter_date = context.args[0] if context.args else str(date.today())
        rows = get_top_downloaders(date=filter_date, limit=10)
        if not rows:
            return await update.message.reply_text(
                f"📭 Belum ada aktivitas download pada <b>{filter_date}</b>.\n\n"
                f"<i>Catatan: Fitur activity log mulai mencatat sejak bot di-deploy ulang hari ini. "
                f"Aktivitas sebelum itu tidak tersimpan.</i>",
                parse_mode=ParseMode.HTML,
            )

        lines = [f"🏆 <b>Top Downloader</b> — <i>{filter_date}</i>\n{'─' * 30}"]
        for i, r in enumerate(rows, 1):
            uname = f"@{r['username']}" if r.get("username") else str(r["user_id"])
            medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
            lines.append(f"{medal} {uname} — <b>{r['total']} download</b>")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    app.add_handler(CommandHandler("activity",        activity))
    app.add_handler(CommandHandler("recentactivity",  recent_activity))
    app.add_handler(CommandHandler("topdownloaders",  top_downloaders))
    app.add_handler(CommandHandler("bulkaddquota",    bulk_add_quota))
    app.add_handler(CommandHandler("bulkpremium",   bulk_premium))
    app.add_handler(CommandHandler("addpremium",    add_premium))
    app.add_handler(CommandHandler("removepremium", rm_premium))
    app.add_handler(CommandHandler("addquota",      add_quota))
    app.add_handler(CommandHandler("userinfo",      user_info))
    app.add_handler(CommandHandler("stats",         stats))
    app.add_handler(CommandHandler("loginlist",     login_list))
    app.add_handler(CommandHandler("ban",           ban_user))
    app.add_handler(CommandHandler("unban",         unban_user))
    app.add_handler(CommandHandler("broadcast",     broadcast))
