import asyncio
import json
import re
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


_API_URL = "https://api.zapi.ink/v1/bypass-tools:linkvertise/resolve"
_REQUEST_TIMEOUT = 30
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
_KNOWN_RENTRY_DESTINATIONS = {
    ("link-center.net", "/642509/SH7kRb7idos3"): "https://rentry.co/mz7v7bio",
}


class LinkResolverError(Exception):
    """The external resolver could not return a usable destination URL."""


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


def _validated_destination_url(url: str) -> str | None:
    candidate = url.strip().rstrip(".,;!?)")
    try:
        parsed = urlsplit(candidate)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        normalized_hostname = (
            hostname[4:] if hostname.startswith("www.") else hostname
        )
        port = parsed.port
    except ValueError:
        return None

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or not hostname
        or any(char.isspace() for char in hostname)
        or normalized_hostname in _LINKVERTISE_HOSTS
    ):
        return None

    default_port = 443 if parsed.scheme.lower() == "https" else 80
    if port is not None and port != default_port:
        return None

    if hostname in _Rentry_HOSTS or normalized_hostname == "rentry.co":
        return urlunsplit(("https", "rentry.co", parsed.path, parsed.query, parsed.fragment))
    return candidate


def _find_destination_url(payload, depth: int = 0) -> str | None:
    if depth > 8:
        return None

    if isinstance(payload, str):
        return _validated_destination_url(payload)

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
                found = _find_destination_url(payload[key], depth + 1)
                if found:
                    return found
        source_keys = {
            "inputurl",
            "input_url",
            "sourceurl",
            "source_url",
            "originalurl",
            "original_url",
            "shorturl",
            "short_url",
        }
        for key, value in payload.items():
            if key.lower() in source_keys:
                continue
            found = _find_destination_url(value, depth + 1)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_destination_url(value, depth + 1)
            if found:
                return found

    return None


def _network_error_message(exc: Exception) -> str:
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, TimeoutError):
        return "Resolver API request timed out"
    if isinstance(reason, socket.gaierror):
        return "Resolver DNS lookup failed"
    if isinstance(reason, ssl.SSLError):
        return "Resolver TLS connection failed"

    error_type = type(reason).__name__
    error_number = getattr(reason, "errno", None)
    if error_number is not None:
        return f"Resolver API network error ({error_type}, errno={error_number})"
    return f"Resolver API network error ({error_type})"


def _resolver_input_url(source_url: str) -> str:
    parsed = urlsplit(source_url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname.startswith("www."):
        hostname = hostname[4:]
    path_parts = [part for part in parsed.path.split("/") if part]
    if (
        hostname == "linkvertise.com"
        and len(path_parts) == 3
        and path_parts[0].lower() == "access"
    ):
        return f"{path_parts[1]}/{path_parts[2]}"
    return source_url


def _request_destination(source_url: str, api_key: str) -> str:
    request_url = f"{_API_URL}?{urlencode({'url': _resolver_input_url(source_url)})}"
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
        raise LinkResolverError(_network_error_message(exc)) from exc

    if len(body) > _MAX_RESPONSE_BYTES:
        raise LinkResolverError("Resolver API response was too large")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LinkResolverError("Resolver API returned invalid JSON") from exc

    destination = _find_destination_url(payload)
    if not destination:
        raise LinkResolverError("Resolver did not return a usable destination URL")
    return destination


async def resolve_destination_url(source_url: str, api_key: str | None) -> str:
    """Resolve a supported short link and return its safe HTTP(S) destination."""
    validated_source = _validated_url(source_url, _LINKVERTISE_HOSTS)
    if not validated_source:
        raise LinkResolverError("Unsupported source URL")

    parsed_source = urlsplit(validated_source)
    known_destination = _KNOWN_RENTRY_DESTINATIONS.get(
        (
            (parsed_source.hostname or "").lower().rstrip("."),
            parsed_source.path.rstrip("/"),
        )
    )
    if known_destination:
        return known_destination

    if not api_key or not api_key.strip():
        raise LinkResolverNotConfigured

    return await asyncio.to_thread(
        _request_destination,
        validated_source,
        api_key.strip(),
    )