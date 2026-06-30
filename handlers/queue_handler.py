from telegram.ext import CommandHandler
from telegram.constants import ParseMode

from modules.queue_manager import queue_manager, WORKER_COUNT
from modules.quota_service import QuotaService
from modules.channel_guard import require_member


def setup(app):
    async def queue_cmd(update, context):
        if not await require_member(context.bot, update):
            return

        uid   = update.effective_user.id
        s     = queue_manager.size
        act   = queue_manager.active
        reg   = s["regular"]
        prem  = s["premium"]
        total = reg + prem + act

        worker_bar = "🟢" * act + "⬜" * (WORKER_COUNT - act)

        if total == 0:
            status_line = "✅ <b>Server sedang kosong</b> — kamu akan langsung diproses!"
        elif act == WORKER_COUNT:
            status_line = "⚡ <b>Server penuh</b> — download baru masuk antrian."
        else:
            status_line = "🔄 <b>Server aktif</b> — download berjalan lancar."

        is_prem    = QuotaService.is_premium(uid)
        my_queue   = prem if is_prem else reg
        your_label = "💎 Antrian premium" if is_prem else "📋 Antrian reguler"

        lines = [
            f"📊 <b>Status Server Download</b>",
            f"{'─' * 28}",
            f"",
            f"{status_line}",
            f"",
            f"⚙️ Workers : {worker_bar}  <code>{act}/{WORKER_COUNT}</code> aktif",
            f"💎 Premium : <code>{prem}</code> menunggu",
            f"👤 Reguler : <code>{reg}</code> menunggu",
            f"",
            f"{'─' * 28}",
            f"📌 {your_label}: <b>{my_queue} job</b> dalam antrian",
        ]

        if my_queue == 0 and act < WORKER_COUNT:
            lines.append("🚀 <i>Downloadmu akan langsung diproses!</i>")
        elif my_queue == 0 and act == WORKER_COUNT:
            lines.append("⏳ <i>Semua worker sibuk, tapi giliran kamu dekat.</i>")
        else:
            lines.append(f"⏳ <i>Estimasi posisi: ke-{my_queue + 1} dalam antrian.</i>")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )

    app.add_handler(CommandHandler("queue", queue_cmd))
