from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden
from config import REQUIRED_CHANNEL
from logger import logger


def _channel_display() -> str:
    if not REQUIRED_CHANNEL:
        return ""
    return REQUIRED_CHANNEL if REQUIRED_CHANNEL.startswith("@") else f"@{REQUIRED_CHANNEL}"


def _channel_url() -> str | None:
    if not REQUIRED_CHANNEL:
        return None
    ch = REQUIRED_CHANNEL.lstrip("@")
    if ch.lstrip("-").isdigit():
        return None
    return f"https://t.me/{ch}"


def _channel_id() -> str:
    """
    Normalisasi REQUIRED_CHANNEL untuk dipakai di Telegram API:
    - Jika angka (ID numerik): langsung pakai
    - Jika username tanpa @: tambahkan @
    """
    if not REQUIRED_CHANNEL:
        return ""
    ch = REQUIRED_CHANNEL.strip()
    if ch.lstrip("-").isdigit():
        return ch
    return ch if ch.startswith("@") else f"@{ch}"


async def is_member(bot: Bot, user_id: int) -> bool:
    """
    Cek apakah user sudah join REQUIRED_CHANNEL.
    Mengembalikan True jika channel tidak diset.
    Mengembalikan False jika pengecekan gagal (fail-closed) agar akses tetap diblokir.
    """
    if not REQUIRED_CHANNEL:
        return True
    channel = _channel_id()
    try:
        from telegram import ChatMemberLeft, ChatMemberBanned
        member = await bot.get_chat_member(channel, user_id)
        return not isinstance(member, (ChatMemberLeft, ChatMemberBanned))
    except (BadRequest, Forbidden) as e:
        logger.error(
            f"channel_guard: GAGAL cek channel '{REQUIRED_CHANNEL}': {e}\n"
            "⚠️  SOLUSI: Jadikan bot sebagai ADMIN di channel tersebut "
            "(minimal hak 'Add Members' / 'Invite Users')."
        )
        return False
    except Exception as e:
        logger.error(f"channel_guard: error tak terduga saat cek '{REQUIRED_CHANNEL}': {e}")
        return False


def join_keyboard() -> InlineKeyboardMarkup | None:
    """Buat keyboard inline dengan tombol Join Channel. None jika URL tidak tersedia."""
    url = _channel_url()
    if not url:
        return None
    ch = _channel_display()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📢 Join {ch}", url=url)],
    ])


async def require_member(bot: Bot, update) -> bool:
    """
    Cek membership. Jika belum join (atau pengecekan gagal), kirim peringatan
    dengan tombol join dan return False.
    Gunakan di awal setiap handler yang perlu proteksi.
    """
    if not REQUIRED_CHANNEL:
        return True

    uid = update.effective_user.id
    if await is_member(bot, uid):
        return True

    ch       = _channel_display()
    keyboard = join_keyboard()

    await update.message.reply_text(
        "🔒 <b>Akses Terbatas</b>\n\n"
        f"Kamu wajib join channel <b>{ch}</b> terlebih dahulu sebelum menggunakan bot ini.\n\n"
        "Setelah join, kirim perintah kembali. ✅",
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    return False
