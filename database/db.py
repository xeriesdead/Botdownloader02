import sqlite3
import threading


class Database:
    def __init__(self, path="data/bot.db"):
        os_import = __import__("os")
        os_import.makedirs("data", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self.lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id        INTEGER PRIMARY KEY,
                    username       TEXT    DEFAULT '',
                    session_string TEXT,
                    phone          TEXT,
                    quota          INTEGER DEFAULT 5,
                    bonus_quota    INTEGER DEFAULT 0,
                    premium        INTEGER DEFAULT 0,
                    premium_until  TEXT,
                    target         TEXT,
                    referrer_id    INTEGER,
                    last_reset     TEXT    DEFAULT (date('now')),
                    banned         INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS activity_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER NOT NULL,
                    event_type TEXT    NOT NULL,
                    detail     TEXT,
                    created_at TEXT    DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_activity_user  ON activity_log(user_id);
                CREATE INDEX IF NOT EXISTS idx_activity_date  ON activity_log(created_at);
                CREATE INDEX IF NOT EXISTS idx_activity_type  ON activity_log(event_type);
                CREATE TABLE IF NOT EXISTS bot_config (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
            # Migrasi kolom baru agar DB lama tidak error
            existing = {row[1] for row in self.conn.execute("PRAGMA table_info(users)")}
            migrations = {
                "bonus_quota":   "ALTER TABLE users ADD COLUMN bonus_quota INTEGER DEFAULT 0",
                "premium_until": "ALTER TABLE users ADD COLUMN premium_until TEXT",
                "referrer_id":   "ALTER TABLE users ADD COLUMN referrer_id INTEGER",
                "phone":         "ALTER TABLE users ADD COLUMN phone TEXT",
                "last_reset":    "ALTER TABLE users ADD COLUMN last_reset TEXT",
                "banned":        "ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0",
                "login_at":      "ALTER TABLE users ADD COLUMN login_at TEXT",
            }
            for col, sql in migrations.items():
                if col not in existing:
                    self.conn.execute(sql)
            self.conn.commit()

    # ------------------------------------------------------------------ #
    #  Generic helpers
    # ------------------------------------------------------------------ #

    def execute(self, query: str, params: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(query, params)
            self.conn.commit()
            return cur.rowcount

    def fetchone(self, query: str, params: tuple = ()):
        with self.lock:
            cur = self.conn.execute(query, params)
            row = cur.fetchone()
            return dict(row) if row else None

    def fetchall(self, query: str, params: tuple = ()):
        with self.lock:
            cur = self.conn.execute(query, params)
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    #  User helpers
    # ------------------------------------------------------------------ #

    def create_user(self, user_id: int, username: str = ""):
        self.execute(
            "INSERT OR IGNORE INTO users(user_id, username) VALUES (?, ?)",
            (user_id, username),
        )

    def get_user(self, user_id: int):
        return self.fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))

    def get_all_users(self):
        return self.fetchall("SELECT user_id FROM users")

    def update(self, query: str, params: tuple):
        return self.execute(query, params)

    def is_premium(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        if not user or user.get("premium") != 1:
            return False
        until = user.get("premium_until")
        if not until:
            return True
        from datetime import datetime, timezone
        try:
            return datetime.fromisoformat(until) > datetime.now(tz=timezone.utc)
        except Exception:
            return True

    def is_banned(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        return bool(user and user.get("banned") == 1)

    def total_quota(self, user_id: int) -> int:
        user = self.get_user(user_id)
        if not user:
            return 0
        return (user.get("quota") or 0) + (user.get("bonus_quota") or 0)

    def config_get(self, key: str) -> str | None:
        row = self.fetchone("SELECT value FROM bot_config WHERE key = ?", (key,))
        return row["value"] if row else None

    def config_set(self, key: str, value: str):
        self.execute(
            "INSERT INTO bot_config(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def config_delete(self, key: str):
        self.execute("DELETE FROM bot_config WHERE key = ?", (key,))

    def reset_daily_quota(self, user_id: int, amount: int = 5):
        self.execute(
            "UPDATE users SET quota = ?, last_reset = date('now') WHERE user_id = ?",
            (amount, user_id),
        )


db = Database()
