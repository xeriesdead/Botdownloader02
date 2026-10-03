import asyncio
import json
import socket
import ssl
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from modules import link_resolver


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self._body


class LinkResolverTests(unittest.TestCase):
    def test_extracts_link_center_url_from_message(self):
        result = link_resolver.extract_linkvertise_url(
            "Tolong cek https://link-center.net/642509/SH7kRb7idos3."
        )
        self.assertEqual(
            result,
            "https://link-center.net/642509/SH7kRb7idos3",
        )

    def test_accepts_supported_linkvertise_family_host(self):
        self.assertEqual(
            link_resolver.extract_linkvertise_url(
                "https://linkvertise.com/642509/SH7kRb7idos3"
            ),
            "https://linkvertise.com/642509/SH7kRb7idos3",
        )

    def test_resolves_linkvertise_access_path_using_documented_bare_format(self):
        response = _FakeResponse({
            "data": {
                "url": "https://panelhenil-oss.github.io/UPD/",
                "inputUrl": "https://linkvertise.com/access/1239053/engobhM4ZGTH",
            },
        })
        with patch("modules.link_resolver.urlopen", return_value=response) as open_url:
            result = asyncio.run(
                link_resolver.resolve_destination_url(
                    "https://linkvertise.com/access/1239053/engobhM4ZGTH",
                    "test-api-key",
                )
            )

        self.assertEqual(result, "https://panelhenil-oss.github.io/UPD/")
        request = open_url.call_args.args[0]
        self.assertEqual(
            parse_qs(urlsplit(request.full_url).query)["url"],
            ["1239053/engobhM4ZGTH"],
        )

    def test_normalizes_www_linkvertise_access_host(self):
        self.assertEqual(
            link_resolver._resolver_input_url(
                "https://www.linkvertise.com/access/1239053/engobhM4ZGTH"
            ),
            "1239053/engobhM4ZGTH",
        )

    def test_rejects_lookalike_and_arbitrary_hosts(self):
        self.assertIsNone(
            link_resolver.extract_linkvertise_url(
                "https://link-center.net.example.org/642509/slug"
            )
        )
        self.assertIsNone(
            link_resolver.extract_linkvertise_url(
                "https://example.org/642509/slug"
            )
        )

    def test_requires_api_key_before_making_request(self):
        with self.assertRaises(link_resolver.LinkResolverNotConfigured):
            asyncio.run(
                link_resolver.resolve_destination_url(
                    "https://link-center.net/642509/slug",
                    None,
                )
            )

    def test_resolves_known_link_center_url_without_api_key(self):
        with patch("modules.link_resolver.urlopen") as open_url:
            result = asyncio.run(
                link_resolver.resolve_destination_url(
                    "https://link-center.net/642509/SH7kRb7idos3",
                    None,
                )
            )

        self.assertEqual(result, "https://rentry.co/mz7v7bio")
        open_url.assert_not_called()

    def test_canonicalizes_rentry_destination_to_https(self):
        response = _FakeResponse({
            "project": "linkvertise",
            "data": {
                "url": "http://www.rentry.co/mz7v7bio?source=resolver",
                "inputUrl": "https://linkvertise.com/642509/slug",
            },
        })
        with patch("modules.link_resolver.urlopen", return_value=response) as open_url:
            result = asyncio.run(
                link_resolver.resolve_destination_url(
                    "https://link-center.net/642509/slug",
                    "test-api-key",
                )
            )

        self.assertEqual(result, "https://rentry.co/mz7v7bio?source=resolver")
        request = open_url.call_args.args[0]
        self.assertEqual(
            parse_qs(urlsplit(request.full_url).query)["url"],
            ["https://link-center.net/642509/slug"],
        )
        self.assertEqual(request.get_header("X-api-key"), "test-api-key")

    def test_does_not_return_linkvertise_input_as_destination(self):
        response = _FakeResponse({
            "data": {
                "inputUrl": "https://linkvertise.com/access/1239053/engobhM4ZGTH",
                "source_url": "https://linkvertise.com/access/1239053/engobhM4ZGTH",
            },
        })
        with patch("modules.link_resolver.urlopen", return_value=response):
            with self.assertRaisesRegex(
                link_resolver.LinkResolverError,
                "usable destination URL",
            ):
                asyncio.run(
                    link_resolver.resolve_destination_url(
                        "https://link-center.net/642509/slug",
                        "test-api-key",
                    )
                )

    def test_reports_http_status_without_exposing_request_details(self):
        for status in (401, 429):
            with self.subTest(status=status):
                error = HTTPError(
                    "https://api.zapi.ink",
                    status,
                    "Upstream error",
                    {},
                    None,
                )
                with patch(
                    "modules.link_resolver.urlopen",
                    side_effect=error,
                ):
                    with self.assertRaisesRegex(
                        link_resolver.LinkResolverError,
                        f"HTTP {status}",
                    ):
                        asyncio.run(
                            link_resolver.resolve_destination_url(
                                "https://link-center.net/642509/slug",
                                "test-api-key",
                            )
                        )

    def test_rejects_unsupported_destination_schemes(self):
        response = _FakeResponse({"data": {"url": "javascript:alert(1)"}})
        with patch("modules.link_resolver.urlopen", return_value=response):
            with self.assertRaisesRegex(
                link_resolver.LinkResolverError,
                "usable destination URL",
            ):
                asyncio.run(
                    link_resolver.resolve_destination_url(
                        "https://link-center.net/642509/slug",
                        "test-api-key",
                    )
                )

    def test_reports_safe_network_failure_details(self):
        cases = (
            (TimeoutError("socket timed out"), "request timed out"),
            (socket.gaierror(-3, "temporary DNS failure"), "DNS lookup failed"),
            (ssl.SSLError("TLS handshake failed"), "TLS connection failed"),
        )
        for reason, expected in cases:
            with self.subTest(expected=expected):
                with patch(
                    "modules.link_resolver.urlopen",
                    side_effect=URLError(reason),
                ):
                    with self.assertRaisesRegex(
                        link_resolver.LinkResolverError,
                        expected,
                    ):
                        asyncio.run(
                            link_resolver.resolve_destination_url(
                                "https://link-center.net/642509/slug",
                                "test-api-key",
                            )
                        )

    def test_retries_once_after_resolver_timeout(self):
        response = _FakeResponse({
            "url": "https://panelhenil-oss.github.io/UPD/",
        })
        with (
            patch(
                "modules.link_resolver.urlopen",
                side_effect=[
                    URLError(TimeoutError("socket timed out")),
                    response,
                ],
            ) as open_url,
            patch("modules.link_resolver.time.sleep") as sleep,
        ):
            result = asyncio.run(
                link_resolver.resolve_destination_url(
                    "https://link-target.net/642509/pastelink-r43l1lbl4ck",
                    "test-api-key",
                )
            )

        self.assertEqual(result, "https://panelhenil-oss.github.io/UPD/")
        self.assertEqual(open_url.call_count, 2)
        self.assertEqual(
            [call.kwargs["timeout"] for call in open_url.call_args_list],
            [60, 60],
        )
        sleep.assert_called_once_with(1)

    def test_rejects_unsupported_source_before_request(self):
        with patch("modules.link_resolver.urlopen") as open_url:
            with self.assertRaises(link_resolver.LinkResolverError):
                asyncio.run(
                    link_resolver.resolve_destination_url(
                        "https://example.org/642509/slug",
                        "test-api-key",
                    )
                )
        open_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()