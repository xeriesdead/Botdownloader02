import os
import re
import threading

import psycopg2
import psycopg2.extras


def _to_pg(query: str) -> str:
    """Ubah placeholder '?' gaya SQLite jadi '%s' gaya PostgreSQL."""
    return query.replace("?", "%s")


class Database:
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.environ.get(
            "DATABASE_URL",
            os.environ.get("Connection_String") or os.environ.get("Connecting String"),
        )
        if not self.dsn:
            raise RuntimeError(
                "DATABASE_URL tidak ditemukan di environment. "
                "Set secret DATABASE_URL dengan connection string PostgreSQL."
            )
        self.lock = threading.Lock()
        self.conn = psycopg2.connect(self.dsn)
        self.conn.autocommit = True
        self._init_db()

    def _init_db(self):
        with self.lock, self.conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id        BIGINT PRIMARY KEY,
                    username       TEXT    DEFAULT '',
                    session_string TEXT,
                    phone          TEXT,
                    quota          INTEGER DEFAULT 5,
                    bonus_quota    INTEGER DEFAULT 0,
                    premium        INTEGER DEFAULT 0,
                    premium_until  TEXT,
                    target         TEXT,
                    referrer_id    BIGINT,
                    last_reset     TEXT    DEFAULT CURRENT_DATE::TEXT,
                    banned         INTEGER DEFAULT 0,
                    login_at       TEXT
                );
                CREATE TABLE IF NOT EXISTS activity_log (
                    id         SERIAL PRIMARY KEY,
                    user_id    BIGINT NOT NULL,
                    event_type TEXT    NOT NULL,
                    detail     TEXT,
                    created_at TEXT    DEFAULT (NOW()::TEXT)
                );
                CREATE INDEX IF NOT EXISTS idx_activity_user  ON activity_log(user_id);
                CREATE INDEX IF NOT EXISTS idx_activity_date  ON activity_log(created_at);
                CREATE INDEX IF NOT EXISTS idx_activity_type  ON activity_log(event_type);
                CREATE TABLE IF NOT EXISTS bot_config (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
            """)

    # ------------------------------------------------------------------ #
    #  Generic helpers
    # ------------------------------------------------------------------ #

    def execute(self, query: str, params: tuple = ()) -> int:
        with self.lock:
            try:
                with self.conn.cursor() as cur:
                    cur.execute(_to_pg(query), params)
                    return cur.rowcount
            except psycopg2.Error:
                self.conn.rollback()
                raise

    def fetchone(self, query: str, params: tuple = ()):
        with self.lock:
            try:
                with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(_to_pg(query), params)
                    row = cur.fetchone()
                    return dict(row) if row else None
            except psycopg2.Error:
                self.conn.rollback()
                raise

    def fetchall(self, query: str, params: tuple = ()):
        with self.lock:
            try:
                with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(_to_pg(query), params)
                    return [dict(r) for r in cur.fetchall()]
            except psycopg2.Error:
                self.conn.rollback()
                raise

    # ------------------------------------------------------------------ #
    #  User helpers
    # ------------------------------------------------------------------ #

    def create_user(self, user_id: int, username: str = ""):
        self.execute(
            "INSERT INTO users(user_id, username) VALUES (?, ?) "
            "ON CONFLICT (user_id) DO NOTHING",
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
            "UPDATE users SET quota = ?, last_reset = CURRENT_DATE::TEXT WHERE user_id = ?",
            (amount, user_id),
        )


db = Database()
