import re
from urllib.parse import urlparse

_HOST = re.compile(r"^(?:www\.)?(?:t\.me|telegram\.me)$", re.IGNORECASE)
_USERNAME = re.compile(r"^[A-Za-z0-9_]+$")


def parse_telegram_link(link: str):
    """
    Kembalikan (chat, message_id).
    Private channel → chat berupa integer -100xxx.
    Public → chat berupa '@username'.
    Gagal → (None, None).

    Format yang didukung (domain t.me maupun telegram.me):
      t.me/username/123              → public, msg 123
      t.me/username/4/123?single     → public topic, msg 123
      t.me/c/1234567890/123         → private, msg 123
      t.me/c/1234567890/4/123       → private topic, msg 123
      telegram.me/username/123      → public, msg 123
      telegram.me/c/1234567890/123  → private, msg 123
    """
    raw = (link or "").strip()
    if not raw:
        return None, None

    # urlparse treats "t.me/..." as a path, so add a scheme for schemeless
    # links while still accepting the https:// form sent by Telegram.
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if not _HOST.fullmatch(parsed.netloc):
        return None, None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None, None

    # Topic/thread links contain an extra numeric segment. Telegram's actual
    # message ID is always the final path segment before the query string.
    message_part = parts[-1]
    if not message_part.isdigit():
        return None, None
    msg_id = int(message_part)

    if parts[0].lower() == "c":
        if len(parts) < 3 or not parts[1].isdigit():
            return None, None
        return int(f"-100{parts[1]}"), msg_id

    if _USERNAME.fullmatch(parts[0]) and all(part.isdigit() for part in parts[1:]):
        return f"@{parts[0]}", msg_id

    return None, None


def is_public_chat(chat) -> bool:
    """True untuk chat publik yang direpresentasikan sebagai @username."""
    return isinstance(chat, str) and chat.startswith("@")


def is_single_message_link(link: str) -> bool:
    """True jika link Telegram memiliki query `single` dari link album."""
    raw = (link or "").strip()
    if not raw:
        return False

    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return any(
        part.split("=", 1)[0].strip().lower() == "single"
        for part in parsed.query.split("&")
        if part
    )
