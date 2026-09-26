import pytest

from lgtv_mcp.errors import InvalidInput
from lgtv_mcp.youtube import youtube_target

VID = "dQw4w9WgXcQ"
TARGET = f"https://www.youtube.com/tv?v={VID}"


@pytest.mark.parametrize(
    "value",
    [
        VID,
        f"https://www.youtube.com/watch?v={VID}",
        f"https://youtube.com/watch?v={VID}&list=PL123&si=abc",
        f"https://m.youtube.com/watch?feature=share&v={VID}",
        f"https://music.youtube.com/watch?v={VID}",
        f"www.youtube.com/watch?v={VID}",
        f"https://youtu.be/{VID}",
        f"https://youtu.be/{VID}?si=xyz",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/live/{VID}?si=abc",
        f"https://www.youtube.com/embed/{VID}",
        f"https://www.youtube-nocookie.com/embed/{VID}",
        f"https://www.youtube.com/v/{VID}",
        f"  {VID}  ",
    ],
)
def test_accepts_known_forms(value: str) -> None:
    assert youtube_target(value) == TARGET


def test_id_starting_with_dash() -> None:
    assert youtube_target("-8DNbZkeRTs") == "https://www.youtube.com/tv?v=-8DNbZkeRTs"


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        (f"https://youtu.be/{VID}?t=42", 42),
        (f"https://youtu.be/{VID}?t=42s", 42),
        (f"https://www.youtube.com/watch?v={VID}&t=1m30s", 90),
        (f"https://www.youtube.com/watch?v={VID}&t=1h2m3s", 3723),
        (f"https://www.youtube.com/watch?v={VID}#t=15", 15),
        (f"https://www.youtube.com/watch?v={VID}&start=7", 7),
    ],
)
def test_timestamps(value: str, seconds: int) -> None:
    assert youtube_target(value) == f"{TARGET}&t={seconds}"


def test_zero_timestamp_is_dropped() -> None:
    assert youtube_target(f"https://youtu.be/{VID}?t=0") == TARGET


def test_bad_timestamp_is_ignored() -> None:
    assert youtube_target(f"https://youtu.be/{VID}?t=abc") == TARGET


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "short",
        "https://example.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
        "https://evilyoutube.com/watch?v=dQw4w9WgXcQ",
        "javascript:alert(1)",
        "https://www.youtube.com/watch?v=too_short",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ<script>",
        "https://www.youtube.com/channel/UC123",
        "ftp://youtube.com/watch?v=dQw4w9WgXcQ",
        "dQw4w9WgXc!",
        "x" * 3000,
        "https://[::1/watch?v=dQw4w9WgXcQ",
        "https://youtube.com\u2100.evil/watch?v=dQw4w9WgXcQ",
        "https://youtube.com@evil.example/watch?v=dQw4w9WgXcQ",
        "https://youtu.be.evil.example/dQw4w9WgXcQ",
    ],
)
def test_rejects_invalid(value: str) -> None:
    with pytest.raises(InvalidInput):
        youtube_target(value)
