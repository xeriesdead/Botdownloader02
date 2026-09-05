import asyncio
import os
import random
import shutil
import subprocess
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

# Username bot — diset sekali saat startup via set_bot_username()
_BOT_USERNAME: str = ""


def set_bot_username(username: str, bot_id: int | None = None):
    """Store bot identity; bot_id is accepted for compatibility with newer main.py."""
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
            await on_progress(text)
        except Exception:
            pass

    return _cb

_PEER_RESOLVE_TIMEOUT  = 20   # detik — batas waktu resolve peer & get_chat
_MSG_FETCH_TIMEOUT     = 25   # detik — batas waktu get_messages
_ACCESS_CHECK_TIMEOUT  = 12   # detik — batas waktu pre-flight cek akses channel
_DOWNLOAD_TIMEOUT      = 120  # detik — batas waktu download satu file via Pyrogram (2 menit)
_UPLOAD_TIMEOUT        = 300  # detik — batas waktu upload satu file ke Bot API (5 menit)
_ALBUM_UPLOAD_TIMEOUT_PER_FILE = 120  # detik per file — dipakai di _send_album_via_bot

# Timeout PTB untuk operasi upload ke Bot API
_PTB_WRITE_TIMEOUT   = 90    # detik
_PTB_READ_TIMEOUT    = 60    # detik
_PTB_CONNECT_TIMEOUT = 15    # detik


async def check_channel_access(client, chat) -> tuple[bool, str]:
    """
    Pre-flight: cek apakah client bisa mengakses channel/grup.
    Dipanggil SEBELUM quota dipotong agar user tidak kehilangan quota
    jika akun belum bergabung ke channel target.

    Return (True, "") jika bisa diakses, (False, pesan_error) jika tidak.
    """
    label = f"ID {chat}" if isinstance(chat, int) else str(chat)
    try:
        await asyncio.wait_for(client.get_chat(chat), timeout=_ACCESS_CHECK_TIMEOUT)
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


async def _hard_timeout(awaitable, timeout: float, operation: str = "operation"):
    """Wait for an async operation and preserve TimeoutError for callers."""
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("%s timed out after %ss", operation, timeout)
        raise


async def copy_public_message(
    bot,
    user_chat_id: int,
    source_chat,
    message_id: int,
    on_progress=None,
) -> bool:
    """
    Fast path for public Telegram posts.

    Return False instead of raising when Bot API cannot copy the source, so the
    caller can fall back to the logged-in Pyrogram session.
    """
    if on_progress:
        try:
            await on_progress("📤 <b>Mengirim media...</b>")
        except Exception:
            pass
    try:
        await asyncio.wait_for(
            bot.copy_message(
                chat_id=user_chat_id,
                from_chat_id=source_chat,
                message_id=message_id,
                write_timeout=_PTB_WRITE_TIMEOUT,
                read_timeout=_PTB_READ_TIMEOUT,
                connect_timeout=_PTB_CONNECT_TIMEOUT,
            ),
            timeout=_UPLOAD_TIMEOUT,
        )
        return True
    except Exception as exc:
        logger.info(
            "Bot API copy gagal untuk %s/%s, gunakan fallback Pyrogram: %s",
            source_chat,
            message_id,
            exc,
        )
        return False


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
        chat_obj = await asyncio.wait_for(
            client.get_chat(chat), timeout=_PEER_RESOLVE_TIMEOUT
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
    """Resolve source peer. Return (peer, None) atau (None, error_msg)."""
    label = chat if isinstance(chat, str) else f"ID {chat}"
    try:
        peer = await asyncio.wait_for(
            client.resolve_peer(chat), timeout=_PEER_RESOLVE_TIMEOUT
        )
        return peer, None
    except asyncio.TimeoutError:
        logger.warning(f"Timeout resolve_peer({chat})")
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
        logger.warning(f"resolve_peer({chat}) error: {e}")
        return None, f"Gagal resolve peer: {e}"


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


def _is_video_media(msg, path: str | None = None) -> bool:
    """True for native videos and documents whose MIME/extension is video."""
    if getattr(msg, "video", None):
        return True
    document = getattr(msg, "document", None)
    mime_type = (getattr(document, "mime_type", None) or "").lower()
    if mime_type.startswith("video/"):
        return True
    if path:
        return os.path.splitext(path)[1].lower() in {
            ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv",
        }
    return False


def _make_video_thumbnail(path: str) -> str | None:
    """
    Create a small JPEG thumbnail from a video.

    Thumbnail generation is best-effort: if ffmpeg is unavailable or a video
    cannot be decoded, the original media is still sent without a thumbnail.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.warning("ffmpeg tidak tersedia — kirim video tanpa thumbnail")
        return None

    thumb_path = f"{path}.thumb.jpg"
    scale = "scale=320:320:force_original_aspect_ratio=decrease"
    commands = (
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-ss", "1", "-i", path,
            "-frames:v", "1", "-vf", scale,
            "-q:v", "5", thumb_path,
        ],
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-ss", "0", "-i", path,
            "-frames:v", "1", "-vf", scale,
            "-q:v", "5", thumb_path,
        ],
    )
    for command in commands:
        try:
            subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=20,
            )
            if os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 0:
                return thumb_path
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Gagal membuat thumbnail %s: %s", path, exc)
            break
    try:
        os.remove(thumb_path)
    except OSError:
        pass
    return None


def _fmt_size(size_bytes: int) -> str:
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"


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
    try:
        path = await asyncio.wait_for(
            client.download_media(msg, progress=dl_cb),
            timeout=_DOWNLOAD_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise RuntimeError("Download timeout — file terlalu lama diunduh, coba lagi.")
    if not path:
        raise RuntimeError("Download gagal, file tidak tersedia.")

    thumb_path = _make_video_thumbnail(path) if _is_video_media(msg, path) else None
    caption = _build_caption(msg.caption or "")
    _kw = dict(
        write_timeout=_PTB_WRITE_TIMEOUT,
        read_timeout=_PTB_READ_TIMEOUT,
        connect_timeout=_PTB_CONNECT_TIMEOUT,
    )
    try:
        if msg.photo:
            with open(path, "rb") as f:
                await asyncio.wait_for(
                    bot.send_photo(user_chat_id, photo=f, caption=caption, **_kw),
                    timeout=_UPLOAD_TIMEOUT,
                )
        elif _is_video_media(msg, path):
            with open(path, "rb") as f:
                thumb_file = open(thumb_path, "rb") if thumb_path else None
                try:
                    video_kw = dict(_kw)
                    if thumb_file:
                        video_kw["thumbnail"] = thumb_file
                    await asyncio.wait_for(
                        bot.send_video(
                            user_chat_id, video=f, caption=caption, **video_kw
                        ),
                        timeout=_UPLOAD_TIMEOUT,
                    )
                finally:
                    if thumb_file:
                        thumb_file.close()
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
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
        if thumb_path:
            try:
                os.remove(thumb_path)
            except Exception:
                pass


async def _send_album_via_bot(client, bot, chat, msg_id: int, user_chat_id: int,
                              on_progress=None):
    """
    Download seluruh album via Pyrogram, lalu kirim sebagai media group via PTB bot.
    File object tetap terbuka hingga send_media_group selesai, lalu ditutup & dihapus.
    on_progress: async callable(text: str) untuk update status (opsional).
    """
    msgs  = await client.get_media_group(chat, msg_id)
    total = len(msgs)
    paths: list[str]    = []
    handles: list       = []
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
                try:
                    await on_progress(
                        f"📥 <b>Mengunduh album...</b> ({i + 1}/{total})"
                    )
                except Exception:
                    pass
            path = None
            for _dl_attempt in range(2):
                try:
                    path = await asyncio.wait_for(
                        client.download_media(m, progress=dl_cb),
                        timeout=dl_timeout,
                    )
                    if path:
                        break
                except (asyncio.TimeoutError, Exception) as _dl_err:
                    logger.warning(
                        f"Download album item {i + 1}/{total} msg {m.id} "
                        f"attempt {_dl_attempt + 1} gagal: {_dl_err}"
                    )
                    if _dl_attempt == 0:
                        await asyncio.sleep(2)
            if not path:
                logger.error(f"Skip album item {i + 1}/{total} msg {m.id} setelah 2 percobaan.")
                continue
            paths.append(path)

            caption = _build_caption(m.caption or "") if i == 0 else ""
            f       = open(path, "rb")  # noqa: WPS515 — ditutup di finally
            handles.append(f)

            if m.photo:
                media_items.append(InputMediaPhoto(media=f, caption=caption))
            elif m.video:
                media_items.append(InputMediaVideo(media=f, caption=caption))
            elif m.audio:
                media_items.append(InputMediaAudio(media=f, caption=caption))
            elif m.animation:
                media_items.append(InputMediaAnimation(media=f, caption=caption))
            else:
                media_items.append(InputMediaDocument(media=f, caption=caption))

        if media_items:
            if on_progress:
                try:
                    await on_progress(f"📤 <b>Mengirim album...</b> ({len(paths)}/{total})")
                except Exception:
                    pass
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
        for p in paths:
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
    await msg.copy(bot_peer)


async def _download_and_upload_via_pyrogram(client, bot, msg, user_chat_id: int,
                                            file_size: int, on_progress=None):
    """
    Untuk media dari channel private/restricted:
    Download file via Pyrogram lalu upload ulang langsung ke chat bot user via MTProto.
    Ini menghindari batas upload Bot API dan larangan forward/copy dari channel
    restricted. File bisa sampai batas MAX_FILE_SIZE_BYTES di config.
    on_progress: async callable(text: str) untuk update pesan status (opsional).
    """
    show_progress = on_progress and file_size >= _PROGRESS_MIN_BYTES
    dl_cb = _make_pyrogram_progress(on_progress, "Mengunduh", file_size) if show_progress else None

    try:
        path = await asyncio.wait_for(
            client.download_media(msg, progress=dl_cb),
            timeout=_DOWNLOAD_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise RuntimeError("Download timeout — file terlalu lama diunduh, coba lagi.")
    if not path:
        raise RuntimeError("Download gagal, file tidak tersedia.")

    thumb_path = _make_video_thumbnail(path) if _is_video_media(msg, path) else None

    # Kirim ke chat bot (bukan Saved Messages).
    # Dari sudut pandang Pyrogram (login sebagai user), mengirim ke @bot_username
    # membuat file muncul langsung di chat antara user dan bot.
    bot_peer = f"@{_BOT_USERNAME}" if _BOT_USERNAME else user_chat_id

    ul_cb = (
        _make_pyrogram_progress(on_progress, "Mengirim via MTProto", file_size)
        if show_progress
        else None
    )
    caption = _build_caption(msg.caption or "")
    try:
        if msg.photo:
            await asyncio.wait_for(
                client.send_photo(bot_peer, path, caption=caption, progress=ul_cb),
                timeout=_UPLOAD_TIMEOUT,
            )
        elif _is_video_media(msg, path):
            video_kw = {
                "caption": caption,
                "supports_streaming": True,
                "progress": ul_cb,
            }
            if thumb_path:
                video_kw["thumb"] = thumb_path
            await asyncio.wait_for(
                client.send_video(bot_peer, path, **video_kw),
                timeout=_UPLOAD_TIMEOUT,
            )
        elif msg.audio:
            await asyncio.wait_for(
                client.send_audio(bot_peer, path, caption=caption, progress=ul_cb),
                timeout=_UPLOAD_TIMEOUT,
            )
        elif msg.voice:
            await asyncio.wait_for(
                client.send_voice(bot_peer, path, caption=caption, progress=ul_cb),
                timeout=_UPLOAD_TIMEOUT,
            )
        elif msg.video_note:
            await asyncio.wait_for(
                client.send_video_note(bot_peer, path, progress=ul_cb),
                timeout=_UPLOAD_TIMEOUT,
            )
        elif msg.animation:
            await asyncio.wait_for(
                client.send_animation(bot_peer, path, caption=caption, progress=ul_cb),
                timeout=_UPLOAD_TIMEOUT,
            )
        elif msg.sticker:
            await asyncio.wait_for(
                client.send_sticker(bot_peer, path, progress=ul_cb),
                timeout=_UPLOAD_TIMEOUT,
            )
        else:
            await asyncio.wait_for(
                client.send_document(bot_peer, path, caption=caption, progress=ul_cb),
                timeout=_UPLOAD_TIMEOUT,
            )
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
        if thumb_path:
            try:
                os.remove(thumb_path)
            except Exception:
                pass


async def _send_album_individually(
    client, bot, chat, msg_id: int, user_chat_id: int,
    on_progress=None,
) -> tuple[bool, str | None]:
    """
    Fallback album: download semua file lalu coba kirim sebagai album (send_media_group).
    Jika album gagal (misal file terlalu besar / error PTB), kirim satu per satu.
    TIDAK menggunakan copy/forward — semua file didownload fresh agar bypass restriction.
    on_progress: async callable(text: str) untuk update status (opsional).
    """
    try:
        msgs = await client.get_media_group(chat, msg_id)
    except Exception as e:
        return False, f"Gagal mengambil album: {e}"

    if not msgs:
        return False, "Album kosong atau tidak ditemukan."

    total = len(msgs)

    # Download semua file terlebih dahulu
    paths: list[str] = []
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
            try:
                await on_progress(
                    f"📥 <b>Mengunduh album...</b> ({i + 1}/{total})"
                )
            except Exception:
                pass
        path = None
        for _dl_attempt in range(2):
            try:
                path = await asyncio.wait_for(
                    client.download_media(m, progress=dl_cb),
                    timeout=dl_timeout,
                )
                if path:
                    break
            except (asyncio.TimeoutError, Exception) as _dl_err:
                logger.warning(
                    f"Download album item {i + 1}/{total} msg {m.id} "
                    f"attempt {_dl_attempt + 1} gagal: {_dl_err}"
                )
                if _dl_attempt == 0:
                    await asyncio.sleep(2)
        if path:
            paths.append((m, path))
        else:
            logger.error(f"Skip album item {i + 1}/{total} msg {m.id} setelah 2 percobaan.")

    if not paths:
        return False, "Gagal mendownload semua file dalam album."

    # Kirim satu per satu dengan progress per file.
    # send_media_group sengaja dilewati di sini karena fungsi ini adalah fallback
    # path (channel restricted / setelah send_media_group utama gagal) dan
    # send_media_group untuk banyak file besar sering hang tanpa bisa dicancel.
    sent          = 0
    large_sent    = 0
    n_paths       = len(paths)
    for idx, (m, path) in enumerate(paths):
        if on_progress:
            try:
                await on_progress(
                    f"📤 <b>Mengirim satu per satu...</b> ({idx + 1}/{n_paths})"
                )
            except Exception:
                pass
        try:
            caption   = _build_caption(m.caption or "")
            file_size = _get_file_size(m) or 0
            bot_peer  = f"@{_BOT_USERNAME}" if _BOT_USERNAME else user_chat_id
            _kw = dict(
                write_timeout=_PTB_WRITE_TIMEOUT,
                read_timeout=_PTB_READ_TIMEOUT,
                connect_timeout=_PTB_CONNECT_TIMEOUT,
            )
            thumb_path = _make_video_thumbnail(path) if _is_video_media(m, path) else None
            if file_size > _BOT_API_UPLOAD_LIMIT:
                # File terlalu besar untuk Bot API — kirim langsung ke chat bot
                # via Pyrogram MTProto (bypass batas 50 MB, tanpa Saved Messages)
                if m.photo:
                    await asyncio.wait_for(
                        client.send_photo(bot_peer, path, caption=caption),
                        timeout=_UPLOAD_TIMEOUT,
                    )
                elif _is_video_media(m, path):
                    video_kw = {
                        "caption": caption,
                        "supports_streaming": True,
                    }
                    if thumb_path:
                        video_kw["thumb"] = thumb_path
                    await asyncio.wait_for(
                        client.send_video(bot_peer, path, **video_kw),
                        timeout=_UPLOAD_TIMEOUT,
                    )
                elif m.audio:
                    await asyncio.wait_for(
                        client.send_audio(bot_peer, path, caption=caption),
                        timeout=_UPLOAD_TIMEOUT,
                    )
                elif m.voice:
                    await asyncio.wait_for(
                        client.send_voice(bot_peer, path, caption=caption),
                        timeout=_UPLOAD_TIMEOUT,
                    )
                elif m.video_note:
                    await asyncio.wait_for(
                        client.send_video_note(bot_peer, path),
                        timeout=_UPLOAD_TIMEOUT,
                    )
                elif m.animation:
                    await asyncio.wait_for(
                        client.send_animation(bot_peer, path, caption=caption),
                        timeout=_UPLOAD_TIMEOUT,
                    )
                else:
                    await asyncio.wait_for(
                        client.send_document(bot_peer, path, caption=caption),
                        timeout=_UPLOAD_TIMEOUT,
                    )
                large_sent += 1
            else:
                with open(path, "rb") as f:
                    if m.photo:
                        await asyncio.wait_for(
                            bot.send_photo(user_chat_id, photo=f, caption=caption, **_kw),
                            timeout=_UPLOAD_TIMEOUT,
                        )
                    elif _is_video_media(m, path):
                        thumb_file = open(thumb_path, "rb") if thumb_path else None
                        try:
                            video_kw = dict(_kw)
                            if thumb_file:
                                video_kw["thumbnail"] = thumb_file
                            await asyncio.wait_for(
                                bot.send_video(
                                    user_chat_id,
                                    video=f,
                                    caption=caption,
                                    **video_kw,
                                ),
                                timeout=_UPLOAD_TIMEOUT,
                            )
                        finally:
                            if thumb_file:
                                thumb_file.close()
                    elif m.audio:
                        await asyncio.wait_for(
                            bot.send_audio(user_chat_id, audio=f, caption=caption, **_kw),
                            timeout=_UPLOAD_TIMEOUT,
                        )
                    elif m.voice:
                        await asyncio.wait_for(
                            bot.send_voice(user_chat_id, voice=f, caption=caption, **_kw),
                            timeout=_UPLOAD_TIMEOUT,
                        )
                    elif m.animation:
                        await asyncio.wait_for(
                            bot.send_animation(user_chat_id, animation=f, caption=caption, **_kw),
                            timeout=_UPLOAD_TIMEOUT,
                        )
                    else:
                        await asyncio.wait_for(
                            bot.send_document(user_chat_id, document=f, caption=caption, **_kw),
                            timeout=_UPLOAD_TIMEOUT,
                        )
            sent += 1
        except Exception as e:
            logger.warning(f"Gagal kirim file album msg {m.id}: {e}")
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
            if thumb_path:
                try:
                    os.remove(thumb_path)
                except Exception:
                    pass

    if sent == 0:
        return False, "Semua file dalam album gagal dikirim."

    return True, None


# ── SafeForward ───────────────────────────────────────────────────────────────

class SafeForward:

    @staticmethod
    async def run_album(
        client, bot, user_chat_id: int, chat, msg_id: int,
        on_progress=None,
        is_premium: bool = False,
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
        _, src_err = await _resolve_source(client, chat)
        if src_err:
            return False, src_err

        # ── Deteksi noforwards sebelum mencoba forward/copy ───────────────
        if await _is_forwards_restricted(client, chat):
            return await _send_album_individually(
                client, bot, chat, msg_id, user_chat_id, on_progress=on_progress
            )

        for attempt in range(MAX_RETRIES + 1):
            try:
                await _send_album_via_bot(
                    client, bot, chat, msg_id, user_chat_id, on_progress=on_progress
                )
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
                    client, bot, chat, msg_id, user_chat_id, on_progress=on_progress
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
                        client, bot, chat, msg_id, user_chat_id, on_progress=on_progress
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
          • Slow path ≤50 MB: download via Pyrogram → re-upload via PTB bot
          • Fallback >50 MB private terbuka: Pyrogram copy → Saved Messages + notifikasi
          • Fallback >50 MB restricted: tidak bisa dikirim (Bot API limit)
        on_progress: async callable(text: str) untuk update progress ke user (opsional).
        """
        # ── Langkah 1: Pastikan source bisa diakses ──────────────────────
        _, src_err = await _resolve_source(client, chat)
        if src_err:
            return False, src_err

        # ── Deteksi noforwards sebelum fetch pesan ────────────────────────
        is_restricted = await _is_forwards_restricted(client, chat)

        # ── Langkah 2: Ambil pesan ───────────────────────────────────────
        try:
            msg = await asyncio.wait_for(
                client.get_messages(chat, msg_id), timeout=_MSG_FETCH_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning(f"Timeout get_messages({chat}, {msg_id})")
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
            logger.warning(f"get_messages({chat}, {msg_id}) error: {e}")
            return False, f"Gagal mengambil pesan: {e}"

        if not msg or msg.empty:
            return False, f"Pesan `{msg_id}` kosong atau sudah dihapus."

        # ── Auto-deteksi album ────────────────────────────────────────────
        if msg.media_group_id and not single_only:
            return await SafeForward.run_album(
                client, bot, user_chat_id, chat, msg_id, on_progress=on_progress
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
                        # Channel noforwards: jangan gunakan Bot API untuk ukuran
                        # apa pun. Jalur ini download + upload lewat MTProto agar
                        # tidak macet pada media private/protected sekitar 40–50 MB.
                        await _download_and_upload_via_pyrogram(
                            client, bot, msg, user_chat_id, file_size,
                            on_progress=on_progress,
                        )
                        return True, None
                    else:
                        if skip_public_copy:
                            # The Bot API already failed to access this public
                            # source. Use the logged-in Pyrogram session directly
                            # instead of retrying the same copy operation.
                            if is_large:
                                await _pyrogram_copy_with_notice(
                                    client, bot, msg, user_chat_id, file_size
                                )
                            else:
                                await _download_and_upload_via_pyrogram(
                                    client, bot, msg, user_chat_id, file_size,
                                    on_progress=on_progress,
                                )
                            return True, None
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
                                # File >50 MB — tidak bisa di-re-upload via Bot API
                                # Pyrogram copy langsung ke Saved Messages + notifikasi
                                await _pyrogram_copy_with_notice(
                                    client, bot, msg, user_chat_id, file_size
                                )
                                return True, None
                            else:
                                # Bot tidak dapat mengakses source private.
                                # Hindari upload Bot API; gunakan MTProto seperti
                                # jalur protected agar file 40–50 MB tidak hang.
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
                        msg = await asyncio.wait_for(
                            client.get_messages(chat, msg_id),
                            timeout=_MSG_FETCH_TIMEOUT,
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
