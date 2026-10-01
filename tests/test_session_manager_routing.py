import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch


os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "test")

from modules.session_manager import SessionManager


class SessionManagerRoutingTests(unittest.TestCase):
    def test_public_fallback_prefers_logged_in_user_session(self):
        manager = SessionManager()
        user_client = object()
        public_client = object()

        async def scenario():
            with (
                patch.object(
                    manager, "get", new=AsyncMock(return_value=user_client)
                ) as get_user,
                patch.object(
                    manager, "get_public", new=AsyncMock(return_value=public_client)
                ) as get_public,
            ):
                client = await manager.get_for_chat(
                    123, "@lembukacukan34", prefer_user=True
                )
                return client, get_user, get_public

        client, get_user, get_public = asyncio.run(scenario())
        self.assertIs(client, user_client)
        get_user.assert_awaited_once_with(123)
        get_public.assert_not_awaited()

    def test_public_fallback_keeps_bot_session_when_user_session_missing(self):
        manager = SessionManager()
        public_client = object()

        async def scenario():
            with (
                patch.object(manager, "get", new=AsyncMock(return_value=None)),
                patch.object(
                    manager, "get_public", new=AsyncMock(return_value=public_client)
                ) as get_public,
            ):
                client = await manager.get_for_chat(
                    123, "@lembukacukan34", prefer_user=True
                )
                return client, get_public

        client, get_public = asyncio.run(scenario())
        self.assertIs(client, public_client)
        get_public.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()