import asyncio
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch


# safe_forward imports the application configuration at module import time.
# These placeholders keep this unit test independent from live Telegram secrets.
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "test")

from modules import safe_forward


class SafeForwardProgressTests(unittest.TestCase):
    def test_video_document_is_classified_and_named_as_mp4(self):
        message = SimpleNamespace(
            id=42,
            photo=None,
            video=None,
            animation=None,
            video_note=None,
            audio=None,
            voice=None,
            sticker=None,
            document=SimpleNamespace(
                file_name=None,
                mime_type="video/mp4",
                file_size=1024,
            ),
        )

        self.assertTrue(safe_forward._is_video_message(message))
        self.assertEqual(safe_forward._album_media_kind(message), "visual")
        self.assertTrue(
            safe_forward._album_download_target(message, 1, 2, 3).endswith(".mp4")
        )

        document_message = SimpleNamespace(
            id=43,
            photo=None,
            video=None,
            animation=None,
            video_note=None,
            audio=None,
            voice=None,
            sticker=None,
            document=SimpleNamespace(
                file_name="notes.pdf",
                mime_type="application/pdf",
            ),
        )
        self.assertFalse(safe_forward._is_video_message(document_message))
        self.assertEqual(
            safe_forward._album_media_kind(document_message),
            "document",
        )

    def test_video_document_upload_uses_video_endpoint_and_mp4_name(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as temp_dir:
                work_dir = os.path.join(temp_dir, "job")
                os.makedirs(work_dir)

                class FakeBot:
                    kind = None
                    filename = None

                    async def send_video(self, _chat_id, **kwargs):
                        self.kind = "video"
                        self.filename = kwargs["video"].name

                    async def send_document(self, _chat_id, **kwargs):
                        self.kind = "document"
                        self.filename = kwargs["document"].name

                async def fake_download(_client, _message, file_name, **_kwargs):
                    with open(file_name, "wb") as media:
                        media.write(b"video bytes")
                    return file_name

                message = SimpleNamespace(
                    id=42,
                    photo=None,
                    video=None,
                    audio=None,
                    voice=None,
                    video_note=None,
                    animation=None,
                    sticker=None,
                    document=SimpleNamespace(
                        file_name=None,
                        mime_type="video/mp4",
                        file_size=11,
                    ),
                    caption="",
                )
                bot = FakeBot()
                with (
                    patch.object(
                        safe_forward, "_new_download_dir", return_value=work_dir
                    ),
                    patch.object(
                        safe_forward, "_download_media", side_effect=fake_download
                    ),
                    patch.object(
                        safe_forward,
                        "_create_video_thumbnail_async",
                        return_value=None,
                    ),
                ):
                    await safe_forward._download_and_send_via_bot(
                        object(), bot, message, 12345
                    )
                return bot.kind, bot.filename

        kind, filename = asyncio.run(scenario())
        self.assertEqual(kind, "video")
        self.assertTrue(filename.endswith(".mp4"))

    def test_unknown_temp_document_is_probed_and_renamed(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as temp_dir:
                path = os.path.join(temp_dir, "telegram_download.temp")
                with open(path, "wb") as media:
                    media.write(b"video bytes")
                message = SimpleNamespace(
                    id=42,
                    video=None,
                    document=SimpleNamespace(
                        file_name=None,
                        mime_type="application/octet-stream",
                    ),
                )
                with patch.object(
                    safe_forward, "_is_mp4_video_file", return_value=True
                ):
                    normalized_path, is_video = (
                        await safe_forward._classify_video_download(message, path)
                    )
                    self.assertTrue(is_video)
                    self.assertTrue(normalized_path.endswith(".mp4"))
                    self.assertTrue(os.path.isfile(normalized_path))
                    return os.path.basename(normalized_path)

        self.assertEqual(asyncio.run(scenario()), "video_42.mp4")

    def test_mtproto_upload_keeps_group_destination(self):
        original_username = safe_forward._BOT_USERNAME
        try:
            safe_forward.set_bot_username("test_bot")
            self.assertEqual(
                safe_forward._pyrogram_delivery_peer(-1001234567890),
                -1001234567890,
            )
            self.assertEqual(
                safe_forward._pyrogram_delivery_peer(12345),
                "@test_bot",
            )
        finally:
            safe_forward.set_bot_username(original_username)

    def test_album_upload_keeps_forum_topic_destination(self):
        async def scenario():
            class FakeBot:
                async def send_media_group(self, chat_id, **kwargs):
                    self.chat_id = chat_id
                    self.kwargs = kwargs
                    return ["sent"]

            bot = FakeBot()
            batch = [(None, None, object(), "photo")]
            result = await safe_forward._send_media_group_with_watchdog(
                bot,
                -1001234567890,
                batch,
                on_progress=None,
                timeout=1,
                message_thread_id=77,
            )
            return bot, result

        bot, result = asyncio.run(scenario())
        self.assertEqual(result, ["sent"])
        self.assertEqual(bot.chat_id, -1001234567890)
        self.assertEqual(bot.kwargs["message_thread_id"], 77)

    def test_album_strategy_keeps_small_photo_album_as_group(self):
        small_photo = SimpleNamespace(
            photo=SimpleNamespace(file_size=5 * 1024 * 1024),
        )
        messages = [small_photo for _ in range(10)]

        self.assertFalse(safe_forward._should_stream_album(messages))

    def test_album_strategy_streams_when_one_file_is_large(self):
        large_video = SimpleNamespace(
            video=SimpleNamespace(file_size=51 * 1024 * 1024),
        )

        self.assertTrue(safe_forward._should_stream_album([large_video]))

    def test_album_strategy_streams_when_total_size_is_large(self):
        medium_photo = SimpleNamespace(
            photo=SimpleNamespace(file_size=20 * 1024 * 1024),
        )
        messages = [medium_photo for _ in range(8)]

        self.assertTrue(safe_forward._should_stream_album(messages))

    def test_stuck_progress_callback_does_not_block_album_transition(self):
        async def stubborn_progress(_text):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                # Simulate a Telegram edit request that is slow to unwind.
                await asyncio.sleep(0.2)

        async def scenario():
            original_timeout = safe_forward._PROGRESS_CALLBACK_TIMEOUT
            safe_forward._PROGRESS_CALLBACK_TIMEOUT = 0.02
            try:
                started_at = time.monotonic()
                await safe_forward._notify_progress(
                    stubborn_progress,
                    "media selesai; lanjut ke media berikutnya",
                )
                elapsed = time.monotonic() - started_at
                await asyncio.sleep(0.25)
                return elapsed
            finally:
                safe_forward._PROGRESS_CALLBACK_TIMEOUT = original_timeout

        elapsed = asyncio.run(scenario())
        self.assertLess(elapsed, 0.12)


if __name__ == "__main__":
    unittest.main()