import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "test")

from modules import safe_forward


class SafeForwardSizeGuardTests(unittest.TestCase):
    def test_single_only_inspection_does_not_expand_album(self):
        message = SimpleNamespace(
            empty=False,
            media_group_id="album-1",
            media=object(),
            video=SimpleNamespace(file_size=None),
        )

        async def scenario():
            with (
                patch.object(
                    safe_forward,
                    "_resolve_source",
                    new=AsyncMock(return_value=("@channel", None)),
                ),
                patch.object(
                    safe_forward, "_get_message", new=AsyncMock(return_value=message)
                ),
                patch.object(
                    safe_forward,
                    "_fetch_album_messages",
                    new=AsyncMock(side_effect=AssertionError("album must not expand")),
                ) as fetch_album,
            ):
                result = await safe_forward.inspect_message_media_size(
                    object(), "@channel", 14274, single_only=True
                )
                return result, fetch_album.await_count

        result, fetch_count = asyncio.run(scenario())
        self.assertEqual(result, (True, None, False))
        self.assertEqual(fetch_count, 0)

    def test_unknown_size_transfer_aborts_when_reported_size_exceeds_limit(self):
        original_poll_interval = safe_forward._TRANSFER_POLL_INTERVAL

        async def scenario():
            safe_forward._TRANSFER_POLL_INTERVAL = 0.005

            async def fake_download(progress):
                await progress(0, 101)
                await asyncio.sleep(1)

            try:
                await safe_forward._run_transfer_with_watchdog(
                    fake_download,
                    timeout=2,
                    operation="test oversized download",
                    max_bytes=100,
                )
            finally:
                safe_forward._TRANSFER_POLL_INTERVAL = original_poll_interval

        with self.assertRaises(safe_forward.MediaTooLargeError):
            asyncio.run(scenario())

    def test_unknown_size_download_checks_actual_file_before_upload(self):
        async def scenario(work_dir, path):
            with (
                patch.object(
                    safe_forward, "_new_download_dir", return_value=work_dir
                ),
                patch.object(
                    safe_forward,
                    "_download_media",
                    new=AsyncMock(return_value=path),
                ) as download,
            ):
                with self.assertRaises(safe_forward.MediaTooLargeError):
                    await safe_forward._download_and_upload_via_pyrogram(
                        object(),
                        object(),
                        SimpleNamespace(id=14274),
                        123,
                        0,
                        max_file_size=100,
                    )
                download.assert_awaited_once()

        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = os.path.join(temp_dir, "transfer")
            os.makedirs(work_dir)
            path = os.path.join(work_dir, "media.bin")
            with open(path, "wb") as media_file:
                media_file.write(b"x" * 101)

            asyncio.run(scenario(work_dir, path))
            self.assertFalse(os.path.exists(work_dir))


if __name__ == "__main__":
    unittest.main()