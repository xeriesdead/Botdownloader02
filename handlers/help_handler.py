from telegram.ext import CommandHandler
from telegram.constants import ParseMode

from config import REQUIRED_CHANNEL, MAX_FILE_SIZE_MB
from modules.channel_guard import require_member


def setup(app):
    async def help_cmd(update, context):
        if not await require_member(context.bot, update):
            return

        await update.message.reply_text(
            "📖 <b>Panduan Penggunaan Bot</b>\n"
            f"{'─' * 30}\n"
            "\n<b>Setup Awal</b>\n"
            "• /login — hubungkan akun via OTP\n"
            "• /logout — putuskan akun\n"
            "• /cancel — batalkan proses login\n\n"
            "<b>Download Media</b>\n"
            "• <code>/info &lt;link&gt;</code> — lihat metadata sebelum download\n"
            "  <i>(tipe, ukuran, durasi, resolusi, caption, dll)</i>\n\n"
            "• <code>/get &lt;link&gt;</code> — download 1 media / album\n"
            "  <code>→ /get https://t.me/channel/123</code>\n"
            "  <code>→ /get https://t.me/c/1234567890/123</code>\n\n"
            "• <code>/get &lt;link_awal&gt; &lt;link_akhir&gt;</code> — download banyak pesan\n"
            "  <code>→ /get https://t.me/ch/10 https://t.me/ch/20</code>\n"
            "  <i>(maks 50 pesan per request)</i>\n\n"
            "• /canceldownload — batalkan download yang sedang berjalan\n\n"
            "<b>Akun &amp; Quota</b>\n"
            "• /status — lihat quota &amp; status\n"
            "• /referral — dapat +3 bonus quota per referral\n\n"
            "<b>Premium</b>\n"
            "• /pay — info upgrade ke Premium (quota Unlimited)\n\n"
            f"{'─' * 30}\n"
            f"📦 Batas ukuran file: <b>{MAX_FILE_SIZE_MB} MB</b>\n"
            "✅ Mendukung channel &amp; grup public dan private\n"
            "<i>(Akun harus sudah bergabung ke channel/grup private)</i>",
            parse_mode=ParseMode.HTML,
        )

    app.add_handler(CommandHandler("help", help_cmd))
