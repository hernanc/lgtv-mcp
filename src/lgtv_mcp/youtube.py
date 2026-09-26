"""Turn YouTube URLs or video ids into deep links for the webOS YouTube app."""

import re
from urllib.parse import parse_qs, urlsplit

from .errors import InvalidInput

YOUTUBE_APP_ID = "youtube.leanback.v4"

_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_OFFSET = re.compile(r"(?:(\d{1,2})h)?(?:(\d{1,4})m)?(?:(\d{1,6})s?)?")
_HOSTS = ("youtube.com", "youtube-nocookie.com", "youtu.be")
_PATH_PREFIXES = ("shorts", "live", "embed", "v")
_MAX_LEN = 2048


def youtube_target(value: str) -> str:
    """Return a YouTube TV deep link for a video URL or bare 11-character id.

    The result is rebuilt from the parsed id and an integer offset, so nothing
    from the input is echoed through to the TV.
    """
    value = value.strip()
    if not value or len(value) > _MAX_LEN:
        raise InvalidInput("Expected a YouTube video URL or 11-character video id.")

    if _ID.fullmatch(value):
        return _build(value, 0)

    video, offset = _parse_url(value)
    return _build(video, offset)


def _parse_url(value: str) -> tuple[str, int]:
    try:
        url = urlsplit(value if "://" in value else f"https://{value}")
        host = (url.hostname or "").lower()
    except ValueError as err:
        raise InvalidInput(f"Not a valid URL: {_preview(value)}") from err
    if url.scheme not in ("http", "https") or not _is_youtube_host(host):
        raise InvalidInput(f"Not a YouTube URL: {_preview(value)}")

    query = parse_qs(url.query)
    segments = [s for s in url.path.split("/") if s]
    if host == "youtu.be":
        candidate = segments[0] if segments else ""
    elif segments[:1] == ["watch"]:
        candidate = query.get("v", [""])[0]
    elif len(segments) >= 2 and segments[0] in _PATH_PREFIXES:
        candidate = segments[1]
    else:
        candidate = ""

    if not _ID.fullmatch(candidate):
        raise InvalidInput(f"No video id found in URL: {_preview(value)}")

    fragment = parse_qs(url.fragment)
    raw_offset = (query.get("t") or query.get("start") or fragment.get("t") or [""])[0]
    return candidate, _parse_offset(raw_offset)


def _is_youtube_host(host: str) -> bool:
    return any(host == h or host.endswith(f".{h}") for h in _HOSTS)


def _parse_offset(raw: str) -> int:
    match = _OFFSET.fullmatch(raw)
    if not raw or not match:
        return 0
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _build(video: str, offset: int) -> str:
    target = f"https://www.youtube.com/tv?v={video}"
    return f"{target}&t={offset}" if offset > 0 else target


def _preview(value: str) -> str:
    return value if len(value) <= 80 else f"{value[:77]}..."
