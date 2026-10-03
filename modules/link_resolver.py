import asyncio
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


_API_URL = "https://api.zapi.ink/v1/bypass-tools:linkvertise/resolve"
_REQUEST_TIMEOUT = 15
_MAX_RESPONSE_BYTES = 1024 * 1024
_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_LINKVERTISE_HOSTS = frozenset({
    "link-center.net",
    "linkvertise.com",
    "link-to.net",
    "link-hub.net",
    "link-target.org",
    "link-target.net",
    "direct-link.net",
})
_Rentry_HOSTS = frozenset({"rentry.co", "www.rentry.co"})


class LinkResolverError(Exception):
    """The external resolver could not return a usable Rentry URL."""


class LinkResolverNotConfigured(LinkResolverError):
    """The resolver API key has not been configured."""


def _validated_url(url: str, hosts: frozenset[str]) -> str | None:
    try:
        parsed = urlsplit(url.strip())
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if hostname.startswith("www."):
            hostname = hostname[4:]
        port = parsed.port
    except ValueError:
        return None

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or hostname not in hosts
        or not parsed.path.strip("/")
    ):
        return None

    default_port = 443 if parsed.scheme.lower() == "https" else 80
    if port is not None and port != default_port:
        return None
    return url.strip()


def extract_linkvertise_url(text: str) -> str | None:
    """Return the first supported Linkvertise-family URL in a message."""
    for match in _URL_RE.finditer(text):
        candidate = match.group(0).rstrip(".,;!?)")
        source_url = _validated_url(candidate, _LINKVERTISE_HOSTS)
        if source_url:
            return source_url
    return None


def _validated_rentry_url(url: str) -> str | None:
    candidate = url.strip().rstrip(".,;!?)")
    try:
        parsed = urlsplit(candidate)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or hostname not in _Rentry_HOSTS
        or not parsed.path.strip("/")
    ):
        return None

    default_port = 443 if parsed.scheme.lower() == "https" else 80
    if port is not None and port != default_port:
        return None

    return urlunsplit(("https", "rentry.co", parsed.path, parsed.query, parsed.fragment))


def _find_rentry_url(payload, depth: int = 0) -> str | None:
    if depth > 8:
        return None

    if isinstance(payload, str):
        return _validated_rentry_url(payload)

    if isinstance(payload, dict):
        preferred_keys = (
            "url",
            "destination",
            "destinationUrl",
            "destination_url",
            "target",
            "targetUrl",
            "target_url",
        )
        for key in preferred_keys:
            if key in payload:
                found = _find_rentry_url(payload[key], depth + 1)
                if found:
                    return found
        for value in payload.values():
            found = _find_rentry_url(value, depth + 1)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_rentry_url(value, depth + 1)
            if found:
                return found

    return None


def _request_destination(source_url: str, api_key: str) -> str:
    request_url = f"{_API_URL}?{urlencode({'url': source_url})}"
    request = Request(
        request_url,
        headers={
            "Accept": "application/json",
            "User-Agent": "BotDownloader/1.0",
            "x-api-key": api_key,
        },
    )

    try:
        with urlopen(request, timeout=_REQUEST_TIMEOUT) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise LinkResolverError(
            f"Resolver API returned HTTP {exc.code}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise LinkResolverError("Resolver API request failed") from exc

    if len(body) > _MAX_RESPONSE_BYTES:
        raise LinkResolverError("Resolver API response was too large")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LinkResolverError("Resolver API returned invalid JSON") from exc

    destination = _find_rentry_url(payload)
    if not destination:
        raise LinkResolverError("Resolver did not return a Rentry URL")
    return destination


async def resolve_rentry_url(source_url: str, api_key: str | None) -> str:
    """Resolve a supported short link and return only an https://rentry.co URL."""
    if not api_key or not api_key.strip():
        raise LinkResolverNotConfigured

    validated_source = _validated_url(source_url, _LINKVERTISE_HOSTS)
    if not validated_source:
        raise LinkResolverError("Unsupported source URL")

    return await asyncio.to_thread(
        _request_destination,
        validated_source,
        api_key.strip(),
    )