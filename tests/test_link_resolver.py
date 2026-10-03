import asyncio
import json
import unittest
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
                link_resolver.resolve_rentry_url(
                    "https://link-center.net/642509/slug",
                    None,
                )
            )

    def test_resolves_and_returns_only_canonical_rentry_url(self):
        response = _FakeResponse({
            "project": "linkvertise",
            "data": {
                "url": "http://www.rentry.co/mz7v7bio?source=resolver",
                "inputUrl": "https://linkvertise.com/642509/slug",
            },
        })
        with patch("modules.link_resolver.urlopen", return_value=response) as open_url:
            result = asyncio.run(
                link_resolver.resolve_rentry_url(
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

    def test_does_not_return_non_rentry_destination(self):
        response = _FakeResponse({
            "data": {"url": "https://mega.nz/folder/example"},
        })
        with patch("modules.link_resolver.urlopen", return_value=response):
            with self.assertRaises(link_resolver.LinkResolverError):
                asyncio.run(
                    link_resolver.resolve_rentry_url(
                        "https://link-center.net/642509/slug",
                        "test-api-key",
                    )
                )

    def test_rejects_unsupported_source_before_request(self):
        with patch("modules.link_resolver.urlopen") as open_url:
            with self.assertRaises(link_resolver.LinkResolverError):
                asyncio.run(
                    link_resolver.resolve_rentry_url(
                        "https://example.org/642509/slug",
                        "test-api-key",
                    )
                )
        open_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()