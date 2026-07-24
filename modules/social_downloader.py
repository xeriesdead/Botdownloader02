import asyncio
import os
import shutil
import subprocess
import tempfile
import urllib.request
import json as _json
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from logger import logger


SOCIAL_DOMAINS = {
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "fb.watch",
    "fb.com",
    "instagram.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "t.co",
    "threads.net",
    "threads.com",
}

# Facebook: scraping langsung dari halaman HTML
_FACEBOOK_DOMAINS = {"facebook.com", "fb.watch", "fb.com"}
# Threads: pakai cobalt API
_THREADS_DOMAINS  = {"threads.net", "threads.com"}
_COBALT_API       = "https://api.cobalt.tools/"
_COBALT_UA        = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_FB_UA            = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_TWITTER_DOMAINS = {"twitter.com", "x.com", "t.co"}
_TIKTOK_DOMAINS  = {"tiktok.com", "vt.tiktok.com"}

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


def _is_facebook_link(url: str) -> bool:
    try:
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(hostname == d or hostname.endswith(f".{d}") for d in _FACEBOOK_DOMAINS)


def _is_threads_link(url: str) -> bool:
    try:
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(hostname == d or hostname.endswith(f".{d}") for d in _THREADS_DOMAINS)


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


def _facebook_download_sync(url: str, work_dir: str) -> tuple[str, list[str]]:
    """
    Download video Facebook dengan scraping halaman HTML langsung.
    Mendukung semua format URL Facebook termasuk /share/, /watch/, /reel/, dll.
    """
    import re
    import http.cookiejar

    jar    = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent",      _FB_UA),
        ("Accept",          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
        ("Accept-Language", "en-US,en;q=0.9"),
    ]

    try:
        resp = opener.open(url, timeout=30)
        html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise ValueError(f"❌ Gagal mengakses halaman Facebook: {exc}") from exc

    # Cari URL video dari JSON yang di-embed Facebook di dalam HTML
    video_url: str | None = None
    for pattern in [
        r'"playable_url_quality_hd":"((?:[^"\\]|\\.)*)"',
        r'"playable_url":"((?:[^"\\]|\\.)*)"',
        r'"browser_native_hd_url":"((?:[^"\\]|\\.)*)"',
        r'"browser_native_sd_url":"((?:[^"\\]|\\.)*)"',
        r'"hd_src":"((?:[^"\\]|\\.)*)"',
        r'"sd_src":"((?:[^"\\]|\\.)*)"',
    ]:
        m = re.search(pattern, html)
        if m:
            raw = m.group(1)
            # Unescape JSON string escapes
            video_url = (
                raw.replace("\\u0026", "&")
                   .replace("\\/", "/")
                   .replace("\\\\", "\\")
            )
            break

    # Coba juga dari og:video meta tag
    if not video_url:
        m = re.search(r'<meta property="og:video(?::url)?" content="([^"]+)"', html)
        if m:
            video_url = m.group(1)

    if not video_url:
        raise ValueError(
            "❌ Gagal mendapatkan URL video dari Facebook.\n"
            "Pastikan video bersifat <b>publik</b> dan link masih aktif.\n\n"
            "<i>Video yang hanya untuk teman atau privat tidak bisa didownload.</i>"
        )

    logger.info("[social] Facebook video URL: %s...", video_url[:80])

    # Ambil judul dari og:title
    title = "Facebook video"
    tm = re.search(r'<meta property="og:title" content="([^"]*)"', html)
    if tm:
        title = tm.group(1) or title

    dest = os.path.join(work_dir, "001_video.mp4")
    req2 = urllib.request.Request(
        video_url,
        headers={
            "User-Agent": _FB_UA,
            "Referer":    "https://www.facebook.com/",
        },
    )
    try:
        with urllib.request.urlopen(req2, timeout=300) as r:
            with open(dest, "wb") as f:
                while True:
                    chunk = r.read(512 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
    except Exception as exc:
        raise ValueError(f"❌ Gagal mengunduh video Facebook: {exc}") from exc

    return title, [dest]


def _cobalt_download_sync(url: str, work_dir: str) -> tuple[str, list[str]]:
    """
    Download via cobalt.tools API — tanpa auth, mendukung Threads.
    Returns (title, list_of_file_paths).
    """
    body = _json.dumps({"url": url}).encode()
    req  = urllib.request.Request(
        _COBALT_API,
        data=body,
        headers={
            "Accept":       "application/json",
            "Content-Type": "application/json",
            "User-Agent":   _COBALT_UA,
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = _json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ValueError(
            f"❌ Layanan download tidak tersedia saat ini (HTTP {exc.code}). Coba lagi nanti."
        ) from exc
    except Exception as exc:
        raise ValueError(
            "❌ Gagal menghubungi layanan download. Coba lagi nanti."
        ) from exc

    status = data.get("status")
    logger.info("[social] cobalt status=%s url=%s", status, url)

    if status == "error":
        code = (data.get("error") or {}).get("code", "unknown")
        raise ValueError(
            "❌ Gagal mendownload.\n"
            "Pastikan link masih aktif dan bersifat publik.\n"
            f"<i>Kode: {code}</i>"
        )

    def _dl(dl_url: str, dest: str) -> None:
        req2 = urllib.request.Request(dl_url, headers={"User-Agent": _COBALT_UA})
        with urllib.request.urlopen(req2, timeout=120) as r:
            with open(dest, "wb") as f:
                while True:
                    chunk = r.read(512 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)

    files: list[str] = []

    if status in ("tunnel", "redirect"):
        dl_url = data.get("url")
        if not dl_url:
            raise ValueError("❌ Tidak ada link download yang tersedia.")
        # Tentukan ekstensi dari Content-Type atau fallback mp4
        dest = os.path.join(work_dir, "001_video.mp4")
        _dl(dl_url, dest)
        files.append(dest)
        logger.info("[social] cobalt single downloaded: %d bytes", os.path.getsize(dest))

    elif status == "picker":
        items = data.get("picker") or []
        for i, item in enumerate(items, 1):
            item_url  = item.get("url")
            item_type = item.get("type", "video")
            if not item_url:
                continue
            ext  = "jpg" if item_type == "photo" else "mp4"
            dest = os.path.join(work_dir, f"{i:03d}_{item_type}.{ext}")
            try:
                _dl(item_url, dest)
                files.append(dest)
                logger.info(
                    "[social] cobalt picker %d downloaded: %d bytes",
                    i, os.path.getsize(dest),
                )
            except Exception as exc:
                logger.warning("[social] cobalt picker item %d failed: %s", i, exc)

    if not files:
        raise ValueError(
            "❌ Tidak ada media yang berhasil didownload.\n"
            "Pastikan link masih aktif dan bersifat publik."
        )

    return "Facebook/Threads", sorted(files)


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


def _is_tiktok_link(url: str) -> bool:
    try:
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(
        hostname == d or hostname.endswith(f".{d}") for d in _TIKTOK_DOMAINS
    )


def _tikwm_download_sync(url: str, work_dir: str) -> tuple[str, list[str]]:
    """
    Download TikTok video/photo via tikwm.com public API (no auth required).
    Handles geo-blocking that prevents yt-dlp from accessing TikTok pages.
    Returns (title, list_of_file_paths).
    """
    _UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    api_url = f"https://www.tikwm.com/api/?url={url}&hd=1"
    logger.info("[social] tikwm API request: %s", api_url)

    req = urllib.request.Request(api_url, headers={"User-Agent": _UA})
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        data = _json.loads(resp.read())
    except Exception as exc:
        raise ValueError(
            "❌ Gagal menghubungi layanan download TikTok. Coba lagi nanti."
        ) from exc

    if data.get("code") != 0:
        msg = data.get("msg") or "unknown error"
        raise ValueError(
            f"❌ Gagal mengambil info video TikTok: {msg}\n"
            "Pastikan link masih aktif dan bersifat publik."
        )

    v      = data.get("data") or {}
    title  = (v.get("title") or "TikTok video").strip()[:160] or "TikTok video"
    images = v.get("images")  # list of image URLs for photo carousel

    def _dl(dl_url: str, dest: str) -> str:
        req2 = urllib.request.Request(dl_url, headers={"User-Agent": _UA, "Referer": "https://www.tikwm.com/"})
        with urllib.request.urlopen(req2, timeout=120) as r:
            with open(dest, "wb") as f:
                while True:
                    chunk = r.read(512 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
        return dest

    files: list[str] = []

    if images:
        # Photo carousel: download each image
        for i, img_url in enumerate(images, 1):
            ext  = img_url.split("?")[0].rsplit(".", 1)[-1] if "." in img_url.split("?")[0] else "jpg"
            dest = os.path.join(work_dir, f"{i:03d}_photo.{ext}")
            try:
                _dl(img_url, dest)
                files.append(dest)
                logger.info("[social] tikwm photo %d/%d downloaded: %d bytes", i, len(images), os.path.getsize(dest))
            except Exception as exc:
                logger.warning("[social] tikwm photo %d failed: %s", i, exc)
    else:
        # Video: prefer no-watermark URL, fall back to watermarked
        play_url = v.get("play") or v.get("wmplay")
        if not play_url:
            raise ValueError("❌ Tidak ada URL video yang tersedia dari API.")
        dest = os.path.join(work_dir, "001_video.mp4")
        _dl(play_url, dest)
        files.append(dest)
        logger.info("[social] tikwm video downloaded: %d bytes", os.path.getsize(dest))

    if not files:
        raise ValueError(
            "❌ Tidak ada media yang berhasil didownload dari TikTok.\n"
            "Pastikan link masih aktif dan bersifat publik."
        )

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

    # ── TikTok: gunakan tikwm API (bypass geo-block) ─────────────────────
    if _is_tiktok_link(url):
        logger.info("[social] TikTok detected, using tikwm API: %s", url)
        return _tikwm_download_sync(url, work_dir)

    # ── Facebook: scraping langsung dari halaman HTML ─────────────────────
    if _is_facebook_link(url):
        logger.info("[social] Facebook detected, using HTML scrape: %s", url)
        return _facebook_download_sync(url, work_dir)

    # ── Threads: cobalt API ───────────────────────────────────────────────
    if _is_threads_link(url):
        logger.info("[social] Threads detected, using cobalt API: %s", url)
        return _cobalt_download_sync(url, work_dir)

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
