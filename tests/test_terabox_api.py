import hashlib
import hmac
import json
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from modules import terabox_api


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self._body


class TeraboxApiTests(unittest.TestCase):
    def test_accepts_terabox_share_link_and_rejects_lookalikes(self):
        url = "https://1024terabox.com/s/1dDO7zyTCf7ogcs9vl2qY_Q"
        self.assertTrue(terabox_api.is_terabox_link(url))
        self.assertTrue(
            terabox_api.is_terabox_link(
                "https://www.freeterabox.com/sharing/link"
            )
        )
        self.assertFalse(
            terabox_api.is_terabox_link(
                "https://1024terabox.com.example.org/s/1dDO7zyTCf7ogcs9vl2qY_Q"
            )
        )
        self.assertFalse(
            terabox_api.is_terabox_link(
                "https://user@1024terabox.com/s/1dDO7zyTCf7ogcs9vl2qY_Q"
            )
        )

    def test_signs_request_and_returns_stream_and_download_actions(self):
        url = "https://1024terabox.com/s/1dDO7zyTCf7ogcs9vl2qY_Q"
        timestamp = 1_760_000_000
        response = _FakeResponse({
            "errno": 0,
            "title": "TeraBox Download",
            "list": [
                {
                    "server_filename": "video.mp4",
                    "size": 104857600,
                    "direct_link": "https://d.terabox.app/file?id=download-token",
                    "stream_url": "https://stream.terabox.app/stream?id=stream-token",
                },
                {
                    "server_filename": "unsafe-link.txt",
                    "direct_link": "javascript:alert(1)",
                    "stream_url": "http://stream.terabox.app/insecure",
                },
                {
                    "server_filename": "invalid-port.mp4",
                    "direct_link": "https://d.terabox.app:8443/file",
                    "stream_url": None,
                },
            ],
        })

        with (
            patch("modules.terabox_api.time.time", return_value=timestamp),
            patch("modules.terabox_api.urlopen", return_value=response) as open_url,
        ):
            title, files = terabox_api.resolve_terabox_files(
                url,
                "test-api-key",
                "test-api-secret",
            )

        request = open_url.call_args.args[0]
        body = request.data.decode("utf-8")
        expected_signature = hmac.new(
            b"test-api-secret",
            f"POST/v1/api{timestamp}{body}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        self.assertEqual(title, "TeraBox Download")
        self.assertEqual(json.loads(body), {
            "url": url,
            "dir_path": "",
            "page": 1,
        })
        self.assertEqual(request.full_url, "https://api.teraboxdl.site/v1/api")
        self.assertEqual(request.get_header("X-api-key"), "test-api-key")
        self.assertEqual(request.get_header("X-timestamp"), str(timestamp))
        self.assertEqual(request.get_header("X-signature"), expected_signature)
        self.assertEqual(open_url.call_args.kwargs["timeout"], 40)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].filename, "video.mp4")
        self.assertEqual(files[0].size_bytes, 104857600)
        self.assertEqual(
            files[0].direct_url,
            "https://d.terabox.app/file?id=download-token",
        )
        self.assertEqual(
            files[0].stream_url,
            "https://stream.terabox.app/stream?id=stream-token",
        )
        self.assertEqual(
            terabox_api.terabox_button_rows(files),
            [[
                (
                    "▶️ Streaming 1",
                    "https://stream.terabox.app/stream?id=stream-token",
                ),
                (
                    "⬇️ Download 1",
                    "https://d.terabox.app/file?id=download-token",
                ),
            ]],
        )

    def test_limits_telegram_buttons_without_changing_api_results(self):
        files = [
            terabox_api.TeraboxFile(
                filename=f"file-{index}.mp4",
                size_bytes=None,
                direct_url=f"https://d.terabox.app/{index}",
                stream_url=f"https://stream.terabox.app/{index}",
            )
            for index in range(terabox_api.MAX_TERABOX_BUTTON_ITEMS + 2)
        ]
        self.assertEqual(len(terabox_api.terabox_button_rows(files)), 20)

    def test_requires_both_credentials_before_making_request(self):
        with patch("modules.terabox_api.urlopen") as open_url:
            with self.assertRaisesRegex(
                terabox_api.TeraboxApiError,
                "TERABOX_API_KEY dan TERABOX_API_SECRET",
            ):
                terabox_api.resolve_terabox_files(
                    "https://1024terabox.com/s/share-id",
                    "test-api-key",
                    None,
                )
        open_url.assert_not_called()

    def test_reports_credential_errors_without_leaking_request_details(self):
        error = HTTPError(
            "https://api.teraboxdl.site/v1/api",
            401,
            "Unauthorized",
            {},
            None,
        )
        with patch("modules.terabox_api.urlopen", side_effect=error):
            with self.assertRaisesRegex(
                terabox_api.TeraboxApiError,
                "TERABOX_API_KEY dan TERABOX_API_SECRET",
            ):
                terabox_api.resolve_terabox_files(
                    "https://1024terabox.com/s/share-id",
                    "test-api-key",
                    "test-api-secret",
                )


if __name__ == "__main__":
    unittest.main()
