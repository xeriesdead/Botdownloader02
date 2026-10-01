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
from modules.link_parser import is_single_message_link, parse_telegram_link


class SafeForwardSizeGuardTests(unittest.TestCase):
    def test_reported_public_group_link_is_not_a_single_query(self):
        link = "https://t.me/lembukacucukan34/14274/"
        self.assertEqual(
            parse_telegram_link(link),
            ("@lembukacucukan34", 14274),
        )
        self.assertFalse(is_single_message_link(link))

    def test_empty_wrapper_message_uses_raw_api_fallback(self):
        wrapper_message = SimpleNamespace(empty=True)
        raw_message = SimpleNamespace(empty=False, media=object())
        client = SimpleNamespace(
            get_messages=AsyncMock(return_value=wrapper_message)
        )
        progress = AsyncMock()

        async def scenario():
            with patch.object(
                safe_forward,
                "_get_message_via_raw_api",
                new=AsyncMock(return_value=raw_message),
            ) as raw_fetch:
                message = await safe_forward._get_message(
                    client, 12345, 15635, on_progress=progress
                )
                return message, raw_fetch.await_count

        message, raw_count = asyncio.run(scenario())
        self.assertIs(message, raw_message)
        self.assertEqual(raw_count, 1)
        progress.assert_awaited_once()
        self.assertIn("jalur alternatif", progress.await_args.args[0])

    def test_public_username_is_preserved_for_raw_peer_resolution(self):
        wrapper_message = SimpleNamespace(empty=True)
        raw_message = SimpleNamespace(empty=False, media=object())
        client = SimpleNamespace(
            get_messages=AsyncMock(return_value=wrapper_message)
        )

        async def scenario():
            with patch.object(
                safe_forward,
                "_get_message_via_raw_api",
                new=AsyncMock(return_value=raw_message),
            ) as raw_fetch:
                message = await safe_forward._get_message(
                    client,
                    -1004374922177,
                    15636,
                    peer_hint="@lembukacukan34",
                )
                return message, raw_fetch.await_args

        message, raw_args = asyncio.run(scenario())
        self.assertIs(message, raw_message)
        self.assertEqual(raw_args.args[1:3], (-1004374922177, 15636))
        self.assertEqual(raw_args.kwargs["peer_hint"], "@lembukacukan34")

    def test_raw_api_resolves_public_username_instead_of_numeric_peer(self):
        stale_peer = safe_forward.raw.types.InputPeerChannel(
            channel_id=4374922177,
            access_hash=123,
        )
        fresh_resolution = SimpleNamespace(
            peer=safe_forward.raw.types.PeerChannel(channel_id=4374922177),
            chats=[SimpleNamespace(id=4374922177, access_hash=456)],
        )
        client = SimpleNamespace(
            resolve_peer=AsyncMock(return_value=stale_peer),
            invoke=AsyncMock(
                side_effect=[
                    fresh_resolution,
                    SimpleNamespace(
                        messages=[object()],
                        users=[],
                        chats=[],
                    ),
                ]
                )
        )

        async def scenario():
            with patch.object(
                safe_forward.Message,
                "_parse",
                return_value=SimpleNamespace(empty=False),
            ):
                return await safe_forward._get_message_via_raw_api(
                    client,
                    -1004374922177,
                    15636,
                    peer_hint="@lembukacukan34",
                )

        message = asyncio.run(scenario())
        self.assertFalse(message.empty)
        client.resolve_peer.assert_not_awaited()
        self.assertEqual(client.invoke.await_count, 2)
        username_request = client.invoke.await_args_list[0].args[0]
        self.assertIsInstance(
            username_request,
            safe_forward.raw.functions.contacts.ResolveUsername,
        )
        self.assertEqual(username_request.username, "lembukacukan34")
        message_request = client.invoke.await_args_list[1].args[0]
        self.assertIsInstance(
            message_request,
            safe_forward.raw.functions.channels.GetMessages,
        )
        self.assertEqual(message_request.channel.channel_id, 4374922177)
        self.assertEqual(message_request.channel.access_hash, 456)
        self.assertEqual(message_request.id[0].id, 15636)

    def test_wrapper_timeout_uses_raw_api_fallback(self):
        raw_message = SimpleNamespace(empty=False, media=object())
        client = SimpleNamespace(
            get_messages=AsyncMock(side_effect=asyncio.TimeoutError)
        )

        async def scenario():
            with patch.object(
                safe_forward,
                "_get_message_via_raw_api",
                new=AsyncMock(return_value=raw_message),
            ) as raw_fetch:
                message = await safe_forward._get_message(client, 12345, 15635)
                return message, raw_fetch.await_count

        message, raw_count = asyncio.run(scenario())
        self.assertIs(message, raw_message)
        self.assertEqual(raw_count, 1)

    def test_raw_message_fallback_has_a_total_timeout(self):
        original_timeout = safe_forward._RAW_MESSAGE_FETCH_TIMEOUT
        client = SimpleNamespace(
            get_messages=AsyncMock(return_value=SimpleNamespace(empty=True))
        )

        async def scenario():
            safe_forward._RAW_MESSAGE_FETCH_TIMEOUT = 0.005

            async def stalled_raw_fetch(*_args, **_kwargs):
                await asyncio.Event().wait()

            try:
                with patch.object(
                    safe_forward,
                    "_get_message_via_raw_api",
                    new=AsyncMock(side_effect=stalled_raw_fetch),
                ):
                    with self.assertRaises(asyncio.TimeoutError):
                        await safe_forward._get_message(client, 12345, 15635)
            finally:
                safe_forward._RAW_MESSAGE_FETCH_TIMEOUT = original_timeout

        asyncio.run(scenario())

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
        self.assertEqual(result, (True, None, True))
        self.assertEqual(fetch_count, 0)

    def test_public_inspection_error_uses_bounded_transfer_fallback(self):
        async def scenario(single_only):
            with (
                patch.object(
                    safe_forward,
                    "_resolve_source",
                    new=AsyncMock(return_value=(12345, None)),
                ),
                patch.object(
                    safe_forward,
                    "_get_message",
                    new=AsyncMock(side_effect=RuntimeError("protected media metadata")),
                ),
            ):
                return await safe_forward.inspect_message_media_size(
                    object(), "@channel", 14274, single_only=single_only
                )

        for single_only in (True, False):
            with self.subTest(single_only=single_only):
                result = asyncio.run(scenario(single_only))
                self.assertEqual(result, (True, None, single_only))

    def test_empty_public_message_is_retried_by_worker(self):
        async def scenario(single_only):
            with (
                patch.object(
                    safe_forward,
                    "_resolve_source",
                    new=AsyncMock(return_value=(12345, None)),
                ),
                patch.object(
                    safe_forward,
                    "_get_message",
                    new=AsyncMock(return_value=SimpleNamespace(empty=True)),
                ),
            ):
                return await safe_forward.inspect_message_media_size(
                    object(), "@publicgroup", 14274, single_only=single_only
                )

        for single_only in (True, False):
            with self.subTest(single_only=single_only):
                self.assertEqual(
                    asyncio.run(scenario(single_only)),
                    (True, None, single_only),
                )

    def test_public_source_resolution_error_uses_bounded_transfer_fallback(self):
        async def inspect(resolve_result=None, resolve_error=None, single_only=False):
            resolve = AsyncMock(
                return_value=resolve_result,
                side_effect=resolve_error,
            )
            with (
                patch.object(safe_forward, "_resolve_source", new=resolve),
                patch.object(
                    safe_forward,
                    "_get_message",
                    new=AsyncMock(side_effect=RuntimeError("must not fetch")),
                ) as fetch_message,
            ):
                result = await safe_forward.inspect_message_media_size(
                    object(), "@publicgroup", 14274, single_only=single_only
                )
                return result, fetch_message.await_count

        cases = (
            {
                "resolve_error": RuntimeError("temporary resolve failure"),
                "single_only": False,
            },
            {
                "resolve_result": (None, "Gagal mengakses channel: temporary RPC failure"),
                "single_only": True,
            },
        )
        for case in cases:
            with self.subTest(case=case):
                result, fetch_count = asyncio.run(inspect(**case))
                self.assertEqual(
                    result, (True, None, case["single_only"])
                )
                self.assertEqual(fetch_count, 0)

    def test_public_source_resolution_timeout_does_not_use_fallback(self):
        async def scenario():
            with patch.object(
                safe_forward,
                "_resolve_source",
                new=AsyncMock(
                    return_value=(None, "Tidak bisa mengakses channel (timeout).")
                ),
            ):
                return await safe_forward.inspect_message_media_size(
                    object(), "@publicgroup", 14274, single_only=False
                )

        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(scenario())

    def test_preflight_error_does_not_fallback_for_private_links(self):
        async def inspect():
            with (
                patch.object(
                    safe_forward,
                    "_resolve_source",
                    new=AsyncMock(return_value=(12345, None)),
                ),
                patch.object(
                    safe_forward,
                    "_get_message",
                    new=AsyncMock(side_effect=RuntimeError("metadata unavailable")),
                ),
            ):
                return await safe_forward.inspect_message_media_size(
                    object(), -10012345, 14274, single_only=True
                )

        with self.assertRaisesRegex(RuntimeError, "metadata unavailable"):
            asyncio.run(inspect())

    def test_public_album_fetch_error_defers_to_worker_side_size_checks(self):
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
                    new=AsyncMock(return_value=(12345, None)),
                ),
                patch.object(
                    safe_forward, "_get_message", new=AsyncMock(return_value=message)
                ),
                patch.object(
                    safe_forward,
                    "_fetch_album_messages",
                    new=AsyncMock(
                        side_effect=RuntimeError("album metadata unavailable")
                    ),
                ),
            ):
                return await safe_forward.inspect_message_media_size(
                    object(), "@publicgroup", 14274, single_only=False
                )

        self.assertEqual(asyncio.run(scenario()), (True, None, False))

    def test_album_unknown_or_oversized_media_is_rejected_before_copy_or_upload(self):
        async def run_album(file_size):
            message = SimpleNamespace(
                empty=False,
                media_group_id="album-1",
                media=object(),
                video=SimpleNamespace(file_size=file_size),
                has_protected_content=False,
            )
            with (
                patch.object(
                    safe_forward,
                    "_resolve_source",
                    new=AsyncMock(return_value=(12345, None)),
                ),
                patch.object(
                    safe_forward,
                    "_fetch_album_messages",
                    new=AsyncMock(return_value=[message]),
                ),
                patch.object(
                    safe_forward,
                    "_copy_public_album",
                    new=AsyncMock(return_value=None),
                ) as copy_album,
                patch.object(
                    safe_forward,
                    "_send_album_via_bot",
                    new=AsyncMock(return_value=(True, None)),
                ) as send_via_bot,
                patch.object(
                    safe_forward,
                    "_send_album_streaming_individually",
                    new=AsyncMock(return_value=(True, None)),
                ) as stream_album,
            ):
                result = await safe_forward.SafeForward.run_album(
                    object(), object(), 456, "@publicgroup", 14274
                )
                return (
                    result,
                    copy_album.await_count,
                    send_via_bot.await_count,
                    stream_album.await_count,
                )

        cases = (
            (None, "Ukuran salah satu media album tidak tersedia"),
            (
                safe_forward.MAX_FILE_SIZE_BYTES + 1,
                "Album memiliki media terlalu besar",
            ),
        )
        for file_size, expected_reason in cases:
            with self.subTest(file_size=file_size):
                (ok, reason), copy_count, bot_count, stream_count = asyncio.run(
                    run_album(file_size)
                )
                self.assertFalse(ok)
                self.assertIn(expected_reason, reason)
                self.assertEqual((copy_count, bot_count, stream_count), (0, 0, 0))

    def test_public_single_preflight_timeout_is_not_queued_as_unknown_size(self):
        async def scenario():
            with (
                patch.object(
                    safe_forward,
                    "_resolve_source",
                    new=AsyncMock(return_value=(12345, None)),
                ),
                patch.object(
                    safe_forward,
                    "_get_message",
                    new=AsyncMock(side_effect=asyncio.TimeoutError),
                ),
            ):
                return await safe_forward.inspect_message_media_size(
                    object(), "@channel", 14274, single_only=True
                )

        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(scenario())

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