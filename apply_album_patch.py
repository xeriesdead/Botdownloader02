from pathlib import Path

PATH = Path("modules/safe_forward.py")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(
            f"{label}: expected exactly 1 match, found {count}; no changes written."
        )
    return text.replace(old, new, 1)


text = PATH.read_text(encoding="utf-8")

helper_marker = "\n\ndef _video_metadata(msg) -> dict:\n"
helper = '''

async def _safe_create_video_thumbnail_async(
    path: str,
    timeout_seconds: int = 20,
) -> str | None:
    """Buat thumbnail dengan batas waktu agar album tidak menggantung."""
    try:
        return await asyncio.wait_for(
            _create_video_thumbnail_async(path),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Thumbnail timeout setelah %ss: %s; lanjut tanpa thumbnail",
            timeout_seconds,
            path,
        )
        return None
    except Exception:
        logger.exception("Gagal membuat thumbnail: %s", path)
        return None
'''

if "async def _safe_create_video_thumbnail_async(" in text:
    raise SystemExit("Helper thumbnail sudah ada; file tidak diubah.")
text = replace_once(
    text,
    helper_marker,
    helper + helper_marker,
    "marker helper thumbnail",
)

# Terapkan hanya pada pemanggilan thumbnail yang ada setelah helper; helper
# sendiri tetap memanggil fungsi asli.
old_thumbnail = "await _create_video_thumbnail_async(path)"
new_thumbnail = "await _safe_create_video_thumbnail_async(path)"
thumbnail_count = text.count(old_thumbnail)
if thumbnail_count != 3:
    raise SystemExit(
        "Pemanggilan thumbnail: expected exactly 3 matches, "
        f"found {thumbnail_count}; no changes written."
    )
text = text.replace(old_thumbnail, new_thumbnail)

album_marker = '''        for i, m in enumerate(msgs):
            file_size = _get_file_size(m) or 0
'''
album_start = text.find("async def _send_album_via_bot(")
album_end = text.find("\nasync def _pyrogram_copy_with_notice(", album_start)
if album_start < 0 or album_end < 0:
    raise SystemExit("Rentang fungsi album utama tidak ditemukan; file tidak diubah.")
album = text[album_start:album_end]

album_replacement = '''        for i, m in enumerate(msgs):
            file_size = _get_file_size(m) or 0
            logger.info(
                "Album item %s/%s mulai: msg_id=%s size=%s video=%s photo=%s",
                i + 1,
                total,
                getattr(m, "id", "?"),
                file_size,
                bool(getattr(m, "video", None)),
                bool(getattr(m, "photo", None)),
            )
'''
album = replace_once(
    album,
    album_marker,
    album_replacement,
    "loop album utama",
)

download_marker = '''            for _dl_attempt in range(2):
                try:
                    path = await _download_media(
'''
download_replacement = '''            for _dl_attempt in range(2):
                logger.info(
                    "Album item %s/%s mulai download, attempt=%s",
                    i + 1,
                    total,
                    _dl_attempt + 1,
                )
                try:
                    path = await _download_media(
'''
album = replace_once(
    album,
    download_marker,
    download_replacement,
    "download album utama",
)

completed_marker = '''            paths.append(path)
            download_dirs.append(item_dir)

            await _notify_progress(
'''
completed_replacement = '''            logger.info(
                "Album item %s/%s selesai download: msg_id=%s path=%s",
                i + 1,
                total,
                getattr(m, "id", "?"),
                path,
            )
            paths.append(path)
            download_dirs.append(item_dir)

            await _notify_progress(
'''
album = replace_once(
    album,
    completed_marker,
    completed_replacement,
    "hasil download album utama",
)

thumbnail_marker = '''            elif m.video:
                thumbnail_path = await _safe_create_video_thumbnail_async(path)
'''
thumbnail_replacement = '''            elif m.video:
                logger.info(
                    "Album item %s/%s mulai thumbnail: msg_id=%s",
                    i + 1,
                    total,
                    getattr(m, "id", "?"),
                )
                thumbnail_path = await _safe_create_video_thumbnail_async(path)
                logger.info(
                    "Album item %s/%s selesai thumbnail: msg_id=%s result=%s",
                    i + 1,
                    total,
                    getattr(m, "id", "?"),
                    bool(thumbnail_path),
                )
'''
album = replace_once(
    album,
    thumbnail_marker,
    thumbnail_replacement,
    "thumbnail album utama",
)

text = text[:album_start] + album + text[album_end:]
PATH.write_text(text, encoding="utf-8")
print("Patch berhasil diterapkan ke modules/safe_forward.py")
