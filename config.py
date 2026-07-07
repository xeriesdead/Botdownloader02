import os


def _require(key: str) -> str:
    value = os.getenv(key)
    if value is None:
        raise EnvironmentError(
            f"Environment variable '{key}' belum diset. "
            "Tambahkan di Replit Secrets."
        )
    return value


API_ID    = int(_require("API_ID"))
API_HASH  = _require("API_HASH")
BOT_TOKEN = _require("BOT_TOKEN")

# ID Telegram admin (pisahkan dengan koma jika lebih dari satu)
# Contoh: ADMIN_IDS=123456789,987654321
_raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set[int] = {
    int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()
}

# Channel wajib join (username tanpa @, atau ID numerik)
# Contoh: REQUIRED_CHANNEL=mychannel  atau  REQUIRED_CHANNEL=-1001234567890
REQUIRED_CHANNEL: str | None = os.getenv("REQUIRED_CHANNEL") or None

# Batas ukuran file download untuk user reguler (default 1024 MB = 1 GB)
# File ≤50 MB: dikirim langsung via Bot API ke chat bot
# File 50 MB–1 GB (channel private): otomatis dikirim ke Saved Messages + notifikasi
MAX_FILE_SIZE_MB: int    = int(os.getenv("MAX_FILE_SIZE_MB", "1024"))
MAX_FILE_SIZE_BYTES: int = MAX_FILE_SIZE_MB * 1024 * 1024

# Batas ukuran file download untuk user Premium (default 2048 MB = 2 GB)
MAX_FILE_SIZE_MB_PREMIUM: int    = int(os.getenv("MAX_FILE_SIZE_MB_PREMIUM", "2048"))
MAX_FILE_SIZE_BYTES_PREMIUM: int = MAX_FILE_SIZE_MB_PREMIUM * 1024 * 1024

# Sisa quota yang memicu notifikasi (default 2)
QUOTA_WARN_THRESHOLD: int = int(os.getenv("QUOTA_WARN_THRESHOLD", "2"))
