import asyncio
import os
import time
import unittest


# safe_forward imports the application configuration at module import time.
# These placeholders keep this unit test independent from live Telegram secrets.
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "test")

from modules import safe_forward


class SafeForwardProgressTests(unittest.TestCase):
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