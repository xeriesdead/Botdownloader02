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
    RPCError,
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

# Identitas bot — diset sekali saat startup.
_BOT_USERNAME: str = ""
_BOT_USER_ID: int | None = None


def set_bot_username(username: str, user_id: int | None = None):
    global _BOT_USERNAME, _BOT_USER_ID
    _BOT_USERNAME = username
    if user_id:
        _BOT_USER_ID = int(user_id)


def _bot_peer():
    """Kembalikan peer bot yang stabil untuk session user Pyrogram."""
    if _BOT_USER_ID:
        return _BOT_USER_ID
    if _BOT_USERNAME:
        return f"@{_BOT_USERNAME}"
    raise RuntimeError("Identitas bot belum siap untuk upload Telegram.")


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


# ── Helpers ──────────────────────────────────────────────────────────────────

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
    on_progress: async callable(text: str) — fungsi untuk update pesan status.
    Debounce: update maks 1x per 3 detik ATAU tiap lompatan 10%.
    Menampilkan: bar, persentase, ukuran, kecepatan, dan estimasi waktu selesai (ETA).
    """
    state = {
        "last_time": 0.0,
        "last_pct": -1,
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
            pct == state["last_pct"]
            or (now - state["last_time"] < 3.0 and pct - state["last_pct"] < 10)
        ):
            return
        state["last_time"] = now
        state["last_pct"] = pct

        # Hitung kecepatan rata-rata dan ETA
        elapsed = now - state["start_time"]
        speed_bps = current / elapsed if elapsed > 0.5 else 0.0
        remaining = total - current
        eta_str = _fmt_eta(remaining / speed_bps) if speed_bps > 0 else ""
        speed_str = _fmt_speed(speed_bps) if speed_bps > 0 else ""

        bar = _progress_bar(pct)
        size_str = _fmt_size(total_size) if total_size else _fmt_size(total)

        # Baris info: ukuran • kecepatan • ETA (tampilkan hanya jika tersedia)
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

_PEER_RESOLVE_TIMEOUT  = 20   # detik — batas waktu resolve peer & get_chat
_MSG_FETCH_TIMEOUT     = 25   # detik — batas waktu get_messages
_ACCESS_CHECK_TIMEOUT  = 12   # detik — batas waktu pre-flight cek akses channel
_DOWNLOAD_TIMEOUT      = 120  # detik — batas waktu download satu file via Pyrogram (2 menit)
_DOWNLOAD_STALL_TIMEOUT = 45  # detik tanpa byte baru sebelum transfer dibatalkan
_UPLOAD_TIMEOUT        = 300  # detik — batas waktu upload satu file ke Bot API (5 menit)
_ALBUM_FETCH_TIMEOUT   = 30   # detik — batas waktu mengambil metadata album
_ALBUM_UPLOAD_TIMEOUT_PER_FILE = 120  # detik per file — dipakai di _send_album_via_bot
_BOT_COPY_TIMEOUT      = 30   # detik — jalur cepat untuk pesan channel publik
_PROGRESS_CALLBACK_TIMEOUT = 5  # update status tidak boleh menahan transfer
_TRANSFER_POLL_INTERVAL = 2  # detik — frekuensi pemeriksaan watchdog transfer

# Timeout PTB untuk operasi upload ke Bot API
_PTB_WRITE_TIMEOUT   = 90    # detik
_PTB_READ_TIMEOUT    = 60    # detik
_PTB_CONNECT_TIMEOUT = 15    # detik


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


async def _prepare_bot_peer(client):
    """
    Resolve peer bot sebelum transfer media dimulai.

    Upload sebelumnya memakai username langsung di dalam send_video(). Pada
    session user yang hanya connect() (tanpa start()), resolusi peer dapat
    berhenti sebelum callback upload pertama. Resolve sekali dengan timeout dan
    gunakan ID numerik yang sudah masuk peer cache Pyrogram.
    """
    # Username adalah cara paling aman untuk mengisi peer cache session user.
    # Setelah resolve sukses, ID numerik dapat dipakai tanpa lookup jaringan
    # tambahan saat send_* dimulai.
    if _BOT_USERNAME:
        username_peer = f"@{_BOT_USERNAME}"
        try:
            await _hard_timeout(
                client.resolve_peer(username_peer),
                timeout=_PEER_RESOLVE_TIMEOUT,
                operation=f"resolve bot peer {username_peer}",
            )
            return _BOT_USER_ID or username_peer
        except Exception:
            # Jika username tidak bisa di-resolve, coba ID yang sudah diberikan
            # oleh get_me(). Error terakhir tetap dilaporkan bila keduanya gagal.
            if not _BOT_USER_ID:
                raise

    if _BOT_USER_ID:
        await _hard_timeout(
            client.resolve_peer(_BOT_USER_ID),
            timeout=_PEER_RESOLVE_TIMEOUT,
            operation=f"resolve bot peer {_BOT_USER_ID}",
        )
        return _BOT_USER_ID

    raise RuntimeError("Identitas bot belum siap untuk upload Telegram.")


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
    return await _run_transfer_with_watchdog(
        lambda transfer_progress: client.download_media(
            media,
            file_name=file_name,
            progress=transfer_progress,
        ),
        timeout=timeout,
        operation=operation,
        progress=progress,
    )


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
        # Jika cek gagal karena alasan lain (misal network), biarkan lanjut —
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
            logger.info(f"Chat {chat} memiliki noforwards aktif — pakai strategi download+upload")
        return restricted
    except asyncio.TimeoutError:
        logger.warning(f"Timeout get_chat({chat}) saat cek noforwards — anggap tidak restricted")
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
        result = await _hard_timeout(
            client.invoke(
                raw.functions.channels.GetMessages(
                    channel=peer,
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
    """Ambil pesan dengan raw API dan fallback wrapper untuk kompatibilitas."""
    try:
        return await _get_message_via_raw_api(client, chat, msg_id)
    except asyncio.TimeoutError:
        raise
    except (MessageIdInvalid, MsgIdInvalid):
        raise
    except _PEER_ERRORS:
        raise
    except Exception as raw_error:
        logger.warning(
            "Raw get message gagal untuk %s/%s, coba wrapper Pyrogram: %s",
            chat, msg_id, raw_error,
        )
        return await _hard_timeout(
            client.get_messages(chat, msg_id),
            timeout=_MSG_FETCH_TIMEOUT,
            operation=f"get_messages({chat}, {msg_id})",
        )


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


async def _send_downloaded_file_via_bot(
    bot, msg, user_chat_id: int, path: str, on_progress=None,
):
    """
    Kirim file yang sudah ada ke user lewat Bot API.

    Video sengaja dikirim sebagai document pada jalur ini. Itu menghindari
    tahap pemrosesan video Bot API yang tidak diperlukan untuk mengambil media
    dari channel dan membuat upload 30–50 MB lebih deterministik.
    """
    await _notify_progress(on_progress, "📤 <b>Mengirim media via Bot API...</b>")
    caption = _build_caption(msg.caption or "")
    kw = dict(
        write_timeout=_PTB_WRITE_TIMEOUT,
        read_timeout=_PTB_READ_TIMEOUT,
        connect_timeout=_PTB_CONNECT_TIMEOUT,
    )

    with open(path, "rb") as media_file:
        if msg.photo:
            await asyncio.wait_for(
                bot.send_photo(
                    user_chat_id, photo=media_file, caption=caption, **kw
                ),
                timeout=_UPLOAD_TIMEOUT,
            )
        elif msg.audio:
            await asyncio.wait_for(
                bot.send_audio(
                    user_chat_id, audio=media_file, caption=caption, **kw
                ),
                timeout=_UPLOAD_TIMEOUT,
            )
        elif msg.voice:
            await asyncio.wait_for(
                bot.send_voice(
                    user_chat_id, voice=media_file, caption=caption, **kw
                ),
                timeout=_UPLOAD_TIMEOUT,
            )
        elif msg.video_note:
            await asyncio.wait_for(
                bot.send_video_note(
                    user_chat_id, video_note=media_file, **kw
                ),
                timeout=_UPLOAD_TIMEOUT,
            )
        elif msg.animation:
            await asyncio.wait_for(
                bot.send_animation(
                    user_chat_id, animation=media_file, caption=caption, **kw
                ),
                timeout=_UPLOAD_TIMEOUT,
            )
        else:
            await asyncio.wait_for(
                bot.send_document(
                    user_chat_id, document=media_file, caption=caption, **kw
                ),
                timeout=_UPLOAD_TIMEOUT,
            )


async def _send_downloaded_file_via_pyrogram(
    client, msg, path: str, file_size: int, on_progress=None,
):
    """Upload file lokal via MTProto setelah peer bot dipastikan siap."""
    await _notify_progress(on_progress, "📤 <b>Mengirim media via MTProto...</b>")
    bot_peer = await _prepare_bot_peer(client)
    show_progress = on_progress and file_size >= _PROGRESS_MIN_BYTES
    ul_cb = (
        _make_pyrogram_progress(on_progress, "Mengirim", file_size)
        if show_progress else None
    )
    caption = _build_caption(msg.caption or "")
    metadata = _video_metadata(msg)

    if msg.photo:
        send = lambda progress: client.send_photo(
            bot_peer, path, caption=caption, progress=progress,
        )
        operation = "Pyrogram send_photo"
    elif msg.video:
        send = lambda progress: client.send_video(
            bot_peer,
            path,
            caption=caption,
            supports_streaming=True,
            thumb=None,
            progress=progress,
            **metadata,
        )
        operation = "Pyrogram send_video"
    elif msg.audio:
        send = lambda progress: client.send_audio(
            bot_peer, path, caption=caption, progress=progress,
        )
        operation = "Pyrogram send_audio"
    elif msg.voice:
        send = lambda progress: client.send_voice(
            bot_peer, path, caption=caption, progress=progress,
        )
        operation = "Pyrogram send_voice"
    elif msg.video_note:
        send = lambda progress: client.send_video_note(
            bot_peer, path, progress=progress,
        )
        operation = "Pyrogram send_video_note"
    elif msg.animation:
        send = lambda progress: client.send_animation(
            bot_peer, path, caption=caption, progress=progress,
        )
        operation = "Pyrogram send_animation"
    elif msg.sticker:
        send = lambda progress: client.send_sticker(
            bot_peer, path, progress=progress,
        )
        operation = "Pyrogram send_sticker"
    else:
        send = lambda progress: client.send_document(
            bot_peer, path, caption=caption, progress=progress,
        )
        operation = "Pyrogram send_document"

    await _run_transfer_with_watchdog(
        send,
        timeout=_UPLOAD_TIMEOUT,
        operation=operation,
        progress=ul_cb,
    )


async def _download_and_send_via_bot(client, bot, msg, user_chat_id: int,
                                     on_progress=None):
    """
    Download media via Pyrogram, lalu kirim ke user via PTB bot.
    Menggunakan file object (bukan bytes) agar tidak OOM untuk file besar.
    Hanya aman untuk file ≤50 MB (batas upload Bot API).
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
        # Thumbnail tidak wajib. FFmpeg dapat menahan worker setelah download
        # selesai, terutama pada video dengan metadata/container yang rusak.
        thumbnail_path = None
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


async def _send_album_via_bot(client, bot, chat, msg_id: int, user_chat_id: int,
                              on_progress=None, is_premium: bool = False,
                              messages=None):
    """
    Download seluruh album via Pyrogram, lalu kirim sebagai media group via PTB bot.
    File object tetap terbuka hingga send_media_group selesai, lalu ditutup & dihapus.
    on_progress: async callable(text: str) untuk update status (opsional).
    """
    if messages is None:
        try:
                msgs = await _hard_timeout(
                    client.get_media_group(chat, msg_id),
                    timeout=_ALBUM_FETCH_TIMEOUT,
                    operation=f"get_media_group({chat}, {msg_id})",
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                "Timeout saat mengambil metadata album dari Telegram."
            )
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
    media_items         = []

    try:
        for i, m in enumerate(msgs):
            file_size = _get_file_size(m) or 0
            # Timeout dinamis: min 60 detik, +30 detik per 10 MB
            dl_timeout = max(60, 30 + (file_size // (10 * 1024 * 1024)) * 30)
            # Callback progress per-file (hanya untuk file ≥ PROGRESS_MIN_BYTES)
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
                logger.error(f"Skip album item {i + 1}/{total} msg {m.id} setelah 2 percobaan.")
                shutil.rmtree(item_dir, ignore_errors=True)
                continue
            paths.append(path)
            download_dirs.append(item_dir)

            caption = _build_caption(m.caption or "") if i == 0 else ""
            f       = open(path, "rb")  # noqa: WPS515 — ditutup di finally
            handles.append(f)

            if m.photo:
                media_items.append(InputMediaPhoto(media=f, caption=caption))
            elif m.video:
                # Thumbnail bersifat opsional dan tidak boleh menghambat
                # pengiriman album setelah file berhasil di-download.
                media_items.append(
                    InputMediaVideo(
                        media=f,
                        caption=caption,
                        supports_streaming=True,
                        **_video_metadata(m),
                    )
                )
            elif m.audio:
                media_items.append(InputMediaAudio(media=f, caption=caption))
            elif m.animation:
                media_items.append(InputMediaAnimation(media=f, caption=caption))
            else:
                media_items.append(InputMediaDocument(media=f, caption=caption))

        if len(paths) != total:
            raise RuntimeError(
                f"Album tidak lengkap: hanya {len(paths)}/{total} media berhasil diunduh."
            )

        if media_items:
            if on_progress:
                await _notify_progress(
                    on_progress,
                    f"📤 <b>Mengirim album...</b> ({len(paths)}/{total})",
                )
            # Timeout proporsional: 120 detik per file + 60 detik buffer
            _album_timeout = len(paths) * _ALBUM_UPLOAD_TIMEOUT_PER_FILE + 60
            await asyncio.wait_for(
                bot.send_media_group(
                    user_chat_id,
                    media=media_items,
                    write_timeout=_PTB_WRITE_TIMEOUT,
                    read_timeout=_PTB_READ_TIMEOUT,
                    connect_timeout=_PTB_CONNECT_TIMEOUT,
                ),
                timeout=_album_timeout,
            )
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
    bot_peer = await _prepare_bot_peer(client)
    await _hard_timeout(
        msg.copy(bot_peer),
        timeout=_BOT_COPY_TIMEOUT,
        operation=f"copy message {getattr(msg, 'id', '?')}",
    )


async def _download_and_upload_via_pyrogram(client, bot, msg, user_chat_id: int,
                                            file_size: int, on_progress=None):
    """
    Untuk media dari channel restricted/private:
    Download file via Pyrogram lalu upload ulang langsung ke chat bot user via MTProto.
    Jalur MTProto diprioritaskan agar upload 30–50 MB tidak bergantung pada
    multipart Bot API yang tidak menyediakan progress. Bot API hanya fallback
    jika request MTProto ditolak secara eksplisit dan file masih ≤50 MB.
    on_progress: async callable(text: str) untuk update pesan status (opsional).
    """
    show_progress = on_progress and file_size >= _PROGRESS_MIN_BYTES
    dl_cb = _make_pyrogram_progress(on_progress, "Mengunduh", file_size) if show_progress else None
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

        # Untuk channel restricted/private, MTProto adalah jalur utama. Bot API
        # tidak memberi progress upload dan sebelumnya membuat job tampak
        # berhenti pada "Mengirim media via Bot API...".
        try:
            await _send_downloaded_file_via_pyrogram(
                client, msg, path, file_size, on_progress=on_progress,
            )
            return
        except FloodWait:
            # Biarkan handler SafeForward utama mengatur jeda rate-limit.
            raise
        except (BadRequest, Forbidden, RPCError) as exc:
            # Request MTProto ditolak secara eksplisit, jadi aman mencoba
            # Bot API. Timeout tidak masuk fallback karena bisa saja pesan
            # sebenarnya sudah diterima Telegram dan retry akan menggandakan.
            if not file_size or file_size > _BOT_API_UPLOAD_LIMIT:
                raise
            logger.warning(
                "MTProto upload ditolak untuk msg %s, fallback ke Bot API: %s",
                getattr(msg, "id", "?"),
                exc,
            )
            await _notify_progress(
                on_progress,
                "📤 <b>Jalur MTProto ditolak, mencoba Bot API...</b>",
            )
            await _send_downloaded_file_via_bot(
                bot, msg, user_chat_id, path, on_progress=on_progress,
            )
            return

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
) -> None:
    """Kirim satu item album melalui jalur yang sesuai dengan ukuran file."""
    caption = _build_caption(msg.caption or "")
    file_size = _get_file_size(msg) or 0
    bot_peer = f"@{_BOT_USERNAME}" if _BOT_USERNAME else user_chat_id
    # Thumbnail tidak wajib; jalur fallback harus fokus mengirim media.
    thumbnail_path = None
    metadata = _video_metadata(msg)
    _kw = dict(
        write_timeout=_PTB_WRITE_TIMEOUT,
        read_timeout=_PTB_READ_TIMEOUT,
        connect_timeout=_PTB_CONNECT_TIMEOUT,
    )

    try:
        if file_size > _BOT_API_UPLOAD_LIMIT:
            if msg.photo:
                await _hard_timeout(
                    client.send_photo(bot_peer, path, caption=caption),
                    timeout=_UPLOAD_TIMEOUT,
                    operation="Pyrogram album send_photo",
                )
            elif msg.video:
                await _hard_timeout(
                    client.send_video(
                        bot_peer,
                        path,
                        caption=caption,
                        supports_streaming=True,
                        thumb=thumbnail_path,
                        **metadata,
                    ),
                    timeout=_UPLOAD_TIMEOUT,
                    operation="Pyrogram album send_video",
                )
            elif msg.audio:
                await _hard_timeout(
                    client.send_audio(bot_peer, path, caption=caption),
                    timeout=_UPLOAD_TIMEOUT,
                    operation="Pyrogram album send_audio",
                )
            elif msg.voice:
                await _hard_timeout(
                    client.send_voice(bot_peer, path, caption=caption),
                    timeout=_UPLOAD_TIMEOUT,
                    operation="Pyrogram album send_voice",
                )
            elif msg.video_note:
                await _hard_timeout(
                    client.send_video_note(bot_peer, path),
                    timeout=_UPLOAD_TIMEOUT,
                    operation="Pyrogram album send_video_note",
                )
            elif msg.animation:
                await _hard_timeout(
                    client.send_animation(bot_peer, path, caption=caption),
                    timeout=_UPLOAD_TIMEOUT,
                    operation="Pyrogram album send_animation",
                )
            else:
                await _hard_timeout(
                    client.send_document(bot_peer, path, caption=caption),
                    timeout=_UPLOAD_TIMEOUT,
                    operation="Pyrogram album send_document",
                )
            return

        with open(path, "rb") as f:
            if msg.photo:
                await asyncio.wait_for(
                    bot.send_photo(user_chat_id, photo=f, caption=caption, **_kw),
                    timeout=_UPLOAD_TIMEOUT,
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
                await asyncio.wait_for(
                    bot.send_audio(user_chat_id, audio=f, caption=caption, **_kw),
                    timeout=_UPLOAD_TIMEOUT,
                )
            elif msg.voice:
                await asyncio.wait_for(
                    bot.send_voice(user_chat_id, voice=f, caption=caption, **_kw),
                    timeout=_UPLOAD_TIMEOUT,
                )
            elif msg.animation:
                await asyncio.wait_for(
                    bot.send_animation(user_chat_id, animation=f, caption=caption, **_kw),
                    timeout=_UPLOAD_TIMEOUT,
                )
            else:
                await asyncio.wait_for(
                    bot.send_document(user_chat_id, document=f, caption=caption, **_kw),
                    timeout=_UPLOAD_TIMEOUT,
                )
    finally:
        if thumbnail_path:
            try:
                os.remove(thumbnail_path)
            except Exception:
                pass


async def _send_album_individually(
    client, bot, chat, msg_id: int, user_chat_id: int,
    on_progress=None, is_premium: bool = False,
) -> tuple[bool, str | None]:
    """
    Fallback album: download semua file lalu coba kirim sebagai album (send_media_group).
    Jika album gagal (misal file terlalu besar / error PTB), kirim satu per satu.
    TIDAK menggunakan copy/forward — semua file didownload fresh agar bypass restriction.
    on_progress: async callable(text: str) untuk update status (opsional).
    """
    try:
        msgs = await _hard_timeout(
            client.get_media_group(chat, msg_id),
            timeout=_ALBUM_FETCH_TIMEOUT,
            operation=f"get_media_group({chat}, {msg_id})",
        )
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
                await _send_album_item(client, bot, m, path, user_chat_id)
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


async def _fetch_album_messages(client, chat, msg_id: int):
    """Ambil metadata album sekali dengan batas waktu yang tegas."""
    try:
        return await _hard_timeout(
            client.get_media_group(chat, msg_id),
            timeout=_ALBUM_FETCH_TIMEOUT,
            operation=f"get_media_group({chat}, {msg_id})",
        )
    except asyncio.TimeoutError:
        raise RuntimeError(
            "Timeout saat mengambil metadata album dari Telegram."
        )


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


# ── SafeForward ───────────────────────────────────────────────────────────────

class SafeForward:

    @staticmethod
    async def run_album(
        client, bot, user_chat_id: int, chat, msg_id: int,
        on_progress=None, is_premium: bool = False,
    ) -> tuple[bool, str | None]:
        """
        Kirim seluruh album yang mengandung `msg_id` ke `user_chat_id`.

        Strategi pengiriman:
          0. Deteksi noforwards (has_protected_content) — jika aktif, langsung ke (2)
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

        # ── Deteksi noforwards sebelum mencoba forward/copy ───────────────
        await _notify_progress(on_progress, "🔎 <b>Memeriksa akses media...</b>")
        if await _is_forwards_restricted(client, source_chat):
            return await _send_album_individually(
                client, bot, source_chat, msg_id, user_chat_id,
                on_progress=on_progress, is_premium=is_premium,
            )

        for attempt in range(MAX_RETRIES + 1):
            try:
                await _notify_progress(on_progress, "📥 <b>Mengambil album...</b>")
                album_messages = await _fetch_album_messages(
                    client, source_chat, msg_id
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
                    # (JANGAN gunakan copy_media_group — akan gagal di channel restricted)
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
          0. Deteksi noforwards (has_protected_content) — jika aktif, pakai download+upload
          • Fast path (bot.copy_message): tanpa download, bebas ukuran, untuk channel terbuka
          • Slow path: download via Pyrogram → upload via MTProto with progress
            (Bot API hanya fallback jika MTProto ditolak dan file ≤50 MB)
          • Fallback >50 MB private terbuka: Pyrogram copy → Saved Messages + notifikasi
          • Fallback >50 MB restricted: tidak bisa dikirim (Bot API limit)
        on_progress: async callable(text: str) untuk update progress ke user (opsional).
        """
        # ── Jalur cepat untuk channel publik ──────────────────────────────
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

        # ── Langkah 1: Pastikan source bisa diakses ──────────────────────
        await _notify_progress(on_progress, "🔌 <b>Menghubungkan ke channel...</b>")
        source_chat, src_err = await _resolve_source(client, chat)
        if src_err:
            return False, src_err

        # ── Deteksi noforwards sebelum fetch pesan ────────────────────────
        await _notify_progress(on_progress, "🔎 <b>Memeriksa akses media...</b>")
        is_restricted = await _is_forwards_restricted(client, source_chat)

        # ── Langkah 2: Ambil pesan ───────────────────────────────────────
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

        # ── Auto-deteksi album ────────────────────────────────────────────
        if msg.media_group_id and not single_only:
            return await SafeForward.run_album(
                client, bot, user_chat_id, chat, msg_id,
                on_progress=on_progress, is_premium=is_premium,
            )

        # ── Langkah 3: Cek ukuran file terhadap hard limit ───────────────
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

        # ── Langkah 4: Kirim ke user (dengan retry) ──────────────────────
        for attempt in range(MAX_RETRIES + 1):
            try:
                if msg.media:
                    if is_restricted:
                        # Channel noforwards: setelah download, selalu upload ulang
                        # lewat Pyrogram MTProto. Bot API multipart sering macet pada
                        # video 30–50 MB walaupun masih di bawah batas 50 MB.
                        await _download_and_upload_via_pyrogram(
                            client, bot, msg, user_chat_id, file_size,
                            on_progress=on_progress,
                        )
                        return True, None
                    else:
                        # Untuk link private yang tidak dilindungi, Telegram bisa
                        # menyalin media di server tanpa download + upload ulang.
                        # Ini menghilangkan titik macet transfer 41 MB di Railway.
                        if isinstance(chat, int):
                            try:
                                await _pyrogram_copy_with_notice(
                                    client, bot, msg, user_chat_id, file_size
                                )
                                return True, None
                            except (BadRequest, Forbidden, asyncio.TimeoutError) as exc:
                                logger.info(
                                    "Server-side copy gagal untuk msg %s, "
                                    "lanjut ke jalur upload: %s",
                                    msg_id,
                                    exc,
                                )

                        # Fast path: PTB bot.copy_message
                        # Tidak ada batasan ukuran (file tidak di-download),
                        # tidak masuk Saved Messages karena dikirim dari bot.
                        try:
                            await _hard_timeout(
                                bot.copy_message(
                                    chat_id=user_chat_id,
                                    from_chat_id=chat,
                                    message_id=msg_id,
                                    write_timeout=_PTB_WRITE_TIMEOUT,
                                    read_timeout=_PTB_READ_TIMEOUT,
                                    connect_timeout=_PTB_CONNECT_TIMEOUT,
                                ),
                                timeout=_BOT_COPY_TIMEOUT,
                                operation=f"copy_message({chat}, {msg_id})",
                            )
                            return True, None
                        except (BadRequest, Forbidden, asyncio.TimeoutError):
                            # Bot tidak bisa akses source (private / restricted)
                            if is_large:
                                # File >50 MB — tidak bisa di-re-upload via Bot API
                                # Pyrogram copy langsung ke Saved Messages + notifikasi
                                await _pyrogram_copy_with_notice(
                                    client, bot, msg, user_chat_id, file_size
                                )
                                return True, None
                            else:
                                # Jika copy bot gagal, gunakan jalur MTProto
                                # yang sama agar upload tidak tersangkut Bot API.
                                await _download_and_upload_via_pyrogram(
                                    client, bot, msg, user_chat_id, file_size,
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

            except asyncio.TimeoutError as e:
                # Jangan mengulang seluruh download setelah transfer Telegram
                # benar-benar timeout. Pada file besar, retry seperti ini hanya
                # membuat user menunggu berulang kali dan dapat memperpanjang
                # job sampai timeout queue tanpa hasil baru.
                logger.error(
                    "transfer timeout msg %s attempt %s: %s",
                    msg_id,
                    attempt + 1,
                    e,
                )
                return False, "Transfer Telegram timeout — coba lagi beberapa saat lagi."

            except RuntimeError as e:
                error_text = str(e)
                if "timeout" in error_text.lower():
                    # Kedua helper upload membungkus timeout agar pesan user
                    # mudah dipahami. Perlakukan timeout sebagai hasil akhir,
                    # bukan alasan mengunduh ulang media dari awal.
                    logger.error(
                        "transfer runtime timeout msg %s attempt %s: %s",
                        msg_id,
                        attempt + 1,
                        error_text,
                    )
                    return False, error_text
                logger.error(f"send error msg {msg_id} attempt {attempt}: {e}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1 + random.uniform(0, 1))
                else:
                    return False, f"Gagal mengirim: {e}"

            except Exception as e:
                logger.error(f"send error msg {msg_id} attempt {attempt}: {e}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1 + random.uniform(0, 1))
                else:
                    return False, f"Gagal mengirim: {e}"

        return False, "Gagal setelah beberapa percobaan."
