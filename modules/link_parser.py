import re

_PRIVATE = re.compile(r"(?:t\.me|telegram\.me)/c/(\d+)/(\d+)")
_PUBLIC  = re.compile(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)/(\d+)")


def parse_telegram_link(link: str):
    """
    Kembalikan (chat, message_id).
    Private channel → chat berupa integer -100xxx.
    Public → chat berupa '@username'.
    Gagal → (None, None).

    Format yang didukung (domain t.me maupun telegram.me):
      t.me/username/123              → public, msg 123
      t.me/c/1234567890/123         → private, msg 123
      telegram.me/username/123      → public, msg 123
      telegram.me/c/1234567890/123  → private, msg 123
    """
    m = _PRIVATE.search(link)
    if m:
        return int(f"-100{m.group(1)}"), int(m.group(2))

    m = _PUBLIC.search(link)
    if m:
        return f"@{m.group(1)}", int(m.group(2))

    return None, None
