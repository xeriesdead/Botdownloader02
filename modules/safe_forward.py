import asyncio
import os
import random
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
        chat_obj = await client.get_chat(chat)
        restricted = bool(getattr(chat_obj, "has_protected_content", False))
        _forwards_restricted_cache[cache_key] = restricted
        if restricted:
            logger.info(f"Chat {chat} memiliki noforwards aktif — pakai strategi download+upload")
        return restricted
    except Exception as e:
        logger.warning(f"Gagal cek has_protected_content untuk {chat}: {e}")
        return False


async def _resolve_source(client, chat) -> tuple[object | None, str | None]:
    """Resolve source peer. Return (peer, None) atau (None, error_msg)."""
    try:
        peer = await client.resolve_peer(chat)
        return peer, None
    except (UsernameNotOccupied, UsernameInvalid):
        label = chat if isinstance(chat, str) else f"ID {chat}"
        return None, f"Channel/grup `{label}` tidak ditemukan atau sudah tidak aktif."
    except _PEER_ERRORS:
        label = chat if isinstance(chat, str) else f"ID {chat}"
        return None, (
            f"Tidak bisa mengakses `{label}`.\n"
            "Pastikan akun yang login sudah **bergabung** ke channel/grup tersebut."
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
    path = await client.download_media(msg, progress=dl_cb)
    if not path:
        raise RuntimeError("Download gagal, file tidak tersedia.")

    caption = _build_caption(msg.caption or "")
    try:
        if msg.photo:
            with open(path, "rb") as f:
                await bot.send_photo(user_chat_id, photo=f, caption=caption)
        elif msg.video:
            with open(path, "rb") as f:
                await bot.send_video(user_chat_id, video=f, caption=caption)
        elif msg.audio:
            with open(path, "rb") as f:
                await bot.send_audio(user_chat_id, audio=f, caption=caption)
        elif msg.voice:
            with open(path, "rb") as f:
                await bot.send_voice(user_chat_id, voice=f, caption=caption)
        elif msg.video_note:
            with open(path, "rb") as f:
                await bot.send_video_note(user_chat_id, video_note=f)
        elif msg.animation:
            with open(path, "rb") as f:
                await bot.send_animation(user_chat_id, animation=f, caption=caption)
        elif msg.sticker:
            with open(path, "rb") as f:
                await bot.send_sticker(user_chat_id, sticker=f)
        else:
            with open(path, "rb") as f:
                await bot.send_document(user_chat_id, document=f, caption=caption)
    finally:
        try:
            os.remove(path)
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
            if on_progress:
                try:
                    await on_progress(
                        f"📥 <b>Mempersiapkan album...</b> ({i + 1}/{total})"
                    )
                except Exception:
                    pass
            path = await client.download_media(m)
            if not path:
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
            await bot.send_media_group(user_chat_id, media=media_items)
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
    Pyrogram meng-copy ke Saved Messages user + bot mengirim notifikasi.
    """
    await msg.copy(user_chat_id)
    await bot.send_message(
        user_chat_id,
        f"ℹ️ File berukuran <b>{_fmt_size(file_size)}</b> dikirim ke "
        "<b>Saved Messages</b> karena channel bersifat private dan "
        "melebihi batas upload Bot API (50 MB).",
        parse_mode="HTML",
    )


async def _download_and_upload_via_pyrogram(client, bot, msg, user_chat_id: int,
                                            file_size: int, on_progress=None):
    """
    Untuk file besar (>50 MB) dari channel restricted:
    Download file via Pyrogram lalu upload ulang ke Saved Messages user via Pyrogram (MTProto).
    Bypass sekaligus: batas 50 MB Bot API + larangan forward/copy dari channel restricted.
    File bisa sampai 1 GB (sesuai MAX_FILE_SIZE_BYTES di config).
    on_progress: async callable(text: str) untuk update pesan status (opsional).
    """
    show_progress = on_progress and file_size >= _PROGRESS_MIN_BYTES
    dl_cb = _make_pyrogram_progress(on_progress, "Mengunduh", file_size) if show_progress else None

    path = await client.download_media(msg, progress=dl_cb)
    if not path:
        raise RuntimeError("Download gagal, file tidak tersedia.")

    ul_cb = _make_pyrogram_progress(on_progress, "Mengirim", file_size) if show_progress else None
    caption = _build_caption(msg.caption or "")
    try:
        if msg.photo:
            await client.send_photo(user_chat_id, path, caption=caption, progress=ul_cb)
        elif msg.video:
            await client.send_video(user_chat_id, path, caption=caption,
                                    supports_streaming=True, progress=ul_cb)
        elif msg.audio:
            await client.send_audio(user_chat_id, path, caption=caption, progress=ul_cb)
        elif msg.voice:
            await client.send_voice(user_chat_id, path, caption=caption, progress=ul_cb)
        elif msg.video_note:
            await client.send_video_note(user_chat_id, path, progress=ul_cb)
        elif msg.animation:
            await client.send_animation(user_chat_id, path, caption=caption, progress=ul_cb)
        elif msg.sticker:
            await client.send_sticker(user_chat_id, path, progress=ul_cb)
        else:
            await client.send_document(user_chat_id, path, caption=caption, progress=ul_cb)

        await bot.send_message(
            user_chat_id,
            f"ℹ️ File berukuran <b>{_fmt_size(file_size)}</b> dikirim ke "
            "<b>Saved Messages</b> karena melebihi batas upload Bot API (50 MB).",
            parse_mode="HTML",
        )
    finally:
        try:
            os.remove(path)
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
        if on_progress:
            try:
                await on_progress(
                    f"📥 <b>Mengunduh album...</b> ({i + 1}/{total})"
                )
            except Exception:
                pass
        try:
            path = await client.download_media(m)
            if path:
                paths.append((m, path))
        except Exception as e:
            logger.warning(f"Gagal download file album msg {m.id}: {e}")

    if not paths:
        return False, "Gagal mendownload semua file dalam album."

    # Coba kirim sebagai album (send_media_group) dulu agar tetap tampil sebagai album
    album_sent = False
    handles = []
    media_items = []
    try:
        for i, (m, path) in enumerate(paths):
            caption = _build_caption(m.caption or "") if i == 0 else ""
            f = open(path, "rb")
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
            await bot.send_media_group(user_chat_id, media=media_items)
            album_sent = True
    except Exception as e:
        logger.warning(f"send_media_group gagal untuk album msg {msg_id}, kirim satu per satu: {e}")
    finally:
        for f in handles:
            try:
                f.close()
            except Exception:
                pass
        if album_sent:
            # Berhasil kirim sebagai album — bersihkan semua file download
            for _, path in paths:
                try:
                    os.remove(path)
                except Exception:
                    pass
            return True, None

    # Fallback terakhir: kirim satu per satu
    # File > 50 MB dikirim via Pyrogram MTProto (ke Saved Messages) agar bypass limit Bot API.
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
            if file_size > _BOT_API_UPLOAD_LIMIT:
                # File terlalu besar untuk Bot API — kirim via Pyrogram MTProto
                if m.photo:
                    await client.send_photo(user_chat_id, path, caption=caption)
                elif m.video:
                    await client.send_video(user_chat_id, path, caption=caption,
                                            supports_streaming=True)
                elif m.audio:
                    await client.send_audio(user_chat_id, path, caption=caption)
                elif m.voice:
                    await client.send_voice(user_chat_id, path, caption=caption)
                elif m.video_note:
                    await client.send_video_note(user_chat_id, path)
                elif m.animation:
                    await client.send_animation(user_chat_id, path, caption=caption)
                else:
                    await client.send_document(user_chat_id, path, caption=caption)
                large_sent += 1
            else:
                with open(path, "rb") as f:
                    if m.photo:
                        await bot.send_photo(user_chat_id, photo=f, caption=caption)
                    elif m.video:
                        await bot.send_video(user_chat_id, video=f, caption=caption)
                    elif m.audio:
                        await bot.send_audio(user_chat_id, audio=f, caption=caption)
                    elif m.voice:
                        await bot.send_voice(user_chat_id, voice=f, caption=caption)
                    elif m.animation:
                        await bot.send_animation(user_chat_id, animation=f, caption=caption)
                    else:
                        await bot.send_document(user_chat_id, document=f, caption=caption)
            sent += 1
        except Exception as e:
            logger.warning(f"Gagal kirim file album msg {m.id}: {e}")
        finally:
            try:
                os.remove(path)
            except Exception:
                pass

    if sent == 0:
        return False, "Semua file dalam album gagal dikirim."

    # Beritahu user jika ada file besar yang dikirim ke Saved Messages
    if large_sent > 0:
        try:
            await bot.send_message(
                user_chat_id,
                f"ℹ️ <b>{large_sent} file</b> dalam album berukuran >50 MB dikirim ke "
                "<b>Saved Messages</b> (melebihi batas upload Bot API).",
                parse_mode="HTML",
            )
        except Exception:
            pass

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
            msg = await client.get_messages(chat, msg_id)
        except (MessageIdInvalid, MsgIdInvalid):
            return False, f"Pesan nomor `{msg_id}` tidak ditemukan."
        except _PEER_ERRORS:
            return False, "Tidak bisa mengakses channel. Pastikan akun sudah bergabung."
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
                        msg = await client.get_messages(chat, msg_id)
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
