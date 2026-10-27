import pytest

from scrappy.titlematch import normalize, title_score


def test_normalize():
    assert normalize("Tidepool Tactics: Chapter 5!") == "tidepool tactics chapter 5"
    assert normalize("Bramble’s Bandits 3") == "brambles bandits 3"
    assert normalize("Café Quokka & Friends") == "cafe quokka and friends"
    assert normalize("The Quokka® 3: Wild Tide") == "the quokka 3 wild tide"


def test_normalize_keeps_non_latin_titles():
    assert normalize("ゼルダの伝説") == "ゼルダの伝説"
    assert title_score("ゼルダの伝説", "ゼルダの伝説") == 1.0


def test_title_score_prefers_the_game_over_soundtrack_demo_dlc():
    q = "Tidepool Tactics Chapter 5"
    game = title_score(q, "Tidepool Tactics - Chapter 5")
    assert game == 1.0
    for other in ("Tidepool Tactics - Chapter 5 Soundtrack", "Tidepool Tactics Chapter 5 Demo", "Tidepool Tactics - Chapter 5 DLC Pack",
                  "Tidepool Tactics - Chapter 4", "Tidepool Tactics", "Portal 2"):
        assert title_score(q, other) < game
    assert 0.0 <= title_score(q, "Zzz") < 0.5


def test_title_score_orders_sensibly():
    q = "Tidepool Tactics Chapter 5"
    exact = title_score(q, "Tidepool Tactics - Chapter 5")
    other_chapter = title_score(q, "Tidepool Tactics - Chapter 4")
    soundtrack = title_score(q, "Tidepool Tactics - Chapter 5 Soundtrack")
    unrelated = title_score(q, "Lantern Lagoon")
    assert exact > soundtrack > unrelated
    assert exact > other_chapter > unrelated


def test_title_score_ignores_case_punctuation_and_leading_the_and_asks_for_soundtracks_when_wanted():
    assert title_score("the quokka 3: wild tide", "The Quokka® 3: Wild Tide") == 1.0
    assert title_score("Portal 2 Soundtrack", "Portal 2 Soundtrack") == 1.0
    assert title_score("Portal 2 Soundtrack", "Portal 2 Soundtrack") > title_score("Portal 2 Soundtrack", "Portal 2")


@pytest.mark.parametrize("a, b", [("", "x"), ("x", ""), (None, "x"), ("x", None), (1, 2), ("!!!", "???")])
def test_title_score_junk_is_zero(a, b):
    assert title_score(a, b) == 0.0
