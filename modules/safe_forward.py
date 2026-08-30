import asyncio
import os
import random
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


async def _notify_progress(on_progress, text: str):
    """Kirim status fase tanpa membuat download gagal jika Telegram sedang timeout."""
    if not on_progress:
        return
    try:
        await on_progress(text)
    except Exception:
        pass


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

    caption = _build_caption(msg.caption or "")
    thumbnail_path = (
        await _create_video_thumbnail_async(path) if msg.video else None
    )
    metadata = _video_metadata(msg)
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
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
        if thumbnail_path:
            try:
                os.remove(thumbnail_path)
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
                try:
                    await on_progress(
                        f"📥 <b>Mengunduh album...</b> ({i + 1}/{total})"
                    )
                except Exception:
                    pass
            path = None
            target = _album_download_target(m, user_chat_id, msg_id, i + 1)
            for _dl_attempt in range(2):
                try:
                    path = await asyncio.wait_for(
                        client.download_media(
                            m, file_name=target, progress=dl_cb
                        ),
                        timeout=dl_timeout,
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
                continue
            paths.append(path)

            caption = _build_caption(m.caption or "") if i == 0 else ""
            f       = open(path, "rb")  # noqa: WPS515 — ditutup di finally
            handles.append(f)

            if m.photo:
                media_items.append(InputMediaPhoto(media=f, caption=caption))
            elif m.video:
                thumb_path = await _create_video_thumbnail_async(path)
                if thumb_path:
                    thumbnail_paths.append(thumb_path)
                    thumb = open(thumb_path, "rb")
                    thumbnail_handles.append(thumb)
                    media_items.append(
                        InputMediaVideo(
                            media=f,
                            caption=caption,
                            thumbnail=thumb,
                            supports_streaming=True,
                            **_video_metadata(m),
                        )
                    )
                else:
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
    await msg.copy(bot_peer)


async def _download_and_upload_via_pyrogram(client, bot, msg, user_chat_id: int,
                                            file_size: int, on_progress=None):
    """
    Untuk file besar (>50 MB) dari channel restricted:
    Download file via Pyrogram lalu upload ulang langsung ke chat bot user via MTProto.
    Bypass sekaligus: batas 50 MB Bot API + larangan forward/copy dari channel restricted.
    File bisa sampai 1 GB (sesuai MAX_FILE_SIZE_BYTES di config).
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

    # Kirim ke chat bot (bukan Saved Messages).
    # Dari sudut pandang Pyrogram (login sebagai user), mengirim ke @bot_username
    # membuat file muncul langsung di chat antara user dan bot.
    bot_peer = f"@{_BOT_USERNAME}" if _BOT_USERNAME else user_chat_id

    ul_cb = _make_pyrogram_progress(on_progress, "Mengirim", file_size) if show_progress else None
    caption = _build_caption(msg.caption or "")
    thumbnail_path = (
        await _create_video_thumbnail_async(path) if msg.video else None
    )
    metadata = _video_metadata(msg)
    try:
        if msg.photo:
            await asyncio.wait_for(
                client.send_photo(bot_peer, path, caption=caption, progress=ul_cb),
                timeout=_UPLOAD_TIMEOUT,
            )
        elif msg.video:
            await asyncio.wait_for(
                client.send_video(
                    bot_peer,
                    path,
                    caption=caption,
                    supports_streaming=True,
                    thumb=thumbnail_path,
                    progress=ul_cb,
                    **metadata,
                ),
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
        if thumbnail_path:
            try:
                os.remove(thumbnail_path)
            except Exception:
                pass


async def _send_album_item(
    client, bot, msg, path: str, user_chat_id: int,
) -> None:
    """Kirim satu item album melalui jalur yang sesuai dengan ukuran file."""
    caption = _build_caption(msg.caption or "")
    file_size = _get_file_size(msg) or 0
    bot_peer = f"@{_BOT_USERNAME}" if _BOT_USERNAME else user_chat_id
    thumbnail_path = (
        await _create_video_thumbnail_async(path) if msg.video else None
    )
    metadata = _video_metadata(msg)
    _kw = dict(
        write_timeout=_PTB_WRITE_TIMEOUT,
        read_timeout=_PTB_READ_TIMEOUT,
        connect_timeout=_PTB_CONNECT_TIMEOUT,
    )

    try:
        if file_size > _BOT_API_UPLOAD_LIMIT:
            if msg.photo:
                await asyncio.wait_for(
                    client.send_photo(bot_peer, path, caption=caption),
                    timeout=_UPLOAD_TIMEOUT,
                )
            elif msg.video:
                await asyncio.wait_for(
                    client.send_video(
                        bot_peer,
                        path,
                        caption=caption,
                        supports_streaming=True,
                        thumb=thumbnail_path,
                        **metadata,
                    ),
                    timeout=_UPLOAD_TIMEOUT,
                )
            elif msg.audio:
                await asyncio.wait_for(
                    client.send_audio(bot_peer, path, caption=caption),
                    timeout=_UPLOAD_TIMEOUT,
                )
            elif msg.voice:
                await asyncio.wait_for(
                    client.send_voice(bot_peer, path, caption=caption),
                    timeout=_UPLOAD_TIMEOUT,
                )
            elif msg.video_note:
                await asyncio.wait_for(
                    client.send_video_note(bot_peer, path),
                    timeout=_UPLOAD_TIMEOUT,
                )
            elif msg.animation:
                await asyncio.wait_for(
                    client.send_animation(bot_peer, path, caption=caption),
                    timeout=_UPLOAD_TIMEOUT,
                )
            else:
                await asyncio.wait_for(
                    client.send_document(bot_peer, path, caption=caption),
                    timeout=_UPLOAD_TIMEOUT,
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
        target = _album_download_target(m, user_chat_id, msg_id, i + 1)
        for _dl_attempt in range(2):
            try:
                path = await asyncio.wait_for(
                    client.download_media(
                        m, file_name=target, progress=dl_cb
                    ),
                    timeout=dl_timeout,
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
        else:
            logger.error(f"Skip album item {i + 1}/{total} msg {m.id} setelah 2 percobaan.")

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

    if sent == 0:
        return False, "Semua file dalam album gagal dikirim."
    if sent != total:
        return False, f"Album hanya terkirim {sent}/{total} media. Silakan coba lagi."

    return True, None


# ── SafeForward ───────────────────────────────────────────────────────────────

class SafeForward:

    @staticmethod
    async def run_album(
        client, bot, user_chat_id: int, chat, msg_id: int,
        on_progress=None,
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
        _, src_err = await _resolve_source(client, chat)
        if src_err:
            return False, src_err

        # ── Deteksi noforwards sebelum mencoba forward/copy ───────────────
        await _notify_progress(on_progress, "🔎 <b>Memeriksa akses media...</b>")
        if await _is_forwards_restricted(client, chat):
            return await _send_album_individually(
                client, bot, chat, msg_id, user_chat_id, on_progress=on_progress
            )

        for attempt in range(MAX_RETRIES + 1):
            try:
                await _notify_progress(on_progress, "📥 <b>Mengambil album...</b>")
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
        await _notify_progress(on_progress, "🔌 <b>Menghubungkan ke channel...</b>")
        _, src_err = await _resolve_source(client, chat)
        if src_err:
            return False, src_err

        # ── Deteksi noforwards sebelum fetch pesan ────────────────────────
        await _notify_progress(on_progress, "🔎 <b>Memeriksa akses media...</b>")
        is_restricted = await _is_forwards_restricted(client, chat)

        # ── Langkah 2: Ambil pesan ───────────────────────────────────────
        await _notify_progress(on_progress, "📥 <b>Mengambil pesan dari channel...</b>")
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
        if msg.media_group_id:
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
                        # Channel noforwards: skip copy_message & pyrogram copy —
                        # keduanya akan ditolak Telegram. Langsung download + upload.
                        if is_large:
                            # File >50 MB: download via Pyrogram, upload ulang via Pyrogram MTProto
                            # (bukan Bot API — tidak ada batas 50 MB), kirim ke Saved Messages user.
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
                                # File >50 MB — tidak bisa di-re-upload via Bot API
                                # Pyrogram copy langsung ke Saved Messages + notifikasi
                                await _pyrogram_copy_with_notice(
                                    client, bot, msg, user_chat_id, file_size
                                )
                                return True, None
                            else:
                                # File ≤50 MB — download Pyrogram, upload via bot
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
