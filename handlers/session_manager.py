import asyncio
from pyrogram import Client
from pyrogram.errors import AuthKeyUnregistered, UserDeactivated, SessionRevoked
from config import API_ID, API_HASH
from database.db import db
from logger import logger


class SessionManager:

    def __init__(self):
        self._sessions: dict[int, Client] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    async def get(self, user_id: int) -> Client | None:
        async with self._lock(user_id):
            if user_id in self._sessions:
                c = self._sessions[user_id]
                if c.is_connected:
                    if c.me is None:
                        try:
                            c.me = await c.get_me()
                        except Exception:
                            pass
                    return c
                del self._sessions[user_id]

            user = db.get_user(user_id)
            if not user or not user.get("session_string"):
                return None

            try:
                client = Client(
                    name=f"user_{user_id}",
                    api_id=API_ID,
                    api_hash=API_HASH,
                    session_string=user["session_string"],
                    in_memory=True,
                    device_model="BOT Downloader",
                    app_version="1.0",
                    system_version="BOT Downloader Server",
                )
                await client.connect()
                client.me = await client.get_me()
                self._sessions[user_id] = client
                return client

            except (AuthKeyUnregistered, UserDeactivated, SessionRevoked):
                logger.warning(f"Session expired user {user_id}")
                db.update("UPDATE users SET session_string = NULL WHERE user_id = ?", (user_id,))
                return None

            except Exception as e:
                logger.error(f"Session error user {user_id}: {e}")
                return None

    async def close(self, user_id: int):
        if user_id in self._sessions:
            try:
                await self._sessions[user_id].disconnect()
            except Exception:
                pass
            del self._sessions[user_id]

    async def close_all(self):
        for uid in list(self._sessions.keys()):
            await self.close(uid)


session_manager = SessionManager()
