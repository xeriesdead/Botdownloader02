import re
import os
import asyncio
import aiohttp
import aiofiles
from typing import Optional
from database.db import db
from logger import logger

TERABOX_DOMAINS = (
    "terabox.com", "1024terabox.com", "teraboxapp.com",
    "4funbox.com", "nephobox.com", "mirrobox.com",
    "1024tera.com", "teraboxlink.com", "momerybox.com",
)

_APP_ID  = "250528"
_CHUNK   = 512 * 1024
_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10)
_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def is_terabox_link(url: str) -> bool:
    return any(d in url for d in TERABOX_DOMAINS)


def extract_share_key(url: str) -> Optional[str]:
    m = re.search(r"/s/([A-Za-z0-9_\-]+)", url)
    return m.group(1) if m else None


def get_bduss() -> Optional[str]:
    """Ambil session cookie Terabox (ndus / BDUSS) yang tersimpan di database."""
    return db.config_get("terabox_bduss")


def _build_cookie_header(raw: str) -> str:
    """
    Buat Cookie header yang benar dari nilai yang disimpan.
    - 'ABC123...'       → ndus=ABC123...  (default)
    - 'ndus=ABC123...'  → dikirim apa adanya
    - 'BDUSS=ABC123...' → dikirim apa adanya
    """
    raw = raw.strip()
    if "=" in raw.split(";")[0]:
        return raw
    return f"ndus={raw}"


def _base_headers(cookie_val: Optional[str] = None) -> dict:
    h = {
        "User-Agent":      _DESKTOP_UA,
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if cookie_val:
        h["Cookie"] = _build_cookie_header(cookie_val)
    return h


async def get_terabox_files(url: str) -> tuple[list[dict], str]:
    """
    Ambil info file dari Terabox share link.
    Return (list_of_files, surl)
    """
    key = extract_share_key(url)
    if not key:
        raise ValueError("Format link Terabox tidak dikenali")

    bduss = get_bduss()
    hdrs  = _base_headers(bduss)

    try:
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(),
            timeout=_TIMEOUT,
        ) as session:

            # Step 1: visit share URL → dapat base host + surl dari redirect
            logger.info(f"[terabox] visiting share URL: {url}")
            async with session.get(url, headers=hdrs, allow_redirects=True) as r:
                final_url = str(r.url)
                base      = f"https://{r.url.host}"
                m         = re.search(r"surl=([^&\s]+)", final_url)
                surl      = m.group(1) if m else key.lstrip("1")
                logger.info(f"[terabox] redirected to base={base} surl={surl}")

            # Step 2: ambil daftar file
            list_url = (
                f"{base}/share/list?app_id={_APP_ID}&shorturl={surl}"
                f"&root=1&page=1&num=20&order=time&channel=dubox&web=1&vid=1"
            )
            logger.info(f"[terabox] fetching file list: {list_url}")
            async with session.get(
                list_url,
                headers={**hdrs, "Referer": final_url},
            ) as r2:
                data  = await r2.json(content_type=None)
                errno = data.get("errno", -1)
                logger.info(f"[terabox] share/list errno={errno} "
                            f"files={len(data.get('list', []))}")
                if errno != 0:
                    raise ValueError(f"Gagal mengambil daftar file (errno={errno})")

    except aiohttp.ClientConnectorError as e:
        raise ValueError(f"Tidak bisa terhubung ke Terabox: {e}")
    except asyncio.TimeoutError:
        raise ValueError("Terabox tidak merespons (timeout 20 detik). Coba lagi.")

    files = []
    for f in data.get("list", []):
        files.append({
            "filename": f.get("server_filename") or "file",
            "size":     int(f.get("size", 0) or 0),
            "fs_id":    f.get("fs_id"),
            "path":     f.get("path", ""),
            "is_dir":   str(f.get("isdir", "0")) == "1",
            "share_id": data.get("share_id"),
            "uk":       data.get("uk"),
            "base":     base,
            "surl":     surl,
            "final_url":final_url,
        })

    if not files:
        raise ValueError("Tidak ada file di link ini")
    return files, surl


async def get_download_url(file_info: dict) -> str:
    """
    Dapatkan URL download langsung. Memerlukan cookie yang valid.
    """
    bduss = get_bduss()
    if not bduss:
        raise PermissionError("Cookie Terabox belum dikonfigurasi. Ketik /setterabox untuk panduan.")

    hdrs      = _base_headers(bduss)
    base      = file_info["base"]
    uk        = file_info["uk"]
    share_id  = file_info["share_id"]
    fs_id     = file_info["fs_id"]
    final_url = file_info["final_url"]

    dl_url = (
        f"{base}/api/download?app_id={_APP_ID}&channel=dubox"
        f"&clienttype=0&web=1&uk={uk}&shareid={share_id}"
        f"&primaryid={fs_id}&fid_list=[{fs_id}]"
    )

    try:
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(),
            timeout=_TIMEOUT,
        ) as session:
            async with session.get(file_info["final_url"], headers=hdrs, allow_redirects=True):
                pass
            async with session.get(
                dl_url,
                headers={**hdrs, "Referer": final_url},
                allow_redirects=False,
            ) as r:
                d = await r.json(content_type=None)

    except asyncio.TimeoutError:
        raise ValueError("Terabox tidak merespons saat mengambil link download (timeout).")
    except aiohttp.ClientConnectorError as e:
        raise ValueError(f"Tidak bisa terhubung ke Terabox: {e}")

    errno = d.get("errno", -1)
    logger.info(f"[terabox] download API errno={errno}")
    if errno != 0:
        errmsg = {
            -6:     "Server Terabox menolak permintaan dari IP bot ini (errno=-6). "
                    "Fitur ini memerlukan server dengan IP non-datacenter.",
            400310: "Terabox meminta verifikasi tambahan dari IP ini (errno=400310). "
                    "Fitur ini memerlukan server dengan IP non-datacenter.",
        }.get(errno, f"Gagal mendapatkan link download (errno={errno})")
        raise ValueError(errmsg)

    dlinks = d.get("dlink", [])
    if not dlinks:
        raise ValueError("Server tidak mengembalikan link download")
    return dlinks[0].get("dlink", "")


async def download_file(
    dlink: str,
    dest_path: str,
    on_progress=None,
) -> str:
    """Download file dari URL ke dest_path dengan progress callback."""
    bduss   = get_bduss()
    dl_hdrs = {**_base_headers(bduss), "Accept": "*/*"}

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=3600, connect=15),
    ) as session:
        async with session.get(
            dlink, headers=dl_hdrs, allow_redirects=True,
        ) as resp:
            if resp.status not in (200, 206):
                raise ValueError(f"Gagal download: HTTP {resp.status}")

            total      = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)

            async with aiofiles.open(dest_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(_CHUNK):
                    await f.write(chunk)
                    downloaded += len(chunk)
                    if on_progress:
                        try:
                            await on_progress(downloaded, total)
                        except Exception:
                            pass

    return dest_path
