import pytest

from lgtv_mcp.errors import Ambiguous, NotFound
from lgtv_mcp.matching import clean_text, pick

APPS = {
    "netflix": "Netflix",
    "youtube.leanback.v4": "YouTube",
    "amazon": "Prime Video",
    "com.playworks.app.tetris": "Tetris",
    "com.playworks.app.pacman": "Pac-Man",
    "googleplaymovieswebos": "Google Play Movies & TV",
    "com.disney.disneyplus-prod": "Disney+",
}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("netflix", "netflix"),
        ("NETFLIX", "netflix"),
        ("youtube", "youtube.leanback.v4"),
        ("youtube.leanback.v4", "youtube.leanback.v4"),
        ("prime", "amazon"),
        ("prime video", "amazon"),
        ("pac man", "com.playworks.app.pacman"),
        ("disney", "com.disney.disneyplus-prod"),
    ],
)
def test_pick_matches(query: str, expected: str) -> None:
    assert pick(query, APPS, "app") == expected


def test_exact_label_beats_substring() -> None:
    items = {"a": "HDMI 1", "b": "HDMI 10"}
    assert pick("hdmi 1", items, "input") == "a"


def test_ambiguous_lists_candidates() -> None:
    with pytest.raises(Ambiguous, match="Google Play Movies & TV") as err:
        pick("play", APPS, "app")
    assert "Tetris" in str(err.value)


def test_not_found() -> None:
    with pytest.raises(NotFound, match="No app matches 'zzz'"):
        pick("zzz", APPS, "app")


def test_empty_query_is_not_found() -> None:
    with pytest.raises(NotFound):
        pick("  ", APPS, "app")


def test_accents_are_folded() -> None:
    assert pick("musica", {"a": "Música", "b": "Fotos y vídeos"}, "app") == "a"
    assert pick("videos", {"a": "Música", "b": "Fotos y vídeos"}, "app") == "b"


def test_names_in_other_scripts_match() -> None:
    apps = {"coupang": "쿠팡플레이", "kinopoisk": "Кинопоиск", "amediateka": "Amediateka HD"}
    assert pick("쿠팡플레이", apps, "app") == "coupang"
    assert pick("кинопоиск", apps, "app") == "kinopoisk"
    with pytest.raises(NotFound):  # used to shrink to 'hd' and open Amediateka
        pick("Кинопоиск HD", apps, "app")


def test_ambiguous_caps_candidate_list() -> None:
    items = {f"id{i}": f"App {i}" for i in range(30)}
    with pytest.raises(Ambiguous) as err:
        pick("app", items, "app")
    assert "and 20 more" in str(err.value)


def test_clean_text_strips_control_characters() -> None:
    assert clean_text("Evil\x1b[31m\nTitle\u202e") == "Evil[31m Title"


def test_clean_text_truncates() -> None:
    assert len(clean_text("a" * 500)) == 200


def test_clean_text_handles_non_strings() -> None:
    assert clean_text(None) == ""
    assert clean_text(42) == "42"
