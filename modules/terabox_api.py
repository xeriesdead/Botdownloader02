import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


_API_URL = "https://api.teraboxdl.site/v1/api"
_REQUEST_TIMEOUT = 40
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TERABOX_BUTTON_ITEMS = 20

TERABOX_DOMAINS = frozenset({
    "1024terabox.com",
    "1024tera.com",
    "terabox.com",
    "teraboxapp.com",
    "terabox.app",
    "freeterabox.com",
    "4funbox.com",
    "teraboxlink.com",
    "terasharelink.com",
    "terafileshare.com",
    "terabox.fun",
    "terabox.club",
    "teraboxdownload.com",
})


class TeraboxApiError(ValueError):
    """The TeraBox extraction API could not return usable links."""


@dataclass(frozen=True)
class TeraboxFile:
    filename: str
    size_bytes: int | None
    direct_url: str | None
    stream_url: str | None


def is_terabox_link(url: str) -> bool:
    try:
        parsed = urlsplit((url or "").strip())
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return False

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.strip("/")
        or any(char.isspace() for char in hostname)
    ):
        return False

    default_port = 443 if parsed.scheme.lower() == "https" else 80
    if port is not None and port != default_port:
        return False

    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in TERABOX_DOMAINS
    )


def _safe_action_url(value) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or any(char.isspace() for char in candidate):
        return None
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(char.isspace() for char in parsed.hostname)
        or port not in (None, 443)
    ):
        return None
    return candidate


def terabox_button_rows(
    files: list[TeraboxFile],
) -> list[list[tuple[str, str]]]:
    """Return a bounded Telegram-button layout for the available file actions."""
    rows = []
    for index, item in enumerate(files[:MAX_TERABOX_BUTTON_ITEMS], 1):
        row = []
        if item.stream_url:
            row.append((f"▶️ Streaming {index}", item.stream_url))
        if item.direct_url:
            row.append((f"⬇️ Download {index}", item.direct_url))
        if row:
            rows.append(row)
    return rows


def _request_page(source_url: str, api_key: str, api_secret: str) -> dict:
    body = json.dumps(
        {"url": source_url, "dir_path": "", "page": 1},
        separators=(",", ":"),
    )
    timestamp = str(int(time.time()))
    message = f"POST/v1/api{timestamp}{body}"
    signature = hmac.new(
        api_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    request = Request(
        _API_URL,
        data=body.encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "X-Timestamp": timestamp,
            "X-Signature": signature,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=_REQUEST_TIMEOUT) as response:
            raw_body = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise TeraboxApiError(
                "❌ Kredensial AR Digital ditolak. Periksa "
                "TERABOX_API_KEY dan TERABOX_API_SECRET di Railway."
            ) from exc
        if exc.code == 429:
            raise TeraboxApiError(
                "❌ Kuota atau batas permintaan API AR Digital tercapai."
            ) from exc
        raise TeraboxApiError(
            f"❌ API AR Digital mengembalikan HTTP {exc.code}."
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        reason = exc.reason if isinstance(exc, URLError) else exc
        if isinstance(reason, TimeoutError):
            raise TeraboxApiError(
                "❌ API TeraBox terlalu lama merespons. Coba lagi nanti."
            ) from exc
        raise TeraboxApiError(
            "❌ API TeraBox tidak dapat dijangkau. Coba lagi nanti."
        ) from exc

    if len(raw_body) > _MAX_RESPONSE_BYTES:
        raise TeraboxApiError("❌ Respons API TeraBox terlalu besar.")
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TeraboxApiError("❌ API TeraBox mengembalikan respons yang tidak valid.") from exc

    if not isinstance(payload, dict):
        raise TeraboxApiError("❌ Format respons API TeraBox tidak dikenali.")

    error_code = payload.get("errno")
    if error_code not in (None, 0, "0"):
        code_display = str(error_code)[:30]
        if not code_display.isdigit():
            code_display = "unknown"
        raise TeraboxApiError(
            f"❌ AR Digital tidak dapat memproses tautan (kode {code_display})."
        )
    return payload


def resolve_terabox_files(
    source_url: str,
    api_key: str | None,
    api_secret: str | None,
) -> tuple[str, list[TeraboxFile]]:
    """Resolve a public TeraBox share link to browser streaming/download URLs."""
    if not is_terabox_link(source_url):
        raise TeraboxApiError("❌ Format tautan TeraBox tidak didukung.")
    if not api_key or not api_key.strip() or not api_secret or not api_secret.strip():
        raise TeraboxApiError(
            "❌ Fitur TeraBox belum dikonfigurasi. Pengelola bot perlu "
            "menambahkan TERABOX_API_KEY dan TERABOX_API_SECRET ke Railway Variables."
        )

    payload = _request_page(source_url.strip(), api_key.strip(), api_secret.strip())
    entries = payload.get("list")
    if not isinstance(entries, list):
        raise TeraboxApiError("❌ API TeraBox tidak mengembalikan daftar file.")

    files = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        direct_url = _safe_action_url(entry.get("direct_link"))
        stream_url = _safe_action_url(entry.get("stream_url"))
        if not direct_url and not stream_url:
            continue

        filename = entry.get("server_filename")
        if not isinstance(filename, str) or not filename.strip():
            filename = f"File {len(files) + 1}"
        size_bytes = entry.get("size")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            size_bytes = None
        files.append(
            TeraboxFile(
                filename=filename.strip()[:200],
                size_bytes=size_bytes,
                direct_url=direct_url,
                stream_url=stream_url,
            )
        )

    if not files:
        raise TeraboxApiError(
            "❌ Tidak ditemukan file yang memiliki tautan streaming atau download."
        )

    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        title = "TeraBox"
    return title.strip()[:180], files
