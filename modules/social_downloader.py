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

# ── YouTube cookies helper ────────────────────────────────────────────────────
# Jika env YOUTUBE_COOKIES diset (Netscape cookies.txt format), tulis ke file
# sementara sekali saat modul di-import agar yt-dlp bisa menggunakannya.
_YT_COOKIE_FILE: str | None = None

def _init_yt_cookie_file() -> None:
    global _YT_COOKIE_FILE
    try:
        from config import YOUTUBE_COOKIES  # import di sini untuk hindari circular
        if not YOUTUBE_COOKIES:
            return
        # Tulis ke file permanen di dalam direktori kerja (bukan /tmp yang
        # bisa dihapus OS). File ini hanya dibaca yt-dlp, tidak dikirim kemana-mana.
        cookie_dir = os.path.join(os.path.dirname(__file__), "..", "downloads")
        os.makedirs(cookie_dir, exist_ok=True)
        cookie_path = os.path.join(cookie_dir, ".yt_cookies.txt")
        with open(cookie_path, "w", encoding="utf-8") as f:
            f.write(YOUTUBE_COOKIES)
        _YT_COOKIE_FILE = cookie_path
        logger.info("[social] YouTube cookies dimuat dari env YOUTUBE_COOKIES")
    except Exception as exc:
        logger.warning("[social] Gagal inisialisasi YouTube cookies: %s", exc)

_init_yt_cookie_file()


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

_TWITTER_DOMAINS  = {"twitter.com", "x.com", "t.co"}
_TIKTOK_DOMAINS   = {"tiktok.com", "vt.tiktok.com"}
_YOUTUBE_DOMAINS  = {"youtube.com", "youtu.be", "www.youtube.com", "m.youtube.com"}

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

# Transient server-side errors — worth retrying once automatically
_TRANSIENT_PHRASES = (
    "empty media response",
    "instagram sent an empty",
    "got http error 429",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "connection reset",
    "temporarily unavailable",
)


def _is_transient_error(message: str) -> bool:
    lower = message.lower()
    return any(p in lower for p in _TRANSIENT_PHRASES)


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


def _extract_facebook_photo_urls(html: str) -> list[str]:
    """
    Ekstrak URL foto utama dari og:image di HTML Facebook.
    Hanya mengembalikan 1 URL (foto utama post) untuk menghindari duplikat
    yang timbul jika mengekstrak dari JSON/img tag (banyak ukuran berbeda).
    Cobalt API adalah strategi utama untuk album multi-foto.
    """
    import re
    for pat in (
        r'<meta\b[^>]*\bproperty="og:image"\b[^>]*\bcontent="([^"]+)"',
        r'<meta\b[^>]*\bcontent="([^"]+)"\b[^>]*\bproperty="og:image"',
    ):
        m = re.search(pat, html)
        if m:
            url = m.group(1).replace("\\u0026", "&").replace("\\/", "/")
            if "fbcdn" in url:
                return [url]
    return []


def _facebook_html_scrape_sync(url: str, work_dir: str) -> tuple[str, list[str]]:
    """
    Last-resort: Download video Facebook dengan scraping halaman HTML langsung.
    Mendukung semua format URL Facebook termasuk /share/, /watch/, /reel/, dll.
    Strategi:
      1. Kunjungi homepage Facebook dulu untuk dapat cookies sesi (datr, sb).
      2. Akses URL asli; jika gagal, coba versi mbasic.facebook.com.
      3. Ekstrak URL video dari JSON/meta yang di-embed dalam HTML.
    """
    import re
    import http.cookiejar

    jar    = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent",               _FB_UA),
        ("Accept",                   "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
        ("Accept-Language",          "en-US,en;q=0.9"),
        ("Connection",               "keep-alive"),
        ("Upgrade-Insecure-Requests","1"),
        ("Sec-Fetch-Dest",           "document"),
        ("Sec-Fetch-Mode",           "navigate"),
        ("Sec-Fetch-Site",           "none"),
    ]

    # Step 1: Kunjungi homepage Facebook untuk mendapatkan cookies sesi
    for init_url in ("https://www.facebook.com/", "https://mbasic.facebook.com/"):
        try:
            opener.open(init_url, timeout=10)
            break
        except Exception:
            continue

    # Step 2: Coba URL asli, fallback ke mbasic.facebook.com
    def _to_mbasic(u: str) -> str:
        return (
            u.replace("www.facebook.com",  "mbasic.facebook.com")
             .replace("m.facebook.com",    "mbasic.facebook.com")
             .replace("fb.watch",          "mbasic.facebook.com")
        )

    html: str | None = None
    for try_url in (url, _to_mbasic(url)):
        try:
            resp = opener.open(try_url, timeout=30)
            html = resp.read().decode("utf-8", errors="replace")
            logger.info("[social] Facebook page fetched from: %s", try_url)
            break
        except Exception as exc:
            logger.warning("[social] Facebook fetch failed (%s): %s", try_url, exc)

    if not html:
        raise ValueError(
            "❌ Gagal mengakses halaman Facebook.\n"
            "Pastikan link masih aktif dan bersifat publik."
        )

    # Step 3: Cari URL video dari berbagai pola HTML/JSON Facebook
    video_url: str | None = None

    # Pola JSON yang di-embed dalam halaman (www.facebook.com)
    json_patterns = [
        r'"playable_url_quality_hd":"((?:[^"\\]|\\.)*)"',
        r'"playable_url":"((?:[^"\\]|\\.)*)"',
        r'"browser_native_hd_url":"((?:[^"\\]|\\.)*)"',
        r'"browser_native_sd_url":"((?:[^"\\]|\\.)*)"',
        r'"hd_src":"((?:[^"\\]|\\.)*)"',
        r'"sd_src":"((?:[^"\\]|\\.)*)"',
    ]
    for pat in json_patterns:
        m = re.search(pat, html)
        if m:
            raw = m.group(1)
            video_url = (
                raw.replace("\\u0026", "&")
                   .replace("\\/",     "/")
                   .replace("\\\\",    "\\")
            )
            break

    # Pola HTML (mbasic.facebook.com): tag <video> atau link fbcdn
    if not video_url:
        for pat in (
            r'<video[^>]+src="(https://[^"]+\.fbcdn\.net[^"]*)"',
            r'href="(https://video\.[^"]+\.fbcdn\.net[^"]*)"',
            r'<meta property="og:video(?::url)?" content="([^"]+)"',
        ):
            m = re.search(pat, html)
            if m:
                video_url = m.group(1)
                break

    # Step 4: Ambil judul dari og:title — handle kedua urutan atribut
    title = "Facebook"
    for _tp in (
        r'<meta\b[^>]*\bproperty="og:title"\b[^>]*\bcontent="([^"]*)"',
        r'<meta\b[^>]*\bcontent="([^"]*)"\b[^>]*\bproperty="og:title"',
    ):
        tm = re.search(_tp, html)
        if tm:
            title = tm.group(1) or title
            break

    def _dl_url(src: str, dest: str) -> None:
        req2 = urllib.request.Request(
            src,
            headers={"User-Agent": _FB_UA, "Referer": "https://www.facebook.com/"},
        )
        with urllib.request.urlopen(req2, timeout=300) as r:
            with open(dest, "wb") as f:
                while True:
                    chunk = r.read(512 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)

    # Step 5a: Video ditemukan → download langsung
    if video_url:
        logger.info("[social] Facebook video_url=%s...", video_url[:80])
        dest = os.path.join(work_dir, "001_video.mp4")
        try:
            _dl_url(video_url, dest)
        except Exception as exc:
            raise ValueError(f"Gagal mengunduh video Facebook via HTML scrape: {exc}") from exc
        return title, [dest]

    # Step 5b: Tidak ada video → cari URL foto
    photo_urls = _extract_facebook_photo_urls(html)
    if not photo_urls:
        raise ValueError(
            "Tidak ada URL video atau foto yang ditemukan di HTML Facebook."
        )

    logger.info("[social] Facebook: ditemukan %d foto dari HTML scrape", len(photo_urls))
    files = []
    for i, purl in enumerate(photo_urls, 1):
        dest = os.path.join(work_dir, f"{i:03d}_photo.jpg")
        try:
            _dl_url(purl, dest)
            files.append(dest)
            logger.info("[social] Facebook photo %d downloaded: %d bytes", i, os.path.getsize(dest))
        except Exception as exc:
            logger.warning("[social] Facebook photo %d download failed: %s", i, exc)

    if not files:
        raise ValueError("Gagal mengunduh foto Facebook via HTML scrape.")
    return title, sorted(files)


def _facebook_ytdlp_sync(url: str, work_dir: str) -> tuple[str, list[str]]:
    """
    Fallback: Download video Facebook via yt-dlp dengan opsi khusus Facebook.
    """
    before = {str(p) for p in Path(work_dir).rglob("*") if p.is_file()}
    output_template = str(Path(work_dir) / "%(autonumber)03d_%(title).80s.%(ext)s")
    options = {
        "outtmpl":              output_template,
        "format":               "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format":  "mp4",
        "noplaylist":           True,
        "quiet":                True,
        "no_warnings":          True,
        "no_color":             True,
        "restrictfilenames":    True,
        "writethumbnail":       False,
        "writeinfojson":        False,
        "writesubtitles":       False,
        "writeautomaticsub":    False,
        "socket_timeout":       20,
        "retries":              3,
        "fragment_retries":     3,
        "http_headers": {
            "User-Agent": _FB_UA,
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

    title = (info or {}).get("title") or "Facebook video"
    files = [
        str(p) for p in Path(work_dir).rglob("*")
        if p.is_file()
        and str(p) not in before
        and p.suffix.lower() not in _IGNORED_SUFFIXES
    ]
    if not files:
        raise ValueError("yt-dlp: tidak ada file yang terdownload dari Facebook.")
    return title, sorted(files)


def _facebook_download_sync(url: str, work_dir: str) -> tuple[str, list[str]]:
    """
    Download video Facebook — multi-strategy tanpa login:
      1. cobalt.tools API  (paling handal untuk video & reel publik)
      2. yt-dlp            (fallback, berhasil untuk banyak video embed publik)
      3. HTML scraping     (last-resort, untuk URL yang tidak didukung API lain)
    Setiap strategi ditulis ke subdirektori terpisah agar tidak saling mengganggu.
    """
    import urllib.error as _ue

    # Frasa dalam pesan error yang menandakan kegagalan TRANSIEN (bukan konten privat)
    _FB_TRANSIENT_PHRASES = (
        "coba lagi nanti",
        "layanan download tidak tersedia",
        "gagal menghubungi layanan",
        "connection reset",
        "temporarily unavailable",
        "http error 429",
        "http error 500",
        "http error 502",
        "http error 503",
        "http error 504",
        "timed out",
        "timeout",
    )

    strategies = [
        ("cobalt",      lambda d: _cobalt_download_sync(url, d)),
        ("ytdlp",       lambda d: _facebook_ytdlp_sync(url, d)),
        ("html-scrape", lambda d: _facebook_html_scrape_sync(url, d)),
    ]

    last_error = ""
    has_transient = False  # apakah ada error transien (bukan konten privat)?
    for name, fn in strategies:
        sub_dir = os.path.join(work_dir, name)
        os.makedirs(sub_dir, exist_ok=True)
        try:
            logger.info("[social] Facebook: mencoba strategi %s: %s", name, url)
            title, files = fn(sub_dir)
            if files:
                logger.info("[social] Facebook: berhasil dengan strategi %s (%d file)", name, len(files))
                return title, files
        except Exception as exc:
            last_error = str(exc)
            logger.warning("[social] Facebook: strategi %s gagal: %s", name, exc)
            # Tandai transien jika: network/OS error, atau pesan mengindikasikan masalah server sementara
            _msg_lower = str(exc).lower()
            if isinstance(exc, (OSError, ConnectionError, _ue.URLError)) or any(
                p in _msg_lower for p in _FB_TRANSIENT_PHRASES
            ):
                has_transient = True

    # Jika salah satu strategi gagal karena error transien (bukan konten privat),
    # raise dengan pesan yang cocok _TRANSIENT_PHRASES agar download_public_media
    # otomatis retry sekali setelah 3 detik.
    if has_transient:
        logger.info("[social] Facebook: semua strategi gagal tapi ada error transien — akan di-retry: %s", last_error)
        raise ValueError(
            "temporarily unavailable: semua strategi Facebook gagal (error transien). "
            f"Detail terakhir: {last_error}"
        )

    # Deteksi jenis konten dari URL agar pesan error tepat
    _ul = url.lower()
    if any(x in _ul for x in ("/share/p/", "/photo/", "/photos/")):
        _media = "foto"
    elif any(x in _ul for x in ("/share/v/", "/videos/", "/video/", "/reel/")):
        _media = "video atau Reel"
    else:
        _media = "konten"

    raise ValueError(
        f"❌ Gagal mendownload {_media} Facebook.\n"
        "Pastikan konten bersifat <b>publik</b> dan link masih aktif.\n\n"
        "<i>Konten privat, yang hanya untuk teman, atau yang memerlukan "
        "login tidak bisa didownload.</i>"
    )


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
    # Retry hingga 2x untuk error transien (429, 5xx, network blip)
    _cobalt_last_exc: Exception | None = None
    for _attempt in range(3):
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            data = _json.loads(resp.read())
            _cobalt_last_exc = None
            break
        except urllib.error.HTTPError as exc:
            _cobalt_last_exc = exc
            if exc.code in (429, 500, 502, 503, 504) and _attempt < 2:
                import time as _time
                _time.sleep(3 * (_attempt + 1))
                continue
            raise ValueError(
                f"❌ Layanan download tidak tersedia saat ini (HTTP {exc.code}). Coba lagi nanti."
            ) from exc
        except Exception as exc:
            _cobalt_last_exc = exc
            if _attempt < 2:
                import time as _time
                _time.sleep(3)
                continue
            raise ValueError(
                "❌ Gagal menghubungi layanan download. Coba lagi nanti."
            ) from exc
    if _cobalt_last_exc:
        raise ValueError("❌ Gagal menghubungi layanan download. Coba lagi nanti.") from _cobalt_last_exc

    status = data.get("status")
    logger.info("[social] cobalt status=%s url=%s", status, url)

    if status == "error":
        code = (data.get("error") or {}).get("code", "unknown")
        raise ValueError(
            "❌ Gagal mendownload.\n"
            "Pastikan link masih aktif dan bersifat publik.\n"
            f"<i>Kode: {code}</i>"
        )

    def _dl(dl_url: str, dest: str) -> str:
        """Download file ke dest, kembalikan Content-Type dari response."""
        req2 = urllib.request.Request(dl_url, headers={"User-Agent": _COBALT_UA})
        content_type = "application/octet-stream"
        with urllib.request.urlopen(req2, timeout=120) as r:
            content_type = r.headers.get("Content-Type", content_type)
            with open(dest, "wb") as f:
                while True:
                    chunk = r.read(512 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
        return content_type

    files: list[str] = []

    if status in ("tunnel", "redirect"):
        dl_url = data.get("url")
        if not dl_url:
            raise ValueError("❌ Tidak ada link download yang tersedia.")
        # Download dulu ke file sementara, lalu rename sesuai Content-Type
        tmp = os.path.join(work_dir, "001_media.tmp")
        ct  = _dl(dl_url, tmp)
        if "image" in ct:
            dest = os.path.join(work_dir, "001_media.jpg")
        else:
            dest = os.path.join(work_dir, "001_media.mp4")
        os.rename(tmp, dest)
        files.append(dest)
        logger.info("[social] cobalt single downloaded: %d bytes (ct=%s)", os.path.getsize(dest), ct)

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


def _is_youtube_link(url: str) -> bool:
    try:
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(
        hostname == d or hostname.endswith(f".{d}") for d in _YOUTUBE_DOMAINS
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
    # Retry hingga 2x untuk network error atau kode non-0 yang bersifat transien
    data: dict = {}
    for _attempt in range(3):
        try:
            resp = urllib.request.urlopen(req, timeout=20)
            data = _json.loads(resp.read())
        except Exception as exc:
            if _attempt < 2:
                import time as _time
                _time.sleep(3)
                continue
            raise ValueError(
                "❌ Gagal menghubungi layanan download TikTok. Coba lagi nanti."
            ) from exc
        if data.get("code") == 0:
            break
        # Kode non-0: kemungkinan rate-limit sementara, coba lagi
        if _attempt < 2:
            import time as _time
            _time.sleep(3)
        else:
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


def _youtube_pytubefix_sync(url: str, work_dir: str,
                            client: str = "TV_EMBEDDED") -> tuple[str, list[str]]:
    """
    Download YouTube via pytubefix — menggunakan InnerTube API langsung.
    Dicoba dengan TV_EMBEDDED terlebih dahulu (bypass PO token),
    lalu fallback ke WEB_EMBEDDED jika gagal.
    """
    try:
        from pytubefix import YouTube  # type: ignore
    except ImportError as exc:
        raise ValueError("pytubefix tidak terinstall.") from exc

    last_exc: Exception | None = None
    for _client in (client, "WEB_EMBEDDED", "ANDROID"):
        try:
            kwargs: dict = {"client": _client, "use_oauth": False, "allow_oauth_cache": False}
            if _YT_COOKIE_FILE and os.path.isfile(_YT_COOKIE_FILE):
                kwargs["use_oauth"] = False   # cookies sudah cukup
            yt = YouTube(url, **kwargs)
            title = yt.title or "YouTube video"

            # Coba progressive stream (video+audio dalam 1 file) <=720p
            stream = (
                yt.streams
                .filter(progressive=True, file_extension="mp4")
                .order_by("resolution")
                .last()
            )
            if not stream:
                stream = yt.streams.get_highest_resolution()
            if not stream:
                raise ValueError(f"pytubefix [{_client}]: tidak ada stream yang tersedia.")

            out_path = stream.download(output_path=work_dir, filename=f"001_video_{_client}.mp4")
            if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
                raise ValueError(f"pytubefix [{_client}]: file kosong setelah download.")

            # Rename ke nama standar
            final_path = os.path.join(work_dir, "001_video.mp4")
            os.replace(out_path, final_path)
            logger.info("[social] pytubefix berhasil dengan client %s", _client)
            return title, [final_path]
        except Exception as exc:
            last_exc = exc
            logger.warning("[social] pytubefix client %s gagal: %s", _client, exc)
            continue

    raise ValueError(f"pytubefix: semua client gagal. Terakhir: {last_exc}") from last_exc


def _youtube_try_player(url: str, work_dir: str, player_client: str,
                        skip_webpage: bool = False) -> tuple[str, list[str]]:
    """
    Coba download YouTube dengan satu player_client yt-dlp tertentu.
    Lempar exception jika gagal.
    """
    before = {str(p) for p in Path(work_dir).rglob("*") if p.is_file()}
    output_template = str(Path(work_dir) / "%(autonumber)03d_%(title).80s.%(ext)s")
    extractor_args: dict = {"player_client": [player_client]}
    if skip_webpage:
        extractor_args["player_skip"] = ["webpage"]

    # Header sesuai client agar terlihat seperti klien asli
    _MOBILE_UA = (
        "com.google.android.youtube/19.29.37 (Linux; U; Android 11) gzip"
    )
    _TV_UA = (
        "Mozilla/5.0 (SMART-TV; Linux; Tizen 6.0) AppleWebKit/538.1 "
        "(KHTML, like Gecko) Version/6.0 TV Safari/538.1"
    )
    if player_client in ("android", "android_vr"):
        http_headers = {"User-Agent": _MOBILE_UA}
    elif player_client in ("tv_embedded",):
        http_headers = {"User-Agent": _TV_UA}
    else:
        http_headers = {"User-Agent": _FB_UA}   # desktop browser UA

    options = {
        "outtmpl":                       output_template,
        "format":                        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[ext=mp4]/best",
        "merge_output_format":           "mp4",
        "noplaylist":                    True,
        "quiet":                         True,
        "no_warnings":                   True,
        "no_color":                      True,
        "restrictfilenames":             True,
        "writethumbnail":                False,
        "writeinfojson":                 False,
        "writesubtitles":                False,
        "writeautomaticsub":             False,
        "socket_timeout":                30,
        "retries":                       3,
        "fragment_retries":              3,
        "extractor_retries":             3,
        "concurrent_fragment_downloads": 2,
        "http_headers":                  http_headers,
        "extractor_args":                {"youtube": extractor_args},
    }
    if _YT_COOKIE_FILE and os.path.isfile(_YT_COOKIE_FILE):
        options["cookiefile"] = _YT_COOKIE_FILE
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

    title = (info or {}).get("title") or "YouTube video"
    files = [
        str(p) for p in Path(work_dir).rglob("*")
        if p.is_file()
        and str(p) not in before
        and p.suffix.lower() not in _IGNORED_SUFFIXES
    ]
    if not files:
        raise ValueError(f"yt-dlp [{player_client}]: tidak ada file yang terdownload.")
    return title, sorted(files)


def _youtube_download_sync(url: str, work_dir: str) -> tuple[str, list[str]]:
    """
    Download YouTube — multi-strategy berurutan (urutan dari yang paling
    andal di server, tanpa cookies):

      1. yt-dlp tv_embedded          — bypass PO token; ambil visitor_data dari halaman
      2. yt-dlp ios                  — mobile InnerTube, minim bot detection
      3. yt-dlp android              — mobile InnerTube, alternatif ios
      4. pytubefix (TV_EMBEDDED)     — InnerTube langsung via pytubefix
      5. yt-dlp tv_embedded+skip     — tv_embedded tanpa fetch halaman (lebih cepat, tapi
                                       tanpa visitor_data)
      6. yt-dlp web_embedded         — embedded player, kadang lolos di mana web gagal
      7. yt-dlp web_creator          — fallback terakhir yt-dlp
    """
    strategies: list[tuple[str, object]] = []

    # Jika cookies tersedia: tambahkan strategi web (paling kompatibel) di awal.
    # Cookies membuat server terlihat seperti browser yang sudah login, sehingga
    # semua client — termasuk web biasa — bekerja tanpa PO token workaround.
    if _YT_COOKIE_FILE and os.path.isfile(_YT_COOKIE_FILE):
        strategies += [
            ("ytdlp-web-cookies",     lambda d: _youtube_try_player(url, d, "web")),
            ("ytdlp-ios-cookies",     lambda d: _youtube_try_player(url, d, "ios")),
        ]

    strategies += [
        ("ytdlp-tv_emb",          lambda d: _youtube_try_player(url, d, "tv_embedded")),
        ("ytdlp-ios",             lambda d: _youtube_try_player(url, d, "ios")),
        ("ytdlp-android",         lambda d: _youtube_try_player(url, d, "android")),
        ("pytubefix",             lambda d: _youtube_pytubefix_sync(url, d)),
        ("ytdlp-tv_emb-skip",     lambda d: _youtube_try_player(url, d, "tv_embedded", skip_webpage=True)),
        ("ytdlp-web_embedded",    lambda d: _youtube_try_player(url, d, "web_embedded")),
        ("ytdlp-web_creator",     lambda d: _youtube_try_player(url, d, "web_creator")),
    ]

    last_error = ""
    for name, fn in strategies:
        sub_dir = os.path.join(work_dir, name)
        os.makedirs(sub_dir, exist_ok=True)
        try:
            logger.info("[social] YouTube: mencoba strategi %s: %s", name, url)
            title, files = fn(sub_dir)
            if files:
                logger.info("[social] YouTube: berhasil dengan strategi %s (%d file)", name, len(files))
                return title, files
        except Exception as exc:
            last_error = str(exc)
            logger.warning("[social] YouTube: strategi %s gagal: %s", name, exc)

    raise ValueError(
        "❌ Gagal mendownload video YouTube.\n"
        "YouTube memblokir download otomatis dari server ini.\n\n"
        "<i>Coba lagi nanti, atau download manual via browser.</i>"
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
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": 2,
    }

    # ── YouTube: multi-strategy dengan Android/iOS player client ──────────
    if _is_youtube_link(url):
        logger.info("[social] YouTube detected, using multi-strategy: %s", url)
        return _youtube_download_sync(url, work_dir)

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
    except Exception as exc:
        # Retry sekali otomatis untuk error transien:
        # - ValueError dengan pesan transien (misal Instagram empty media response)
        # - Network-level errors: OSError, urllib.error.URLError, ConnectionError
        _is_network_err = isinstance(exc, (OSError, ConnectionError))
        _is_transient_val = isinstance(exc, ValueError) and _is_transient_error(str(exc))
        if _is_transient_val or _is_network_err:
            logger.info("[social] transient error, retrying once in 3s: %s", exc)
            await asyncio.sleep(3)
            try:
                title, files = await asyncio.to_thread(_download_sync, url, work_dir)
                return title, files, work_dir
            except Exception:
                shutil.rmtree(work_dir, ignore_errors=True)
                raise
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def cleanup_download(directory: str) -> None:
    shutil.rmtree(directory, ignore_errors=True)
