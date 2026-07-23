import asyncio
from pyrogram import Client
from pyrogram.errors import AuthKeyUnregistered, UserDeactivated, SessionRevoked
from config import API_ID, API_HASH, BOT_TOKEN
from database.db import db
from logger import logger


class SessionManager:

    def __init__(self):
        self._sessions: dict[int, Client] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._public_session: Client | None = None
        self._public_lock = asyncio.Lock()

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

    async def get_public(self) -> Client | None:
        """Sesi bot untuk membaca channel publik tanpa login sebagai user."""
        async with self._public_lock:
            if self._public_session is not None:
                if self._public_session.is_connected:
                    return self._public_session
                self._public_session = None

            client = None
            try:
                client = Client(
                    name="public_bot",
                    api_id=API_ID,
                    api_hash=API_HASH,
                    bot_token=BOT_TOKEN,
                    in_memory=True,
                )
                await client.start()
                self._public_session = client
                logger.info("Public Telegram session started")
                return client
            except Exception as e:
                logger.error(f"Public session error: {e}")
                if client is not None:
                    try:
                        await client.stop()
                    except Exception:
                        pass
                return None

    async def get_for_chat(self, user_id: int, chat) -> Client | None:
        """Pilih sesi bot untuk publik, atau sesi user untuk chat private."""
        if isinstance(chat, str) and chat.startswith("@"):
            return await self.get_public()
        return await self.get(user_id)

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
        if self._public_session is not None:
            try:
                await self._public_session.stop()
            except Exception:
                pass
            self._public_session = None


session_manager = SessionManager()
