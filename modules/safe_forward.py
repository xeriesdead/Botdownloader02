import asyncio
import os
import random
import shutil
import subprocess
import tempfile
import time
from pyrogram.errors import (
    FloodWait,
    ChannelPrivate,
    ChannelInvalid,
    ChatForbidden,
    ChatIdInvalid,
    ChatInvalid,
    UsernameNotOccupied,
    UsernameInvalid,
    PeerIdInvalid,
    UserNotParticipant,
    MessageIdInvalid,
    MsgIdInvalid,
    ChatForwardsRestricted,
    FileReferenceExpired,
)
from pyrogram import raw
from pyrogram.types import Message
from telegram import (
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.error import BadRequest, Forbidden
from config import (
    MAX_FILE_SIZE_BYTES, MAX_FILE_SIZE_MB,
    MAX_FILE_SIZE_BYTES_PREMIUM, MAX_FILE_SIZE_MB_PREMIUM,
)
from logger import logger

MAX_RETRIES = 2
FLOOD_LIMIT = 60

# Batas upload ulang via Bot API (file di atas ini tidak bisa di-re-upload oleh bot)
_BOT_API_UPLOAD_LIMIT = 50 * 1024 * 1024  # 50 MB

# Username bot â diset sekali saat startup via set_bot_username()
_BOT_USERNAME: str = ""


def set_bot_username(username: str):
    global _BOT_USERNAME
    _BOT_USERNAME = username


def _build_caption(original: str) -> str:
    """Tambahkan watermark bot ke caption asli."""
    tag = f"@{_BOT_USERNAME}" if _BOT_USERNAME else "Bot Downloader"
    watermark = f"By ({tag})"
    if original:
        return f"{original}\n\n{watermark}"
    return watermark

_PEER_ERRORS = (
    ChannelPrivate, ChannelInvalid, ChatForbidden,
    ChatIdInvalid, ChatInvalid, UserNotParticipant, PeerIdInvalid,
)


# Cache hasil deteksi noforwards per chat agar tidak dipanggil ulang setiap pesan
_forwards_restricted_cache: dict[str, bool] = {}

# Minimum ukuran file agar progress bar ditampilkan (10 MB)
_PROGRESS_MIN_BYTES = 10 * 1024 * 1024

# Telegram menerima thumbnail video dalam bentuk JPEG kecil. Gunakan frame
# setelah pembukaan video agar thumbnail tidak sering berupa frame hitam.
_THUMBNAIL_MAX_SECONDS = 5.0
_THUMBNAIL_MAX_BYTES = 200 * 1024


# ââ Helpers ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def _progress_bar(pct: int, width: int = 10) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _fmt_eta(seconds: float) -> str:
    """Format detik menjadi teks ETA singkat dalam Bahasa Indonesia."""
    s = int(seconds)
    if s < 5:
        return "sebentar lagi"
    if s < 60:
        return f"~{s} detik"
    if s < 3600:
        m = round(s / 60)
        return f"~{m} menit"
    h = s / 3600
    return f"~{h:.1f} jam"


def _fmt_speed(bps: float) -> str:
    """Format bytes/detik menjadi string kecepatan yang mudah dibaca."""
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 * 1024:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps / 1024 / 1024:.1f} MB/s"


def _make_pyrogram_progress(on_progress, phase: str, total_size: int):
    """
    Buat callback progress Pyrogram (signature: current, total).
    on_progress: async callable(text: str) â fungsi untuk update pesan status.
    Debounce: update maks 1x per 3 detik ATAU tiap lompatan 10%.
    Menampilkan: bar, persentase, ukuran, kecepatan, dan estimasi waktu selesai (ETA).
    """
    state = {
        "last_time": 0.0,
        "last_pct": -1,
        "last_current": -1,
        "start_time": 0.0,   # waktu byte pertama diterima
        "started": False,
    }

    async def _cb(current: int, total: int):
        if total <= 0:
            return
        now = time.monotonic()

        # Catat waktu mulai saat callback pertama kali dipanggil
        if not state["started"]:
            state["started"] = True
            state["start_time"] = now

        pct = int(current * 100 / total)
        if (
            current == state["last_current"]
            or (now - state["last_time"] < 3.0 and pct - state["last_pct"] < 10)
        ):
            return
        state["last_time"] = now
        state["last_pct"] = pct
        state["last_current"] = current

        # Hitung kecepatan rata-rata dan ETA
        elapsed = now - state["start_time"]
        speed_bps = current / elapsed if elapsed > 0.5 else 0.0
        remaining = total - current
        eta_str = _fmt_eta(remaining / speed_bps) if speed_bps > 0 else ""
        speed_str = _fmt_speed(speed_bps) if speed_bps > 0 else ""

        bar = _progress_bar(pct)
        size_str = _fmt_size(total_size) if total_size else _fmt_size(total)

        # Baris info: ukuran â¢ kecepatan â¢ ETA (tampilkan hanya jika tersedia)
        info_parts = [f"<b>{_fmt_size(current)}</b> / {size_str}"]
        if speed_str:
            info_parts.append(speed_str)
        if eta_str:
            info_parts.append(f"⏱ {eta_str}")
        info_line = " • ".join(info_parts)

        text = (
            f"⏳ <b>{phase}...</b>\n"
            f"<code>[{bar}]</code> {pct}%\n"
            f"{info_line}"
        )
        try:
            # Progress hanya informasi tambahan. Jika Telegram lambat saat
            # mengedit pesan status, transfer Pyrogram tetap berjalan.
            await asyncio.wait_for(
                on_progress(text),
                timeout=_PROGRESS_CALLBACK_TIMEOUT,
            )
        except Exception:
            pass

    return _cb

_PEER_RESOLVE_TIMEOUT  = 20   # detik â batas waktu resolve peer & get_chat
_MSG_FETCH_TIMEOUT     = 25   # detik â batas waktu get_messages
_ACCESS_CHECK_TIMEOUT  = 12   # detik â batas waktu pre-flight cek akses channel
_DOWNLOAD_TIMEOUT      = 120  # detik â timeout dasar download file kecil
_DOWNLOAD_STALL_TIMEOUT = 45  # detik tanpa byte baru sebelum transfer dibatalkan
_UPLOAD_TIMEOUT        = 300  # detik â timeout dasar upload file kecil
_LARGE_TRANSFER_TIMEOUT_MAX = 2 * 60 * 60  # transfer Premium besar maksimal 2 jam
_ALBUM_FETCH_TIMEOUT   = 30   # detik â batas waktu mengambil metadata album
_ALBUM_UPLOAD_TIMEOUT_PER_FILE = 120  # detik per file â dipakai di _send_album_via_bot
_ALBUM_UPLOAD_TIMEOUT_MAX = 480  # jangan biarkan upload group melewati timeout job regular
_BOT_COPY_TIMEOUT      = 30   # detik â jalur cepat untuk pesan channel publik
_PROGRESS_CALLBACK_TIMEOUT = 5  # update status tidak boleh menahan transfer
_TRANSFER_POLL_INTERVAL = 2  # detik â frekuensi pemeriksaan watchdog transfer

# Timeout PTB untuk operasi upload ke Bot API
_PTB_WRITE_TIMEOUT   = 90    # detik
_PTB_READ_TIMEOUT    = 60    # detik
_PTB_CONNECT_TIMEOUT = 15    # detik


def _media_transfer_timeout(file_size: int | None, base_timeout: int) -> int:
    """Beri waktu proporsional untuk transfer media besar tanpa mengubah file kecil."""
    if not file_size:
        return base_timeout
    size_mb = file_size / (1024 * 1024)
    return max(
        base_timeout,
        min(_LARGE_TRANSFER_TIMEOUT_MAX, int(size_mb * 1.5) + 30),
    )


def _consume_cancelled_task(task: asyncio.Task):
    """Konsumsi hasil task yang dibatalkan agar tidak menghasilkan warning."""
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        pass


async def _hard_timeout(awaitable, timeout: float, operation: str):
    """
    Timeout yang tidak menunggu coroutine Pyrogram selesai dibatalkan.

    Beberapa operasi Pyrogram dapat menahan pembatalan saat koneksi MTProto
    macet. `asyncio.wait_for()` ikut menunggu proses pembatalan tersebut,
    sehingga worker terlihat stuck. Dengan `asyncio.wait()`, worker kembali
    tepat setelah batas waktu dan task yang macet dibersihkan saat selesai.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except BaseException:
        if not task.done():
            task.cancel()
            task.add_done_callback(_consume_cancelled_task)
        raise

    if task in done:
        return task.result()

    logger.warning("%s timeout setelah %ss", operation, timeout)
    task.cancel()
    task.add_done_callback(_consume_cancelled_task)
    raise asyncio.TimeoutError(f"{operation} timeout")


def _new_download_dir(user_chat_id: int) -> str:
    """Buat direktori sementara yang bisa dihapus utuh setelah satu job."""
    os.makedirs("downloads", exist_ok=True)
    return tempfile.mkdtemp(
        prefix=f"telegram_{int(user_chat_id)}_",
        dir="downloads",
    )


async def _notify_progress(on_progress, text: str):
    """Kirim status fase tanpa membuat download gagal jika Telegram sedang timeout."""
    if not on_progress:
        return
    try:
        await asyncio.wait_for(
            on_progress(text),
            timeout=_PROGRESS_CALLBACK_TIMEOUT,
        )
    except Exception:
        pass


async def _send_media_group_with_watchdog(
    bot,
    user_chat_id: int,
    batch: list[tuple],
    on_progress,
    timeout: int,
):
    """
    Upload satu media group tanpa membiarkan status user diam selamanya.

    PTB melakukan upload beberapa file dalam satu request, sehingga callback
    progress Pyrogram tidak tersedia di fase ini. Task dipantau terpisah agar
    status tetap hidup dan request yang macet dibatalkan sebelum timeout job.
    """
    task = asyncio.ensure_future(
        bot.send_media_group(
            user_chat_id,
            media=[item[2] for item in batch],
            write_timeout=_PTB_WRITE_TIMEOUT,
            read_timeout=_PTB_READ_TIMEOUT,
            connect_timeout=_PTB_CONNECT_TIMEOUT,
        )
    )
    started_at = time.monotonic()
    try:
        while True:
            remaining = timeout - (time.monotonic() - started_at)
            if remaining <= 0:
                logger.warning(
                    "Album upload timeout setelah %ss (%s media)",
                    timeout,
                    len(batch),
                )
                task.cancel()
                task.add_done_callback(_consume_cancelled_task)
                raise asyncio.TimeoutError(
                    f"album upload timeout setelah {timeout} detik"
                )

            done, _ = await asyncio.wait(
                {task},
                timeout=min(15, remaining),
            )
            if done:
                return task.result()

            elapsed = int(time.monotonic() - started_at)
            await _notify_progress(
                on_progress,
                f"📤 <b>Mengirim album...</b> ({len(batch)} media)\n"
                f"<i>Upload masih berjalan ({elapsed} detik)...</i>",
            )
    except BaseException:
        if not task.done():
            task.cancel()
            task.add_done_callback(_consume_cancelled_task)
        raise


async def _run_transfer_with_watchdog(
    factory,
    timeout: int,
    operation: str,
    progress=None,
):
    """
    Jalankan transfer Pyrogram dengan timeout total dan timeout saat byte tidak
    bertambah.

    Pyrogram dapat memanggil callback berulang kali dengan posisi byte yang
    sama ketika sedang retry pada chunk yang gagal. Callback saja tidak cukup
    sebagai tanda koneksi masih hidup; watchdog hanya di-reset jika posisi
    transfer benar-benar berubah.
    """
    last_progress_at = time.monotonic()
    last_position = None

    async def _progress(current: int, total: int):
        nonlocal last_progress_at, last_position
        if current != last_position:
            last_position = current
            last_progress_at = time.monotonic()
        if progress:
            try:
                await progress(current, total)
            except Exception:
                pass

    task = asyncio.ensure_future(factory(_progress))
    started_at = time.monotonic()
    try:
        while not task.done():
            await asyncio.sleep(_TRANSFER_POLL_INTERVAL)
            now = time.monotonic()
            if now - last_progress_at >= _DOWNLOAD_STALL_TIMEOUT:
                logger.warning(
                    "%s stalled: no byte progress for %ss (position=%s)",
                    operation,
                    _DOWNLOAD_STALL_TIMEOUT,
                    last_position,
                )
                task.cancel()
                task.add_done_callback(_consume_cancelled_task)
                raise asyncio.TimeoutError(
                    f"{operation} stalled after {_DOWNLOAD_STALL_TIMEOUT}s"
                )
            if now - started_at >= timeout:
                logger.warning("%s timeout setelah %ss", operation, timeout)
                task.cancel()
                task.add_done_callback(_consume_cancelled_task)
                raise asyncio.TimeoutError(f"{operation} timeout")
        return task.result()
    except BaseException:
        if not task.done():
            task.cancel()
            task.add_done_callback(_consume_cancelled_task)
        raise


async def _download_media(
    client,
    media,
    file_name: str,
    timeout: int,
    operation: str,
    progress=None,
):
    """Download media via Pyrogram dengan watchdog transfer yang nyata."""
    downloaded = await _run_transfer_with_watchdog(
        lambda transfer_progress: client.download_media(
            media,
            file_name=file_name,
            progress=transfer_progress,
        ),
        timeout=timeout,
        operation=operation,
        progress=progress,
    )
    if not downloaded:
        return downloaded

    # Pyrogram dapat mengembalikan folder tujuan ketika `file_name` berupa
    # direktori. Telegram Bot API membutuhkan path file aktual, bukan folder.
    if os.path.isfile(downloaded):
        return downloaded

    search_root = downloaded if os.path.isdir(downloaded) else file_name
    if not os.path.isdir(search_root):
        return downloaded

    candidates = []
    for root, _, names in os.walk(search_root):
        for name in names:
            candidate = os.path.join(root, name)
            if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                candidates.append(candidate)

    if not candidates:
        return downloaded

    # Satu media biasanya menghasilkan satu file. Jika ada metadata tambahan,
    # pilih file media terbesar agar folder tetap aman dipakai sebagai target.
    return max(candidates, key=os.path.getsize)


async def copy_public_message(
    bot, user_chat_id: int, chat, msg_id: int, on_progress=None,
) -> bool:
    """Pindahkan pesan publik lewat Bot API tanpa mengunduh media ke Railway."""
    if not isinstance(chat, str) or not chat.startswith("@"):
        return False

    await _notify_progress(
        on_progress, "📤 <b>Menyalin pesan dari channel publik...</b>"
    )
    try:
        await asyncio.wait_for(
            bot.copy_message(
                chat_id=user_chat_id,
                from_chat_id=chat,
                message_id=msg_id,
                write_timeout=_PTB_WRITE_TIMEOUT,
                read_timeout=_PTB_READ_TIMEOUT,
                connect_timeout=_PTB_CONNECT_TIMEOUT,
            ),
            timeout=_BOT_COPY_TIMEOUT,
        )
        return True
    except asyncio.TimeoutError:
        # Jangan langsung mencoba metode kedua setelah timeout: Telegram
        # mungkin sudah menerima copy request dan retry dapat membuat duplikat.
        logger.warning("Timeout copy_message(%s, %s)", chat, msg_id)
        return False
    except (BadRequest, Forbidden) as exc:
        logger.info(
            "copy_message(%s, %s) tidak tersedia: %s",
            chat, msg_id, exc,
        )
    except Exception as exc:
        logger.warning(
            "copy_message(%s, %s) gagal: %s",
            chat, msg_id, exc,
        )

    # Beberapa pesan/media publik ditolak oleh copyMessage tetapi masih bisa
    # diteruskan lewat forwardMessage. Ini juga tidak memakai download lokal.
    await _notify_progress(
        on_progress, "📤 <b>Meneruskan media besar tanpa download ulang...</b>"
    )
    try:
        await asyncio.wait_for(
            bot.forward_message(
                chat_id=user_chat_id,
                from_chat_id=chat,
                message_id=msg_id,
                write_timeout=_PTB_WRITE_TIMEOUT,
                read_timeout=_PTB_READ_TIMEOUT,
                connect_timeout=_PTB_CONNECT_TIMEOUT,
            ),
            timeout=_BOT_COPY_TIMEOUT,
        )
        return True
    except asyncio.TimeoutError:
        logger.warning("Timeout forward_message(%s, %s)", chat, msg_id)
    except (BadRequest, Forbidden) as exc:
        logger.info(
            "forward_message(%s, %s) tidak tersedia, gunakan fallback: %s",
            chat, msg_id, exc,
        )
    except Exception as exc:
        logger.warning(
            "forward_message(%s, %s) gagal, gunakan fallback: %s",
            chat, msg_id, exc,
        )
    return False


async def check_channel_access(client, chat) -> tuple[bool, str]:
    """
    Pre-flight: cek apakah client bisa mengakses channel/grup.
    Dipanggil SEBELUM quota dipotong agar user tidak kehilangan quota
    jika akun belum bergabung ke channel target.

    Return (True, "") jika bisa diakses, (False, pesan_error) jika tidak.
    """
    label = f"ID {chat}" if isinstance(chat, int) else str(chat)
    try:
        await _hard_timeout(
            client.get_chat(chat),
            timeout=_ACCESS_CHECK_TIMEOUT,
            operation=f"get_chat({chat})",
        )
        return True, ""
    except asyncio.TimeoutError:
        return False, (
            "⏳ <b>Tidak bisa memeriksa channel (timeout).</b>\n"
            "Pastikan akun sudah bergabung, lalu coba lagi."
        )
    except _PEER_ERRORS:
        return False, (
            "🔒 <b>Akses ditolak.</b>\n\n"
            f"Akun kamu belum bergabung ke channel <code>{label}</code>.\n"
            "Silakan join channel tersebut terlebih dahulu, lalu coba lagi."
        )
    except (UsernameNotOccupied, UsernameInvalid):
        return False, f"❌ Channel <code>{label}</code> tidak ditemukan atau sudah tidak aktif."
    except Exception as e:
        logger.warning(f"check_channel_access({chat}): {e}")
        # Jika cek gagal karena alasan lain (misal network), biarkan lanjut â
        # error yang lebih spesifik akan muncul saat proses download.
        return True, ""


async def _is_forwards_restricted(client, chat) -> bool:
    """
    Cek apakah channel/grup mengaktifkan 'Restrict Saving Content' (noforwards).
    Hasil di-cache per chat agar efisien saat bulk download.
    Return True jika forward dibatasi, False jika tidak (atau tidak bisa cek).
    """
    cache_key = str(chat)
    if cache_key in _forwards_restricted_cache:
        return _forwards_restricted_cache[cache_key]
    try:
        chat_obj = await _hard_timeout(
            client.get_chat(chat),
            timeout=_PEER_RESOLVE_TIMEOUT,
            operation=f"get_chat({chat})",
        )
        restricted = bool(getattr(chat_obj, "has_protected_content", False))
        _forwards_restricted_cache[cache_key] = restricted
        if restricted:
            logger.info(f"Chat {chat} memiliki noforwards aktif â pakai strategi download+upload")
        return restricted
    except asyncio.TimeoutError:
        logger.warning(f"Timeout get_chat({chat}) saat cek noforwards â anggap tidak restricted")
        return False
    except Exception as e:
        logger.warning(f"Gagal cek has_protected_content untuk {chat}: {e}")
        return False


async def _resolve_source(client, chat) -> tuple[object | None, str | None]:
    """Resolve source chat. Return a stable numeric chat ID when available."""
    label = chat if isinstance(chat, str) else f"ID {chat}"
    try:
        chat_obj = await _hard_timeout(
            client.get_chat(chat),
            timeout=_PEER_RESOLVE_TIMEOUT,
            operation=f"get_chat({chat})",
        )
        # get_messages() with a username can trigger a second username lookup
        # in Pyrogram. Reuse Telegram's numeric ID to avoid that network path.
        stable_chat = getattr(chat_obj, "id", None) or chat
        return stable_chat, None
    except asyncio.TimeoutError:
        logger.warning(f"Timeout get_chat({chat})")
        return None, (
            f"❌ Tidak bisa mengakses channel (timeout).\n"
            "Pastikan akun sudah bergabung ke channel tersebut."
        )
    except (UsernameNotOccupied, UsernameInvalid):
        return None, f"Channel/grup `{label}` tidak ditemukan atau sudah tidak aktif."
    except _PEER_ERRORS:
        return None, (
            f"❌ Tidak bisa mengakses channel.\n"
            "Pastikan akun yang login sudah bergabung ke channel/grup tersebut."
        )
    except Exception as e:
        logger.warning(f"get_chat({chat}) error: {e}")
        return None, f"Gagal mengakses channel: {e}"


async def _get_message_via_raw_api(client, chat, msg_id: int):
    """
    Ambil satu pesan lewat raw MTProto API.

    Pada beberapa koneksi/channel, wrapper Pyrogram get_messages() dapat
    berhenti setelah peer berhasil di-resolve. Raw channels.getMessages /
    messages.getMessages menghindari lookup wrapper tersebut.
    """
    peer = await _hard_timeout(
        client.resolve_peer(chat),
        timeout=_PEER_RESOLVE_TIMEOUT,
        operation=f"resolve_peer({chat})",
    )
    message_id = raw.types.InputMessageID(id=msg_id)

    if isinstance(peer, raw.types.InputPeerChannel):
        # channels.getMessages expects InputChannel, not InputPeerChannel.
        # Both carry the same channel identifiers, but Telegram treats them
        # as different MTProto constructors.
        channel = raw.types.InputChannel(
            channel_id=peer.channel_id,
            access_hash=peer.access_hash,
        )
        result = await _hard_timeout(
            client.invoke(
                raw.functions.channels.GetMessages(
                    channel=channel,
                    id=[message_id],
                )
            ),
            timeout=_MSG_FETCH_TIMEOUT,
            operation=f"channels.getMessages({chat}, {msg_id})",
        )
    else:
        result = await _hard_timeout(
            client.invoke(
                raw.functions.messages.GetMessages(
                    id=[message_id],
                )
            ),
            timeout=_MSG_FETCH_TIMEOUT,
            operation=f"messages.getMessages({chat}, {msg_id})",
        )

    messages = getattr(result, "messages", None) or []
    if not messages:
        return None

    parsed = Message._parse(
        client,
        messages[0],
        getattr(result, "users", None) or [],
        getattr(result, "chats", None) or [],
    )
    if hasattr(parsed, "__await__"):
        parsed = await parsed
    return parsed


async def _get_message(client, chat, msg_id: int):
    """Ambil pesan dengan wrapper Pyrogram lalu fallback ke raw API."""
    try:
        message = await _hard_timeout(
            client.get_messages(chat, msg_id),
            timeout=_MSG_FETCH_TIMEOUT,
            operation=f"get_messages({chat}, {msg_id})",
        )
        if message is not None:
            return message
        logger.warning(
            "Wrapper get message mengembalikan pesan kosong untuk %s/%s, "
            "coba raw API",
            chat, msg_id,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Wrapper get message timeout untuk %s/%s, coba raw API",
            chat, msg_id,
        )
    except (MessageIdInvalid, MsgIdInvalid):
        raise
    except Exception as wrapper_error:
        logger.warning(
            "Wrapper get message gagal untuk %s/%s, coba raw API: %s",
            chat, msg_id, wrapper_error,
        )

    try:
        return await _get_message_via_raw_api(client, chat, msg_id)
    except asyncio.TimeoutError:
        raise
    except (MessageIdInvalid, MsgIdInvalid):
        raise
    except _PEER_ERRORS as raw_error:
        logger.warning(
            "Raw get message menolak peer %s/%s setelah wrapper gagal: %s",
            chat, msg_id, raw_error,
        )
        raise
    except Exception as raw_error:
        logger.warning(
            "Raw get message gagal untuk %s/%s setelah wrapper gagal: %s",
            chat, msg_id, raw_error,
        )
        raise


async def inspect_message_media_size(
    client, chat, msg_id: int,
) -> tuple[bool, int | None, bool]:
    """
    Baca metadata pesan tanpa mengunduh media.

    Return (has_media, largest_file_size, is_album). Dipakai sebagai pre-flight
    agar file yang melewati batas user ditolak sebelum quota dipotong atau
    masuk antrian. Untuk album, ukuran terbesar dihitung dari seluruh media.
    """
    source_chat, source_error = await _resolve_source(client, chat)
    if source_error:
        raise RuntimeError(source_error)

    message = await _get_message(client, source_chat, msg_id)
    if not message or message.empty:
        raise RuntimeError(f"Pesan `{msg_id}` kosong atau sudah dihapus.")

    is_album = bool(getattr(message, "media_group_id", None))
    messages = [message]
    if is_album:
        messages = await _fetch_album_messages(client, source_chat, msg_id)
        if not messages:
            raise RuntimeError(f"Album pesan `{msg_id}` kosong atau sudah dihapus.")

    media_messages = [
        item for item in messages
        if getattr(item, "media", None)
    ]
    sizes = [
        size for size in (_get_file_size(item) for item in media_messages)
        if size is not None
    ]
    return bool(media_messages), max(sizes, default=None), is_album
def _get_file_size(msg) -> int | None:
    """Ambil ukuran file dari pesan, atau None jika tidak ada media."""
    for attr in ("document", "video", "audio", "voice", "video_note", "sticker", "animation"):
        media = getattr(msg, attr, None)
        if media and hasattr(media, "file_size"):
            return media.file_size
    photo = getattr(msg, "photo", None)
    if photo and hasattr(photo, "file_size"):
        return photo.file_size
    return None


def _album_download_target(msg, user_chat_id: int, album_msg_id: int,
                           item_index: int) -> str:
    """Buat nama file unik agar item album tidak saling menimpa."""
    if msg.photo:
        extension = ".jpg"
    elif msg.video or msg.animation or msg.video_note:
        extension = ".mp4"
    elif msg.audio:
        extension = ".mp3"
    elif msg.voice:
        extension = ".ogg"
    elif msg.sticker:
        extension = ".webp"
    else:
        document_name = getattr(getattr(msg, "document", None), "file_name", "")
        extension = os.path.splitext(document_name or "")[1][:10] or ".bin"

    os.makedirs("downloads", exist_ok=True)
    return os.path.join(
        "downloads",
        f"album_{user_chat_id}_{album_msg_id}_{item_index}_{msg.id}{extension}",
    )


def _fmt_size(size_bytes: int) -> str:
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"


def _probe_video_timestamp(path: str) -> float:
    """Pilih timestamp thumbnail yang aman, termasuk untuk video pendek."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        duration = float(result.stdout.strip())
        if duration > 0:
            return min(max(duration * 0.15, 0.5), _THUMBNAIL_MAX_SECONDS)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return 1.0


def _create_video_thumbnail(path: str) -> str | None:
    """
    Buat thumbnail JPEG sementara untuk video.

    Return None jika video tidak dapat dibaca. Kegagalan thumbnail tidak boleh
    menggagalkan upload video utama.
    """
    thumb_fd, thumb_path = tempfile.mkstemp(prefix="bot-thumb-", suffix=".jpg")
    os.close(thumb_fd)
    timestamp = _probe_video_timestamp(path)

    commands = (
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{timestamp:.3f}", "-i", path,
            "-frames:v", "1",
            "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
            "-q:v", "8",
            thumb_path,
        ],
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", path, "-frames:v", "1",
            "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
            "-q:v", "8",
            thumb_path,
        ],
    )

    for command in commands:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=30,
                check=False,
            )
            thumb_size = os.path.getsize(thumb_path)
        except (OSError, subprocess.SubprocessError):
            continue

        if result.returncode == 0 and 0 < thumb_size <= _THUMBNAIL_MAX_BYTES:
            return thumb_path

        if result.returncode == 0 and thumb_size > _THUMBNAIL_MAX_BYTES:
            compact_fd, compact_path = tempfile.mkstemp(
                prefix="bot-thumb-small-", suffix=".jpg"
            )
            os.close(compact_fd)
            try:
                compact_result = subprocess.run(
                    [
                        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", thumb_path, "-frames:v", "1",
                        "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                        "-q:v", "12", compact_path,
                    ],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                compact_size = os.path.getsize(compact_path)
                if (
                    compact_result.returncode == 0
                    and 0 < compact_size <= _THUMBNAIL_MAX_BYTES
                ):
                    os.remove(thumb_path)
                    os.replace(compact_path, thumb_path)
                    return thumb_path
            except (OSError, subprocess.SubprocessError):
                pass
            finally:
                if os.path.exists(compact_path):
                    try:
                        os.remove(compact_path)
                    except OSError:
                        pass

    try:
        os.remove(thumb_path)
    except OSError:
        pass
    return None


async def _create_video_thumbnail_async(path: str) -> str | None:
    """Jalankan FFmpeg di thread agar event loop bot tidak terblokir."""
    return await asyncio.to_thread(_create_video_thumbnail, path)


def _video_metadata(msg) -> dict:
    """Ambil metadata video dari pesan sumber untuk preview Telegram."""
    video = getattr(msg, "video", None)
    if not video:
        return {}
    metadata = {}
    for key in ("duration", "width", "height"):
        value = getattr(video, key, None)
        if value:
            metadata[key] = value
    return metadata


async def _download_and_send_via_bot(client, bot, msg, user_chat_id: int,
                                     on_progress=None):
    """
    Download media via Pyrogram, lalu kirim ke user via PTB bot.
    Menggunakan file object (bukan bytes) agar tidak OOM untuk file besar.
    Hanya aman untuk file â¤50 MB (batas upload Bot API).
    on_progress: async callable(text: str) untuk update pesan status (opsional).
    """
    file_size = _get_file_size(msg) or 0
    dl_cb = (
        _make_pyrogram_progress(on_progress, "Mengunduh", file_size)
        if on_progress and file_size >= _PROGRESS_MIN_BYTES
        else None
    )
    work_dir = _new_download_dir(user_chat_id)
    path = None
    thumbnail_path = None
    try:
        try:
            path = await _download_media(
                client,
                msg,
                file_name=work_dir,
                timeout=_DOWNLOAD_TIMEOUT,
                operation=f"download message {getattr(msg, 'id', '?')}",
                progress=dl_cb,
            )
        except asyncio.TimeoutError:
            raise RuntimeError("Download timeout — file terlalu lama diunduh, coba lagi.")

        if not path:
            raise RuntimeError("Download gagal, file tidak tersedia.")

        await _notify_progress(on_progress, "📤 <b>Mengirim media...</b>")
        caption = _build_caption(msg.caption or "")
        # Buat thumbnail dari frame video setelah download selesai. Jika FFmpeg
        # gagal, upload tetap dilanjutkan tanpa thumbnail.
        thumbnail_path = (
            await _create_video_thumbnail_async(path)
            if msg.video else None
        )
        metadata = _video_metadata(msg)
        _kw = dict(
            write_timeout=_PTB_WRITE_TIMEOUT,
            read_timeout=_PTB_READ_TIMEOUT,
            connect_timeout=_PTB_CONNECT_TIMEOUT,
        )
        if msg.photo:
            with open(path, "rb") as f:
                await asyncio.wait_for(
                    bot.send_photo(user_chat_id, photo=f, caption=caption, **_kw),
                    timeout=_UPLOAD_TIMEOUT,
                )
        elif msg.video:
            with open(path, "rb") as f:
                if thumbnail_path:
                    with open(thumbnail_path, "rb") as thumb:
                        await asyncio.wait_for(
                            bot.send_video(
                                user_chat_id,
                                video=f,
                                caption=caption,
                                thumbnail=thumb,
                                supports_streaming=True,
                                **metadata,
                                **_kw,
                            ),
                            timeout=_UPLOAD_TIMEOUT,
                        )
                else:
                    await asyncio.wait_for(
                        bot.send_video(
                            user_chat_id,
                            video=f,
                            caption=caption,
                            supports_streaming=True,
                            **metadata,
                            **_kw,
                        ),
                        timeout=_UPLOAD_TIMEOUT,
                    )
        elif msg.audio:
            with open(path, "rb") as f:
                await asyncio.wait_for(
                    bot.send_audio(user_chat_id, audio=f, caption=caption, **_kw),
                    timeout=_UPLOAD_TIMEOUT,
                )
        elif msg.voice:
            with open(path, "rb") as f:
                await asyncio.wait_for(
                    bot.send_voice(user_chat_id, voice=f, caption=caption, **_kw),
                    timeout=_UPLOAD_TIMEOUT,
                )
        elif msg.video_note:
            with open(path, "rb") as f:
                await asyncio.wait_for(
                    bot.send_video_note(user_chat_id, video_note=f, **_kw),
                    timeout=_UPLOAD_TIMEOUT,
                )
        elif msg.animation:
            with open(path, "rb") as f:
                await asyncio.wait_for(
                    bot.send_animation(user_chat_id, animation=f, caption=caption, **_kw),
                    timeout=_UPLOAD_TIMEOUT,
                )
        elif msg.sticker:
            with open(path, "rb") as f:
                await asyncio.wait_for(
                    bot.send_sticker(user_chat_id, sticker=f, **_kw),
                    timeout=_UPLOAD_TIMEOUT,
                )
        else:
            with open(path, "rb") as f:
                await asyncio.wait_for(
                    bot.send_document(user_chat_id, document=f, caption=caption, **_kw),
                    timeout=_UPLOAD_TIMEOUT,
                )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            "Upload timeout — koneksi ke Telegram terlalu lambat. Coba lagi."
        ) from exc
    finally:
        if thumbnail_path:
            try:
                os.remove(thumbnail_path)
            except Exception:
                pass
        shutil.rmtree(work_dir, ignore_errors=True)


def _album_media_kind(message) -> str | None:
    """Return the Bot API media-group type supported for this message."""
    if getattr(message, "photo", None) or getattr(message, "video", None):
        return "visual"
    if getattr(message, "audio", None):
        return "audio"
    if getattr(message, "document", None):
        return "document"
    return None


def _album_batches(items: list[tuple]) -> list[list[tuple]]:
    """
    Split downloaded items into Telegram-compatible media groups.

    Photos and videos may share one group. Audio and documents must each stay
    in their own group. Unsupported media types are returned as one-item
    batches so the caller can send them individually.
    """
    batches: list[list[tuple]] = []
    current: list[tuple] = []
    current_kind: str | None = None

    for item in items:
        kind = item[3]
        if kind is None:
            if current:
                batches.append(current)
                current = []
                current_kind = None
            batches.append([item])
            continue

        if current and (kind != current_kind or len(current) >= 10):
            batches.append(current)
            current = []

        current.append(item)
        current_kind = kind

    if current:
        batches.append(current)
    return batches


async def _send_album_via_bot(client, bot, chat, msg_id: int, user_chat_id: int,
                              on_progress=None, is_premium: bool = False,
                              messages=None):
    """
    Download seluruh album via Pyrogram, lalu kirim sebagai media group via PTB bot.
    File object tetap terbuka hingga send_media_group selesai, lalu ditutup & dihapus.
    on_progress: async callable(text: str) untuk update status (opsional).
    """
    if messages is None:
        msgs = await _fetch_album_messages(client, chat, msg_id, on_progress=on_progress)
    else:
        msgs = messages
    total = len(msgs)
    size_limit = MAX_FILE_SIZE_BYTES_PREMIUM if is_premium else MAX_FILE_SIZE_BYTES
    size_label = (
        f"{MAX_FILE_SIZE_MB_PREMIUM} MB (Premium)"
        if is_premium else f"{MAX_FILE_SIZE_MB} MB"
    )
    oversized = [
        _get_file_size(m) for m in msgs
        if _get_file_size(m) and _get_file_size(m) > size_limit
    ]
    if oversized:
        return False, (
            f"Album memiliki media terlalu besar ({_fmt_size(max(oversized))}). "
            f"Batas maksimal: {size_label}."
        )
    paths: list[str]    = []
    download_dirs: list[str] = []
    thumbnail_paths: list[str] = []
    handles: list       = []
    thumbnail_handles: list = []
    downloaded_items: list[tuple] = []

    try:
        for i, m in enumerate(msgs):
            file_size = _get_file_size(m) or 0
            # Timeout dinamis: min 60 detik, +30 detik per 10 MB
            dl_timeout = max(60, 30 + (file_size // (10 * 1024 * 1024)) * 30)
            # Callback progress per-file (hanya untuk file â¥ PROGRESS_MIN_BYTES)
            dl_cb = None
            if on_progress and file_size >= _PROGRESS_MIN_BYTES:
                dl_cb = _make_pyrogram_progress(
                    on_progress,
                    f"Mengunduh ({i + 1}/{total})",
                    file_size,
                )
            elif on_progress:
                await _notify_progress(
                    on_progress,
                    f"📥 <b>Mengunduh album...</b> ({i + 1}/{total})",
                )
            path = None
            item_dir = _new_download_dir(user_chat_id)
            for _dl_attempt in range(2):
                try:
                    path = await _download_media(
                        client,
                        m,
                        file_name=item_dir,
                        timeout=dl_timeout,
                        operation=f"download album item {i + 1}/{total}",
                        progress=dl_cb,
                    )
                    if path and os.path.isfile(path) and os.path.getsize(path) > 0:
                        break
                    path = None
                except (asyncio.TimeoutError, Exception) as _dl_err:
                    logger.warning(
                        f"Download album item {i + 1}/{total} msg {m.id} "
                        f"attempt {_dl_attempt + 1} gagal: {_dl_err}"
                    )
                    if _dl_attempt == 0:
                        await asyncio.sleep(2)
            if not path:
                reason = (
                    f"Album berhenti pada media {i + 1}/{total}: "
                    "download tidak selesai setelah 2 percobaan."
                )
                logger.error("%s msg %s", reason, m.id)
                shutil.rmtree(item_dir, ignore_errors=True)
                return False, reason
            paths.append(path)
            download_dirs.append(item_dir)

            caption = _build_caption(m.caption or "") if i == 0 else ""
            f       = open(path, "rb")  # noqa: WPS515 â ditutup di finally
            handles.append(f)

            if m.photo:
                media_item = InputMediaPhoto(media=f, caption=caption)
            elif m.video:
                thumbnail_path = await _create_video_thumbnail_async(path)
                thumbnail_handle = None
                if thumbnail_path:
                    try:
                        thumbnail_handle = open(thumbnail_path, "rb")
                        thumbnail_paths.append(thumbnail_path)
                        thumbnail_handles.append(thumbnail_handle)
                    except OSError:
                        thumbnail_handle = None
                        try:
                            os.remove(thumbnail_path)
                        except OSError:
                            pass
                        thumbnail_path = None

                video_kwargs = {}
                if thumbnail_handle:
                    video_kwargs["thumbnail"] = thumbnail_handle
                media_item = InputMediaVideo(
                    media=f,
                    caption=caption,
                    supports_streaming=True,
                    **_video_metadata(m),
                    **video_kwargs,
                )
            elif m.audio:
                media_item = InputMediaAudio(media=f, caption=caption)
            elif m.animation:
                media_item = InputMediaAnimation(media=f, caption=caption)
            else:
                media_item = InputMediaDocument(media=f, caption=caption)

            downloaded_items.append(
                (m, path, media_item, _album_media_kind(m))
            )

        if len(downloaded_items) != total:
            return False, (
                f"Album tidak lengkap: hanya {len(downloaded_items)}/{total} "
                "media berhasil diunduh."
            )

        async def send_individually(items: list[tuple]) -> int:
            sent = 0
            for index, (message, path, _, _) in enumerate(items, 1):
                item_sent = False
                for send_attempt in range(2):
                    if on_progress:
                        await _notify_progress(
                            on_progress,
                            f"📤 <b>Mengirim media satuan...</b> "
                            f"({index}/{len(items)}, percobaan {send_attempt + 1}/2)",
                        )
                    try:
                        await _send_album_item(
                            client, bot, message, path, user_chat_id,
                            on_progress=on_progress,
                        )
                        item_sent = True
                        sent += 1
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as send_error:
                        logger.warning(
                            "Gagal mengirim album item %s attempt %s: %s",
                            getattr(message, "id", "?"),
                            send_attempt + 1,
                            send_error,
                        )
                        if send_attempt == 0:
                            await asyncio.sleep(2)
                if not item_sent:
                    logger.error(
                        "Album item %s gagal setelah 2 percobaan.",
                        getattr(message, "id", "?"),
                    )
            return sent

        failed_batches: list[str] = []
        for batch in _album_batches(downloaded_items):
            batch_kind = batch[0][3]
            if batch_kind is None or len(batch) < 2:
                sent = await send_individually(batch)
                if sent != len(batch):
                    failed_batches.append("media tidak kompatibel")
                continue

            # Bot API tidak dapat menerima file >50 MB dalam sendMediaGroup.
            # Jangan mencoba meng-upload group yang pasti ditolak setelah
            # seluruh body request dikirim; gunakan jalur individual yang
            # memilih Bot API atau Pyrogram sesuai ukuran setiap item.
            oversized_for_bot = any(
                (_get_file_size(item[0]) or 0) > _BOT_API_UPLOAD_LIMIT
                for item in batch
            )
            if oversized_for_bot:
                await _notify_progress(
                    on_progress,
                    f"📤 <b>Mengirim album satu per satu...</b> "
                    f"({len(batch)} media, ada file di atas 50 MB)",
                )
                sent = await send_individually(batch)
                if sent != len(batch):
                    failed_batches.append(f"{sent}/{len(batch)} media tipe {batch_kind}")
                continue

            if on_progress:
                await _notify_progress(
                    on_progress,
                    f"📤 <b>Mengirim album...</b> "
                    f"({len(batch)} media)",
                )
            try:
                _album_timeout = (
                    min(
                        len(batch) * _ALBUM_UPLOAD_TIMEOUT_PER_FILE + 60,
                        _ALBUM_UPLOAD_TIMEOUT_MAX,
                    )
                )
                await _send_media_group_with_watchdog(
                    bot,
                    user_chat_id,
                    batch,
                    on_progress,
                    _album_timeout,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError as album_error:
                # Request bisa saja sudah diterima Telegram ketika koneksi
                # client macet. Jangan langsung fallback dan membuat duplikat.
                logger.warning(
                    "Timeout mengirim media group (%s media): %s",
                    len(batch),
                    album_error,
                )
                return False, (
                    "Upload album timeout setelah beberapa menit. "
                    "Coba lagi dengan album yang sama."
                )
            except Exception as album_error:
                logger.warning(
                    "Gagal mengirim media group (%s media, tipe %s): %s; "
                    "fallback ke pengiriman satuan",
                    len(batch),
                    batch_kind,
                    album_error,
                )
                sent = await send_individually(batch)
                if sent != len(batch):
                    failed_batches.append(
                        f"{sent}/{len(batch)} media tipe {batch_kind}"
                    )

        if failed_batches:
            return False, (
                "Sebagian media album gagal dikirim: "
                + ", ".join(failed_batches)
            )
        return True, None
    finally:
        for f in handles:
            try:
                f.close()
            except Exception:
                pass
        for f in thumbnail_handles:
            try:
                f.close()
            except Exception:
                pass
        for p in paths:
            try:
                os.remove(p)
            except Exception:
                pass
        for directory in download_dirs:
            shutil.rmtree(directory, ignore_errors=True)
        for p in thumbnail_paths:
            try:
                os.remove(p)
            except Exception:
                pass


async def _pyrogram_copy_with_notice(client, bot, msg, user_chat_id: int, file_size: int):
    """
    Fallback untuk file besar (>50 MB) di channel private yang TIDAK restricted:
    Pyrogram meng-copy langsung ke chat bot user via MTProto (bypass batas 50 MB Bot API).
    """
    bot_peer = f"@{_BOT_USERNAME}" if _BOT_USERNAME else user_chat_id
    await _hard_timeout(
        msg.copy(bot_peer),
        timeout=_UPLOAD_TIMEOUT,
        operation=f"copy message {getattr(msg, 'id', '?')}",
    )


async def _download_and_upload_via_pyrogram(client, bot, msg, user_chat_id: int,
                                            file_size: int, on_progress=None):
    """
    Untuk file besar (>50 MB) dari channel restricted:
    Download file via Pyrogram lalu upload ulang langsung ke chat bot user via MTProto.
    Bypass sekaligus: batas 50 MB Bot API + larangan forward/copy dari channel restricted.
    File Premium bisa sampai 2 GB (sesuai MAX_FILE_SIZE_BYTES_PREMIUM di config).
    on_progress: async callable(text: str) untuk update pesan status (opsional).
    """
    show_progress = on_progress and file_size >= _PROGRESS_MIN_BYTES
    dl_cb = _make_pyrogram_progress(on_progress, "Mengunduh", file_size) if show_progress else None
    work_dir = _new_download_dir(user_chat_id)
    transfer_timeout = _media_transfer_timeout(file_size, _DOWNLOAD_TIMEOUT)
    upload_timeout = _media_transfer_timeout(file_size, _UPLOAD_TIMEOUT)
    path = None
    thumbnail_path = None

    try:
        try:
            path = await _download_media(
                client,
                msg,
                file_name=work_dir,
                timeout=transfer_timeout,
                operation=f"download message {getattr(msg, 'id', '?')}",
                progress=dl_cb,
            )
        except asyncio.TimeoutError:
            raise RuntimeError("Download timeout — file terlalu lama diunduh, coba lagi.")

        if not path:
            raise RuntimeError("Download gagal, file tidak tersedia.")

        await _notify_progress(on_progress, "📤 <b>Mengirim media...</b>")
        # Kirim ke chat bot (bukan Saved Messages).
        # Dari sudut pandang Pyrogram (login sebagai user), mengirim ke @bot_username
        # membuat file muncul langsung di chat antara user dan bot.
        bot_peer = f"@{_BOT_USERNAME}" if _BOT_USERNAME else user_chat_id

        ul_cb = _make_pyrogram_progress(on_progress, "Mengirim", file_size) if show_progress else None
        caption = _build_caption(msg.caption or "")
        # Thumbnail dibuat setelah download selesai agar preview video tetap ada.
        # Jika gagal, upload utama tetap diteruskan tanpa thumbnail.
        thumbnail_path = (
            await _create_video_thumbnail_async(path)
            if msg.video else None
        )
        metadata = _video_metadata(msg)
        if msg.photo:
            await _run_transfer_with_watchdog(
                lambda transfer_progress: client.send_photo(
                    bot_peer,
                    path,
                    caption=caption,
                    progress=transfer_progress,
                ),
                timeout=upload_timeout,
                operation="Pyrogram send_photo",
                progress=ul_cb,
            )
        elif msg.video:
            await _run_transfer_with_watchdog(
                lambda transfer_progress: client.send_video(
                    bot_peer,
                    path,
                    caption=caption,
                    supports_streaming=True,
                    thumb=thumbnail_path,
                    progress=transfer_progress,
                    **metadata,
                ),
                timeout=upload_timeout,
                operation="Pyrogram send_video",
                progress=ul_cb,
            )
        elif msg.audio:
            await _run_transfer_with_watchdog(
                lambda transfer_progress: client.send_audio(
                    bot_peer,
                    path,
                    caption=caption,
                    progress=transfer_progress,
                ),
                timeout=upload_timeout,
                operation="Pyrogram send_audio",
                progress=ul_cb,
            )
        elif msg.voice:
            await _run_transfer_with_watchdog(
                lambda transfer_progress: client.send_voice(
                    bot_peer,
                    path,
                    caption=caption,
                    progress=transfer_progress,
                ),
                timeout=upload_timeout,
                operation="Pyrogram send_voice",
                progress=ul_cb,
            )
        elif msg.video_note:
            await _run_transfer_with_watchdog(
                lambda transfer_progress: client.send_video_note(
                    bot_peer,
                    path,
                    progress=transfer_progress,
                ),
                timeout=upload_timeout,
                operation="Pyrogram send_video_note",
                progress=ul_cb,
            )
        elif msg.animation:
            await _run_transfer_with_watchdog(
                lambda transfer_progress: client.send_animation(
                    bot_peer,
                    path,
                    caption=caption,
                    progress=transfer_progress,
                ),
                timeout=upload_timeout,
                operation="Pyrogram send_animation",
                progress=ul_cb,
            )
        elif msg.sticker:
            await _run_transfer_with_watchdog(
                lambda transfer_progress: client.send_sticker(
                    bot_peer,
                    path,
                    progress=transfer_progress,
                ),
                timeout=upload_timeout,
                operation="Pyrogram send_sticker",
                progress=ul_cb,
            )
        else:
            await _run_transfer_with_watchdog(
                lambda transfer_progress: client.send_document(
                    bot_peer,
                    path,
                    caption=caption,
                    progress=transfer_progress,
                ),
                timeout=upload_timeout,
                operation="Pyrogram send_document",
                progress=ul_cb,
            )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            "Upload timeout — koneksi Telegram terlalu lambat. Coba lagi."
        ) from exc
    finally:
        if thumbnail_path:
            try:
                os.remove(thumbnail_path)
            except Exception:
                pass
        shutil.rmtree(work_dir, ignore_errors=True)


async def _send_album_item(
    client, bot, msg, path: str, user_chat_id: int,
    on_progress=None,
) -> None:
    """Kirim satu item album melalui jalur yang sesuai dengan ukuran file."""
    caption = _build_caption(msg.caption or "")
    file_size = _get_file_size(msg) or 0
    bot_peer = f"@{_BOT_USERNAME}" if _BOT_USERNAME else user_chat_id
    upload_timeout = _media_transfer_timeout(file_size, _UPLOAD_TIMEOUT)
    # Thumbnail dibuat untuk video, tetapi kegagalannya tidak boleh membatalkan
    # jalur fallback pengiriman media.
    thumbnail_path = (
        await _create_video_thumbnail_async(path)
        if msg.video else None
    )
    metadata = _video_metadata(msg)
    _kw = dict(
        write_timeout=_PTB_WRITE_TIMEOUT,
        read_timeout=_PTB_READ_TIMEOUT,
        connect_timeout=_PTB_CONNECT_TIMEOUT,
    )

    try:
        if file_size > _BOT_API_UPLOAD_LIMIT:
            upload_progress = (
                _make_pyrogram_progress(on_progress, "Mengirim", file_size)
                if on_progress else None
            )
            if msg.photo:
                await _run_transfer_with_watchdog(
                    lambda transfer_progress: client.send_photo(
                        bot_peer,
                        path,
                        caption=caption,
                        progress=transfer_progress,
                    ),
                    timeout=upload_timeout,
                    operation="Pyrogram album send_photo",
                    progress=upload_progress,
                )
            elif msg.video:
                await _run_transfer_with_watchdog(
                    lambda transfer_progress: client.send_video(
                        bot_peer,
                        path,
                        caption=caption,
                        supports_streaming=True,
                        thumb=thumbnail_path,
                        progress=transfer_progress,
                        **metadata,
                    ),
                    timeout=upload_timeout,
                    operation="Pyrogram album send_video",
                    progress=upload_progress,
                )
            elif msg.audio:
                await _run_transfer_with_watchdog(
                    lambda transfer_progress: client.send_audio(
                        bot_peer,
                        path,
                        caption=caption,
                        progress=transfer_progress,
                    ),
                    timeout=upload_timeout,
                    operation="Pyrogram album send_audio",
                    progress=upload_progress,
                )
            elif msg.voice:
                await _run_transfer_with_watchdog(
                    lambda transfer_progress: client.send_voice(
                        bot_peer,
                        path,
                        caption=caption,
                        progress=transfer_progress,
                    ),
                    timeout=upload_timeout,
                    operation="Pyrogram album send_voice",
                    progress=upload_progress,
                )
            elif msg.video_note:
                await _run_transfer_with_watchdog(
                    lambda transfer_progress: client.send_video_note(
                        bot_peer,
                        path,
                        progress=transfer_progress,
                    ),
                    timeout=upload_timeout,
                    operation="Pyrogram album send_video_note",
                    progress=upload_progress,
                )
            elif msg.animation:
                await _run_transfer_with_watchdog(
                    lambda transfer_progress: client.send_animation(
                        bot_peer,
                        path,
                        caption=caption,
                        progress=transfer_progress,
                    ),
                    timeout=upload_timeout,
                    operation="Pyrogram album send_animation",
                    progress=upload_progress,
                )
            else:
                await _run_transfer_with_watchdog(
                    lambda transfer_progress: client.send_document(
                        bot_peer,
                        path,
                        caption=caption,
                        progress=transfer_progress,
                    ),
                    timeout=upload_timeout,
                    operation="Pyrogram album send_document",
                    progress=upload_progress,
                )
            return

        with open(path, "rb") as f:
            if msg.photo:
                await asyncio.wait_for(
                    bot.send_photo(user_chat_id, photo=f, caption=caption, **_kw),
                    timeout=upload_timeout,
                )
            elif msg.video:
                if thumbnail_path:
                    with open(thumbnail_path, "rb") as thumb:
                        await asyncio.wait_for(
                            bot.send_video(
                                user_chat_id,
                                video=f,
                                caption=caption,
                                thumbnail=thumb,
                                supports_streaming=True,
                                **metadata,
                                **_kw,
                            ),
                            timeout=upload_timeout,
                        )
                else:
                    await asyncio.wait_for(
                        bot.send_video(
                            user_chat_id,
                            video=f,
                            caption=caption,
                            supports_streaming=True,
                            **metadata,
                            **_kw,
                        ),
                        timeout=upload_timeout,
                    )
            elif msg.audio:
                await asyncio.wait_for(
                    bot.send_audio(user_chat_id, audio=f, caption=caption, **_kw),
                    timeout=upload_timeout,
                )
            elif msg.voice:
                await asyncio.wait_for(
                    bot.send_voice(user_chat_id, voice=f, caption=caption, **_kw),
                    timeout=upload_timeout,
                )
            elif msg.animation:
                await asyncio.wait_for(
                    bot.send_animation(user_chat_id, animation=f, caption=caption, **_kw),
                    timeout=upload_timeout,
                )
            else:
                await asyncio.wait_for(
                    bot.send_document(user_chat_id, document=f, caption=caption, **_kw),
                    timeout=upload_timeout,
                )
    finally:
        if thumbnail_path:
            try:
                os.remove(thumbnail_path)
            except Exception:
                pass


async def _send_album_individually(
    client, bot, chat, msg_id: int, user_chat_id: int,
    on_progress=None, is_premium: bool = False, messages=None,
) -> tuple[bool, str | None]:
    """
    Fallback album: download semua file lalu coba kirim sebagai album (send_media_group).
    Jika album gagal (misal file terlalu besar / error PTB), kirim satu per satu.
    TIDAK menggunakan copy/forward â semua file didownload fresh agar bypass restriction.
    on_progress: async callable(text: str) untuk update status (opsional).
    """
    if messages is not None:
        # Reuse metadata already fetched by the caller for private t.me/c links.
        msgs = messages
    else:
        try:
            msgs = await _fetch_album_messages(client, chat, msg_id, on_progress=on_progress)
        except Exception as e:
            return False, f"Gagal mengambil album: {e}"

    if not msgs:
        return False, "Album kosong atau tidak ditemukan."

    total = len(msgs)
    size_limit = MAX_FILE_SIZE_BYTES_PREMIUM if is_premium else MAX_FILE_SIZE_BYTES
    size_label = (
        f"{MAX_FILE_SIZE_MB_PREMIUM} MB (Premium)"
        if is_premium else f"{MAX_FILE_SIZE_MB} MB"
    )
    oversized = [
        _get_file_size(m) for m in msgs
        if _get_file_size(m) and _get_file_size(m) > size_limit
    ]
    if oversized:
        return False, (
            f"Album memiliki media terlalu besar ({_fmt_size(max(oversized))}). "
            f"Batas maksimal: {size_label}."
        )

    # Download semua file terlebih dahulu
    paths: list[str] = []
    download_dirs: list[str] = []
    for i, m in enumerate(msgs):
        file_size  = _get_file_size(m) or 0
        dl_timeout = max(60, 30 + (file_size // (10 * 1024 * 1024)) * 30)
        dl_cb = None
        if on_progress and file_size >= _PROGRESS_MIN_BYTES:
            dl_cb = _make_pyrogram_progress(
                on_progress,
                f"Mengunduh ({i + 1}/{total})",
                file_size,
            )
        elif on_progress:
            await _notify_progress(
                on_progress,
                f"📥 <b>Mengunduh album...</b> ({i + 1}/{total})",
            )
        path = None
        item_dir = _new_download_dir(user_chat_id)
        for _dl_attempt in range(2):
            try:
                path = await _download_media(
                    client,
                    m,
                    file_name=item_dir,
                    timeout=dl_timeout,
                    operation=f"download album fallback item {i + 1}/{total}",
                    progress=dl_cb,
                )
                if path and os.path.isfile(path) and os.path.getsize(path) > 0:
                    break
                path = None
            except (asyncio.TimeoutError, Exception) as _dl_err:
                logger.warning(
                    f"Download album item {i + 1}/{total} msg {m.id} "
                    f"attempt {_dl_attempt + 1} gagal: {_dl_err}"
                )
                if _dl_attempt == 0:
                    await asyncio.sleep(2)
        if path:
            paths.append((m, path))
            download_dirs.append(item_dir)
        else:
            logger.error(f"Skip album item {i + 1}/{total} msg {m.id} setelah 2 percobaan.")
            shutil.rmtree(item_dir, ignore_errors=True)

    if not paths:
        return False, "Gagal mendownload semua file dalam album."

    # Kirim satu per satu dengan progress per file.
    # send_media_group sengaja dilewati di sini karena fungsi ini adalah fallback
    # path (channel restricted / setelah send_media_group utama gagal) dan
    # send_media_group untuk banyak file besar sering hang tanpa bisa dicancel.
    sent          = 0
    n_paths       = len(paths)
    for idx, (m, path) in enumerate(paths):
        item_sent = False
        for _send_attempt in range(2):
            if on_progress:
                try:
                    await on_progress(
                        f"📤 <b>Mengirim album...</b> "
                        f"({idx + 1}/{n_paths}, percobaan {_send_attempt + 1}/2)"
                    )
                except Exception:
                    pass
            try:
                await _send_album_item(
                    client, bot, m, path, user_chat_id,
                    on_progress=on_progress,
                )
                item_sent = True
                sent += 1
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    f"Gagal kirim file album msg {m.id} "
                    f"attempt {_send_attempt + 1}: {e}"
                )
                if _send_attempt == 0:
                    await asyncio.sleep(2)
        if not item_sent:
            logger.error(f"File album msg {m.id} gagal setelah 2 percobaan.")
        try:
            os.remove(path)
        except Exception:
            pass
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    if sent == 0:
        return False, "Semua file dalam album gagal dikirim."
    if sent != total:
        return False, f"Album hanya terkirim {sent}/{total} media. Silakan coba lagi."

    return True, None


_ALBUM_MESSAGE_WINDOW = 10


async def _fetch_album_messages(client, chat, msg_id: int, on_progress=None):
    """Ambil seluruh album dengan wrapper Pyrogram dan fallback raw API."""
    await _notify_progress(
        on_progress, "📥 <b>Mengambil metadata album...</b>"
    )

    try:
        album_messages = await _hard_timeout(
            client.get_media_group(chat, msg_id),
            timeout=_ALBUM_FETCH_TIMEOUT,
            operation=f"get_media_group({chat}, {msg_id})",
        )
        if album_messages:
            return sorted(album_messages, key=lambda message: message.id)
    except asyncio.TimeoutError:
        logger.warning(
            "get_media_group timeout untuk %s/%s, coba raw API",
            chat, msg_id,
        )
    except Exception as wrapper_error:
        logger.warning(
            "get_media_group gagal untuk %s/%s, coba raw API: %s",
            chat, msg_id, wrapper_error,
        )

    peer = await _hard_timeout(
        client.resolve_peer(chat),
        timeout=_PEER_RESOLVE_TIMEOUT,
        operation=f"resolve_peer({chat}) for album",
    )

    first_id = max(1, msg_id - _ALBUM_MESSAGE_WINDOW)
    last_id = msg_id + _ALBUM_MESSAGE_WINDOW
    request_ids = [
        raw.types.InputMessageID(id=message_id)
        for message_id in range(first_id, last_id + 1)
    ]
    if isinstance(peer, raw.types.InputPeerChannel):
        channel = raw.types.InputChannel(
            channel_id=peer.channel_id,
            access_hash=peer.access_hash,
        )
        request = raw.functions.channels.GetMessages(
            channel=channel,
            id=request_ids,
        )
    else:
        request = raw.functions.messages.GetMessages(id=request_ids)

    result = await _hard_timeout(
        client.invoke(request),
        timeout=_ALBUM_FETCH_TIMEOUT,
        operation=f"get album range({chat}, {first_id}-{last_id})",
    )
    raw_messages = getattr(result, "messages", None) or []
    parsed_messages = []
    for raw_message in raw_messages:
        parsed = Message._parse(
            client,
            raw_message,
            getattr(result, "users", None) or [],
            getattr(result, "chats", None) or [],
        )
        if hasattr(parsed, "__await__"):
            parsed = await parsed
        if parsed and not getattr(parsed, "empty", False):
            parsed_messages.append(parsed)

    target = next(
        (message for message in parsed_messages if message.id == msg_id),
        None,
    )
    if target is None:
        raise RuntimeError(f"Pesan album {msg_id} tidak ditemukan.")

    group_id = getattr(target, "media_group_id", None)
    if not group_id:
        return [target]

    album_messages = [
        message for message in parsed_messages
        if getattr(message, "media_group_id", None) == group_id
    ]
    if on_progress:
        await _notify_progress(
            on_progress,
            f"📥 <b>Album ditemukan</b> ({len(album_messages)} media)",
        )
    return sorted(album_messages, key=lambda message: message.id)


async def _copy_public_album(
    bot, source_chat: str, messages, user_chat_id: int, on_progress=None,
) -> tuple[bool, str | None] | None:
    """
    Salin album publik langsung dari Telegram tanpa melewati Railway.

    Return None berarti jalur Bot API tidak tersedia sebelum ada pesan yang
    berhasil dikirim, sehingga pemanggil boleh mencoba fallback download.
    Jika sudah ada pesan yang tersalin lalu item berikutnya gagal, return
    (False, reason) agar pemanggil tidak mengirim ulang item yang sama.
    """
    if not isinstance(source_chat, str) or not source_chat.startswith("@"):
        return None

    # Bot API dapat menyalin album publik sebagai satu media group. Gunakan
    # jalur ini terlebih dahulu agar album tidak dipecah menjadi pesan satuan.
    message_ids = [message.id for message in messages]
    if len(message_ids) >= 2 and hasattr(bot, "copy_messages"):
        await _notify_progress(
            on_progress,
            f"📤 <b>Menyalin album...</b> ({len(message_ids)} media)",
        )
        try:
            await asyncio.wait_for(
                bot.copy_messages(
                    chat_id=user_chat_id,
                    from_chat_id=source_chat,
                    message_ids=message_ids,
                    write_timeout=_PTB_WRITE_TIMEOUT,
                    read_timeout=_PTB_READ_TIMEOUT,
                    connect_timeout=_PTB_CONNECT_TIMEOUT,
                ),
                timeout=_BOT_COPY_TIMEOUT,
            )
            return True, None
        except asyncio.TimeoutError:
            logger.warning(
                "Timeout saat menyalin album publik %s sebagai group",
                source_chat,
            )
        except (BadRequest, Forbidden) as exc:
            logger.info(
                "copy_messages tidak tersedia untuk album publik %s: %s",
                source_chat,
                exc,
            )
        except Exception as exc:
            logger.warning(
                "copy_messages gagal untuk album publik %s: %s",
                source_chat,
                exc,
            )

    copied = 0
    for index, message in enumerate(messages, 1):
        await _notify_progress(
            on_progress,
            f"📤 <b>Menyalin album...</b> ({index}/{len(messages)})",
        )
        try:
            await asyncio.wait_for(
                bot.copy_message(
                    chat_id=user_chat_id,
                    from_chat_id=source_chat,
                    message_id=message.id,
                    write_timeout=_PTB_WRITE_TIMEOUT,
                    read_timeout=_PTB_READ_TIMEOUT,
                    connect_timeout=_PTB_CONNECT_TIMEOUT,
                ),
                timeout=_BOT_COPY_TIMEOUT,
            )
            copied += 1
        except asyncio.TimeoutError:
            reason = "Timeout saat menyalin album dari Telegram."
        except (BadRequest, Forbidden) as exc:
            reason = f"Bot API tidak bisa menyalin album: {exc}"
        except Exception as exc:
            reason = f"Gagal menyalin album: {exc}"

        if copied < index:
            if copied:
                return False, (
                    f"Album hanya tersalin {copied}/{len(messages)} media. "
                    "Silakan coba lagi."
                )
            logger.info(
                "Bot API album copy tidak tersedia untuk %s: %s",
                source_chat,
                reason,
            )
            return None

    return True, None


# ââ SafeForward âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

class SafeForward:

    @staticmethod
    async def run_album(
        client, bot, user_chat_id: int, chat, msg_id: int,
        on_progress=None, is_premium: bool = False,
    ) -> tuple[bool, str | None]:
        """
        Kirim seluruh album yang mengandung `msg_id` ke `user_chat_id`.

        Strategi pengiriman:
          0. Deteksi noforwards (has_protected_content) â jika aktif, langsung ke (2)
          1. send_media_group via PTB (download Pyrogram + upload bot, tanpa forward)
          2. Jika gagal / restricted: _send_album_individually (download fresh + send_media_group)
          3. Fallback terakhir: kirim file satu per satu jika send_media_group masih gagal

        on_progress: async callable(text: str) untuk update status (opsional).
        Return (True, None) jika berhasil, (False, alasan) jika gagal.
        """
        await _notify_progress(on_progress, "🔌 <b>Menghubungkan ke channel...</b>")
        source_chat, src_err = await _resolve_source(client, chat)
        if src_err:
            return False, src_err

        # Fetch album metadata before checking protected content.
        # Fetch album metadata first. A private t.me/c link can stall on get_chat
        # before the peer is available; protected-content status is read from
        # the fetched album messages instead.

        for attempt in range(MAX_RETRIES + 1):
            try:
                await _notify_progress(on_progress, "📥 <b>Mengambil album...</b>")
                try:
                    album_messages = await _hard_timeout(
                        _fetch_album_messages(
                            client, source_chat, msg_id, on_progress=on_progress
                        ),
                        timeout=_ALBUM_FETCH_TIMEOUT + 8,
                        operation=f"fetch album({source_chat}, {msg_id})",
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Album metadata timeout for %s/%s", source_chat, msg_id
                    )
                    return False, (
                        "Timeout saat mengambil album. "
                        "Coba lagi beberapa saat kemudian."
                    )
                except Exception as e:
                    logger.exception(
                        "Album metadata error for %s/%s", source_chat, msg_id
                    )
                    return False, f"Gagal mengambil album: {e}"
                is_restricted = any(
                    bool(getattr(message, "has_protected_content", False))
                    for message in album_messages
                )
                if is_restricted:
                    return await _send_album_individually(
                        client, bot, source_chat, msg_id, user_chat_id,
                        on_progress=on_progress, is_premium=is_premium,
                        messages=album_messages,
                    )

                # Album publik tidak perlu di-download ke Railway. Salin
                # setiap item langsung dari channel melalui Bot API.
                if not is_restricted:
                    copied_result = await _copy_public_album(
                        bot, chat, album_messages, user_chat_id,
                        on_progress=on_progress,
                    )
                    if copied_result is not None:
                        return copied_result

                album_result = await _send_album_via_bot(
                    client, bot, source_chat, msg_id, user_chat_id,
                    on_progress=on_progress, is_premium=is_premium,
                    messages=album_messages,
                )
                if album_result is not None:
                    return album_result
                return True, None

            except FloodWait as e:
                wait = min(e.value, FLOOD_LIMIT)
                logger.warning(f"FloodWait {wait}s on album msg {msg_id}")
                if attempt < MAX_RETRIES:
                    try:
                        await bot.send_message(
                            user_chat_id,
                            f"⏳ <b>Telegram membatasi kecepatan sementara.</b>\n"
                            f"Menunggu <b>{wait} detik</b> lalu mencoba ulang...",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(wait)
                else:
                    return False, f"Rate limit Telegram. Coba lagi dalam {e.value} detik."

            except FileReferenceExpired:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1)
                else:
                    return False, "File reference kedaluwarsa. Coba lagi nanti."

            except ChatForwardsRestricted:
                # Channel melarang forwarding. copy_media_group tidak akan pernah berhasil.
                # Langsung kirim satu per satu via download + re-upload (bypass restriction).
                logger.info(f"ChatForwardsRestricted on album msg {msg_id}, kirim satu per satu")
                return await _send_album_individually(
                    client, bot, source_chat, msg_id, user_chat_id,
                    on_progress=on_progress, is_premium=is_premium,
                )

            except Exception as e:
                logger.error(f"send_album error msg {msg_id} attempt {attempt}: {e}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1 + random.uniform(0, 1))
                else:
                    # Fallback terakhir: kirim tiap file satu per satu
                    # (JANGAN gunakan copy_media_group â akan gagal di channel restricted)
                    logger.info(f"Fallback kirim album satu per satu msg {msg_id}: {e}")
                    return await _send_album_individually(
                    client, bot, source_chat, msg_id, user_chat_id,
                        on_progress=on_progress, is_premium=is_premium,
                    )

        return False, "Gagal setelah beberapa percobaan."

    @staticmethod
    async def run(
        client, bot, user_chat_id: int, chat, msg_id: int,
        on_progress=None,
        is_premium: bool = False,
        skip_public_copy: bool = False,
        single_only: bool = False,
    ) -> tuple[bool, str | None]:
        """
        Ambil pesan dari `chat`/`msg_id` dan kirim ke `user_chat_id` via PTB bot.

        Strategi pengiriman berdasarkan ukuran & akses:
          0. Deteksi noforwards (has_protected_content) â jika aktif, pakai download+upload
          â¢ Fast path (bot.copy_message): tanpa download, bebas ukuran, untuk channel terbuka
          â¢ Slow path â¤50 MB: download via Pyrogram â re-upload via PTB bot
          â¢ Fallback >50 MB private terbuka: Pyrogram copy â Saved Messages + notifikasi
          â¢ Fallback >50 MB restricted: upload ulang via Pyrogram MTProto
        on_progress: async callable(text: str) untuk update progress ke user (opsional).
        """
        # ââ Jalur cepat untuk channel publik ââââââââââââââââââââââââââââââ
        # Bot API dapat menyalin pesan publik tanpa mengambilnya terlebih
        # dahulu lewat Pyrogram. Ini menghindari get_messages() yang dapat
        # menunggu terlalu lama pada koneksi server tertentu.
        if not skip_public_copy and isinstance(chat, str) and chat.startswith("@"):
            try:
                if await copy_public_message(
                    bot, user_chat_id, chat, msg_id, on_progress=on_progress
                ):
                    return True, None
            except Exception:
                # copy_public_message already logs expected failures; retain
                # the Pyrogram fallback for unexpected integration errors.
                logger.exception("Public message copy helper failed for %s/%s", chat, msg_id)

        # ââ Langkah 1: Pastikan source bisa diakses ââââââââââââââââââââââ
        await _notify_progress(on_progress, "🔌 <b>Menghubungkan ke channel...</b>")
        source_chat, src_err = await _resolve_source(client, chat)
        if src_err:
            return False, src_err

        # The album branch handles protected content after fetching metadata.

        # ââ Langkah 2: Ambil pesan âââââââââââââââââââââââââââââââââââââââ
        await _notify_progress(on_progress, "📥 <b>Mengambil pesan dari channel...</b>")
        try:
            msg = await _get_message(client, source_chat, msg_id)
        except asyncio.TimeoutError:
            logger.warning(f"Timeout get_messages({source_chat}, {msg_id})")
            return False, (
                "❌ Tidak bisa mengambil pesan (timeout).\n"
                "Pastikan akun sudah bergabung ke channel tersebut."
            )
        except (MessageIdInvalid, MsgIdInvalid):
            return False, f"Pesan nomor `{msg_id}` tidak ditemukan."
        except _PEER_ERRORS:
            return False, (
                "❌ Tidak bisa mengakses channel.\n"
                "Pastikan akun yang login sudah bergabung ke channel/grup tersebut."
            )
        except Exception as e:
            logger.warning(f"get_messages({source_chat}, {msg_id}) error: {e}")
            return False, f"Gagal mengambil pesan: {e}"

        if not msg or msg.empty:
            return False, f"Pesan `{msg_id}` kosong atau sudah dihapus."

        # ââ Auto-deteksi album ââââââââââââââââââââââââââââââââââââââââââââ
        if msg.media_group_id and not single_only:
            return await SafeForward.run_album(
                client, bot, user_chat_id, chat, msg_id,
                on_progress=on_progress, is_premium=is_premium,
            )

        # Single messages still use the protected-content check after fetch.
        await _notify_progress(on_progress, "🔎 <b>Memeriksa akses media...</b>")
        is_restricted = await _is_forwards_restricted(client, source_chat)
        # ââ Langkah 3: Cek ukuran file terhadap hard limit âââââââââââââââ
        file_size  = _get_file_size(msg)
        size_limit = MAX_FILE_SIZE_BYTES_PREMIUM if is_premium else MAX_FILE_SIZE_BYTES
        size_label = f"{MAX_FILE_SIZE_MB_PREMIUM} MB (Premium)" if is_premium else f"{MAX_FILE_SIZE_MB} MB"
        if file_size and file_size > size_limit:
            size_str = _fmt_size(file_size)
            return False, (
                f"File terlalu besar ({size_str}). "
                f"Batas maksimal: {size_label}."
            )

        is_large = bool(file_size and file_size > _BOT_API_UPLOAD_LIMIT)

        # ââ Langkah 4: Kirim ke user (dengan retry) ââââââââââââââââââââââ
        for attempt in range(MAX_RETRIES + 1):
            try:
                if msg.media:
                    if is_restricted:
                        # Channel noforwards: download tetap dilakukan lewat Pyrogram,
                        # tetapi file kecil harus di-upload oleh bot agar pengirimnya
                        # tetap bot. Hanya file >50 MB yang memakai akun Pyrogram,
                        # karena melewati batas upload Bot API.
                        if is_large:
                            await _download_and_upload_via_pyrogram(
                                client, bot, msg, user_chat_id, file_size,
                                on_progress=on_progress,
                            )
                        else:
                            await _download_and_send_via_bot(
                                client, bot, msg, user_chat_id,
                                on_progress=on_progress,
                            )
                        return True, None
                    else:
                        # Fast path: PTB bot.copy_message
                        # Tidak ada batasan ukuran (file tidak di-download),
                        # tidak masuk Saved Messages karena dikirim dari bot.
                        try:
                            await bot.copy_message(
                                chat_id=user_chat_id,
                                from_chat_id=chat,
                                message_id=msg_id,
                            )
                            return True, None
                        except (BadRequest, Forbidden):
                            # Bot tidak bisa akses source (private / restricted)
                            if is_large:
                                # File >50 MB â tidak bisa di-re-upload via Bot API
                                # Pyrogram copy langsung ke Saved Messages + notifikasi
                                await _pyrogram_copy_with_notice(
                                    client, bot, msg, user_chat_id, file_size
                                )
                                return True, None
                            else:
                                # File kecil tetap di-upload oleh bot setelah
                                # download ulang. Jalur Pyrogram hanya diperlukan
                                # untuk file yang melewati batas Bot API.
                                await _download_and_send_via_bot(
                                    client, bot, msg, user_chat_id,
                                    on_progress=on_progress,
                                )
                                return True, None
                else:
                    if msg.text:
                        await bot.send_message(user_chat_id, msg.text)
                    else:
                        return False, f"Pesan `{msg_id}` tidak memiliki konten yang bisa dikirim."
                return True, None

            except FloodWait as e:
                wait = min(e.value, FLOOD_LIMIT)
                logger.warning(f"FloodWait {wait}s on msg {msg_id}")
                if attempt < MAX_RETRIES:
                    try:
                        await bot.send_message(
                            user_chat_id,
                            f"⏳ <b>Telegram membatasi kecepatan sementara.</b>\n"
                            f"Menunggu <b>{wait} detik</b> lalu mencoba ulang...",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(wait)
                else:
                    return False, f"Rate limit Telegram. Coba lagi dalam {e.value} detik."

            except FileReferenceExpired:
                if attempt < MAX_RETRIES:
                    try:
                        msg = await _hard_timeout(
                            client.get_messages(chat, msg_id),
                            timeout=_MSG_FETCH_TIMEOUT,
                            operation=f"refresh get_messages({chat}, {msg_id})",
                        )
                        await asyncio.sleep(1)
                    except Exception:
                        pass
                else:
                    return False, "File reference kedaluwarsa. Coba lagi nanti."

            except Exception as e:
                logger.error(f"send error msg {msg_id} attempt {attempt}: {e}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1 + random.uniform(0, 1))
                else:
                    return False, f"Gagal mengirim: {e}"

        return False, "Gagal setelah beberapa percobaan."
