import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from logger import logger


SOCIAL_DOMAINS = {
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "fb.watch",
    "instagram.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "t.co",
    "threads.net",
}

_TEMP_ROOT = "downloads"
_IGNORED_SUFFIXES = {".part", ".ytdl", ".json", ".description", ".jpg.part"}


def is_social_link(url: str) -> bool:
    """True only for supported public social-media hostnames."""
    try:
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not hostname:
        return False
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in SOCIAL_DOMAINS
    )


def _is_instagram_carousel(url: str) -> bool:
    try:
        path = (urlparse(url).path or "").lower()
    except ValueError:
        return False
    return "instagram.com" in (urlparse(url).netloc or "").lower() and (
        "/p/" in path or "/reel/" in path
    )


def _download_sync(url: str, work_dir: str) -> tuple[str, list[str]]:
    """Run yt-dlp outside the event loop and return title plus downloaded paths."""
    before = {
        str(path)
        for path in Path(work_dir).rglob("*")
        if path.is_file()
    }
    output_template = str(Path(work_dir) / "%(autonumber)03d_%(title).80s.%(ext)s")
    options = {
        "outtmpl": output_template,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        # A single Instagram post may contain a carousel. Other platforms
        # stay single-item to avoid unexpectedly downloading playlists.
        "noplaylist": not _is_instagram_carousel(url),
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "restrictfilenames": True,
        "writethumbnail": False,
        "writeinfojson": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "concurrent_fragment_downloads": 2,
    }

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        message = str(exc)
        logger.warning("[social] yt-dlp failed for %s: %s", url, message)
        raise ValueError(
            "Link tidak bisa didownload. Pastikan link bersifat publik dan masih aktif."
        ) from exc

    title = (info or {}).get("title") or "Media sosial"
    downloaded = []
    for path in Path(work_dir).rglob("*"):
        if not path.is_file() or str(path) in before:
            continue
        if path.suffix.lower() in _IGNORED_SUFFIXES:
            continue
        downloaded.append(str(path))

    if not downloaded:
        raise ValueError("Tidak ada media yang berhasil ditemukan dari link tersebut.")
    return title, sorted(downloaded)


async def download_public_media(url: str, user_id: int) -> tuple[str, list[str], str]:
    """
    Download public social media without cookies or account credentials.
    Returns (title, file_paths, temporary_directory).
    """
    if not is_social_link(url):
        raise ValueError("Platform sosial ini belum didukung.")

    os.makedirs(_TEMP_ROOT, exist_ok=True)
    work_dir = tempfile.mkdtemp(prefix=f"social_{user_id}_", dir=_TEMP_ROOT)
    try:
        title, files = await asyncio.to_thread(_download_sync, url, work_dir)
        return title, files, work_dir
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def cleanup_download(directory: str) -> None:
    shutil.rmtree(directory, ignore_errors=True)