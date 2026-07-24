import asyncio
import os
import shutil
import subprocess
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

_TWITTER_DOMAINS = {"twitter.com", "x.com", "t.co"}

_TEMP_ROOT = "downloads"
_IGNORED_SUFFIXES = {".part", ".ytdl", ".json", ".description", ".jpg.part"}

# yt-dlp errors that clearly mean "no downloadable media in this content"
_NO_MEDIA_PHRASES = (
    "no video could be found",
    "this tweet does not contain",
    "there's no video in this tweet",
)

# yt-dlp errors that clearly mean access/auth is required
_AUTH_PHRASES = (
    "login required",
    "not accessible",
    "private",
    "age-restricted",
    "age restriction",
    "members only",
)


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


def _is_twitter_link(url: str) -> bool:
    try:
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(
        hostname == d or hostname.endswith(f".{d}") for d in _TWITTER_DOMAINS
    )


def _is_instagram_carousel(url: str) -> bool:
    try:
        path = (urlparse(url).path or "").lower()
    except ValueError:
        return False
    return "instagram.com" in (urlparse(url).netloc or "").lower() and (
        "/p/" in path or "/reel/" in path
    )


def _classify_ytdlp_error(message: str) -> str:
    """Return a user-friendly Indonesian message based on the yt-dlp error text."""
    lower = message.lower()
    if any(p in lower for p in _NO_MEDIA_PHRASES):
        return (
            "❌ Tidak ada video atau foto native yang bisa didownload dari link ini.\n\n"
            "Kemungkinan penyebab:\n"
            "• Tweet hanya berisi teks atau link preview artikel\n"
            "• Konten sudah dihapus oleh pemiliknya\n\n"
            "<i>Tip: Hanya tweet yang berisi video/foto yang di-upload langsung "
            "yang bisa didownload.</i>"
        )
    if any(p in lower for p in _AUTH_PHRASES):
        return (
            "❌ Konten ini bersifat privat atau memerlukan login.\n"
            "Bot hanya mendukung konten yang benar-benar publik."
        )
    return (
        "❌ Gagal mendownload. Pastikan link masih aktif dan bersifat publik.\n\n"
        f"<i>Detail: {message[:200]}</i>"
    )


def _gallery_dl_sync(url: str, work_dir: str) -> tuple[str, list[str]]:
    """
    Fallback downloader for X/Twitter using gallery-dl.
    Returns (title, list_of_file_paths).
    """
    result = subprocess.run(
        [
            "gallery-dl",
            "--dest", work_dir,
            "--filename", "{num:>03}_{filename}.{extension}",
            "--no-mtime",
            url,
        ],
        capture_output=True, text=True, timeout=60,
    )
    logger.info("[social] gallery-dl rc=%s stderr=%s", result.returncode, result.stderr[:300])

    files = []
    for path in Path(work_dir).rglob("*"):
        if path.is_file() and path.suffix.lower() not in _IGNORED_SUFFIXES:
            files.append(str(path))

    if result.returncode != 0 and not files:
        stderr_lower = (result.stderr or "").lower()
        if "keyerror" in stderr_lower or "unexpected error" in stderr_lower:
            raise ValueError(
                "❌ Tidak ada video atau foto native yang bisa didownload dari link ini.\n\n"
                "Kemungkinan penyebab:\n"
                "• Tweet hanya berisi teks atau link preview artikel\n"
                "• Konten sudah dihapus oleh pemiliknya\n\n"
                "<i>Tip: Hanya tweet yang berisi video/foto yang di-upload langsung "
                "yang bisa didownload.</i>"
            )
        raise ValueError(
            "❌ Gagal mendownload dari X/Twitter.\n"
            "Pastikan link masih aktif dan bersifat publik."
        )

    if not files:
        raise ValueError(
            "❌ Tidak ada video atau foto native yang bisa didownload dari link ini.\n\n"
            "Kemungkinan penyebab:\n"
            "• Tweet hanya berisi teks atau link preview artikel\n"
            "• Konten sudah dihapus oleh pemiliknya"
        )

    # Use tweet ID as title fallback
    try:
        tweet_id = urlparse(url).path.rstrip("/").split("/")[-1]
        title = f"X post {tweet_id}"
    except Exception:
        title = "X post"

    return title, sorted(files)


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
        # A single Instagram post may contain a carousel; other platforms
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

    ytdlp_error_msg: str | None = None
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        ytdlp_error_msg = str(exc)
        logger.warning("[social] yt-dlp failed for %s: %s", url, ytdlp_error_msg)

        # For X/Twitter: try gallery-dl as fallback (handles photos)
        if _is_twitter_link(url):
            logger.info("[social] trying gallery-dl fallback for X/Twitter: %s", url)
            return _gallery_dl_sync(url, work_dir)

        raise ValueError(_classify_ytdlp_error(ytdlp_error_msg)) from exc

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
