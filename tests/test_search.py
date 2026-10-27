import threading
import time

from scrappy.models import AssetCandidate, GameInfo, Match
from scrappy.providers.base import Provider, ProviderError
from scrappy.search import merge_infos, rank_assets, search_matches


class Fake(Provider):
    def __init__(self, name, matches=None, error=None, delay=0, result=None):
        self.name, self._m, self._e, self._d, self._r = name, matches or [], error, delay, result
        self.threads = []

    def search(self, title):
        self.threads.append(threading.current_thread())
        time.sleep(self._d)
        if self._e:
            raise self._e
        return self._r if self._r is not None else list(self._m)


def test_search_matches_collects_scores_and_errors():
    good = Fake("A", [Match("Tidepool Tactics - Chapter 4", "A", "1"), Match("Tidepool Tactics - Chapter 5", "A", "2")])
    bad = Fake("B", error=RuntimeError("down"))
    out = search_matches([good, bad], "Tidepool Tactics Chapter 5")
    assert [m.source_id for m in out.matches] == ["2", "1"]            # best first
    assert out.matches[0].score == 1.0
    assert out.errors["B"] == "RuntimeError: down"


def test_search_matches_keeps_provider_scores_and_shows_provider_errors_plainly():
    scored = Fake("A", [Match("Tidepool Tactics - Chapter 4", "A", "1", score=0.97), Match("Something", "A", "2", score=0.2)])
    unscored = Fake("B", [Match("Tidepool Tactics - Chapter 5", "B", "3")])
    plain = Fake("C", error=ProviderError("Steam could not be reached (check the internet connection)"))
    out = search_matches([scored, unscored, plain], "Tidepool Tactics Chapter 5")
    assert [(m.source_id, m.score) for m in out.matches] == [("3", 1.0), ("1", 0.97), ("2", 0.2)]   # not re-scored
    assert out.errors == {"C": "Steam could not be reached (check the internet connection)"}


def test_search_matches_times_out_slow_provider():
    slow = Fake("Slow", [Match("X", "Slow", "9")], delay=3.0)
    start = time.time()
    out = search_matches([slow], "X", timeout=0.2)
    assert time.time() - start < 2.5
    assert out.errors == {"Slow": "did not answer in time"}


def test_providers_run_concurrently_in_their_own_threads():
    a = Fake("A", [Match("X", "A", "1")], delay=1.0)
    b = Fake("B", [Match("X", "B", "2")], delay=1.0)
    start = time.time()
    out = search_matches([a, b], "X")
    assert time.time() - start < 1.8              # two 1 s searches one after the other would need 2 s
    assert len(out.matches) == 2
    assert a.threads[0] is not threading.current_thread() and a.threads[0] is not b.threads[0]


def test_slow_provider_does_not_block_or_discard_the_others():
    fast = Fake("Fast", [Match("X", "Fast", "1")])
    slow = Fake("Slow", delay=2.0)
    broken = Fake("Broken", error=ProviderError("no data"))
    start = time.time()
    out = search_matches([slow, fast, broken], "X", timeout=0.3)
    assert time.time() - start < 1.8
    assert [m.source_id for m in out.matches] == ["1"]
    assert out.errors == {"Slow": "did not answer in time", "Broken": "no data"}


def test_timeout_is_one_shared_deadline_not_per_provider():
    providers = [Fake(f"S{i}", delay=2.0) for i in range(4)]
    start = time.time()
    out = search_matches(providers, "X", timeout=0.3)
    assert time.time() - start < 1.8              # four sources at 2 s each in sequence would need 8 s
    assert len(out.errors) == 4


def test_statuses_report_every_provider_in_order():
    out = search_matches([Fake("A", [Match("X", "A", "1")]), Fake("B"), Fake("C", error=ProviderError("offline"))], "X")
    assert [(s.name, s.ok, s.count, s.message) for s in out.statuses] == [
        ("A", True, 1, "1 match"), ("B", True, 0, "no matches"), ("C", False, 0, "offline")]


def test_provider_results_are_not_mutated_or_trusted():
    shared = Match("Tidepool Tactics Chapter 5", "A", "1")
    out = search_matches([Fake("A", [shared, "junk", None])], "Tidepool Tactics Chapter 5")
    assert shared.score == 0.0                                    # the provider's own object is left alone
    assert [m.score for m in out.matches] == [1.0]               # non-Match entries are dropped
    bad = search_matches([Fake("B", result="not a list")], "X")
    assert bad.matches == [] and "B" in bad.errors and not bad.statuses[0].ok


def test_non_finite_provider_scores_are_replaced_by_the_shared_scorer():
    out = search_matches([Fake("A", [Match("X", "A", "1", score=float("nan")), Match("X", "A", "2", score=7)])], "X")
    assert sorted((m.source_id, m.score) for m in out.matches) == [("1", 1.0), ("2", 1.0)]


def test_same_source_and_id_is_listed_once_keeping_the_best_score():
    a = Fake("A", [Match("Portal 2", "A", "620", score=0.5), Match("Portal 2", "A", "620", score=0.9), Match("Portal 2", "A", "7")])
    out = search_matches([a], "Portal 2")
    assert [(m.source_id, m.score) for m in out.matches] == [("7", 1.0), ("620", 0.9)]


def test_hits_without_an_id_are_not_collapsed_into_one():
    a = Fake("A", [Match("Portal", "A", ""), Match("Portal 2", "A", ""), Match("Portal", "A", "")])
    out = search_matches([a], "Portal")
    assert sorted(m.title for m in out.matches) == ["Portal", "Portal 2"]       # same title and no id = the same hit


def test_equal_scores_keep_provider_order():
    out = search_matches([Fake("A", [Match("X", "A", "1")]), Fake("B", [Match("X", "B", "2")])], "X")
    assert [m.source_id for m in out.matches] == ["1", "2"]


def test_cancel_stops_waiting():
    slow = Fake("Slow", delay=2.0)
    start = time.time()
    out = search_matches([slow], "X", timeout=10, cancelled=lambda: time.time() - start > 0.2)
    assert time.time() - start < 1.8
    assert out.errors == {"Slow": "search cancelled"}


def test_blank_title_or_no_providers_gives_nothing():
    p = Fake("A", [Match("X", "A", "1")])
    assert search_matches([p], "   ").matches == [] and p.threads == []
    assert search_matches([], "X").matches == []


def test_merge_infos_prefers_first_non_empty_and_concats_assets():
    a = GameInfo(name="A", description="", developer="Dev A", assets=[AssetCandidate(slot="logo", url="1")])
    b = GameInfo(name="B", description="desc B", developer="Dev B", genre="Horror", assets=[AssetCandidate(slot="logo", url="2")])
    m = merge_infos([a, b])
    assert (m.name, m.description, m.developer, m.genre) == ("A", "desc B", "Dev A", "Horror")
    assert [x.url for x in m.assets] == ["1", "2"]
    assert merge_infos([]).name == ""


def test_merge_infos_leaves_inputs_alone_and_drops_repeated_assets():
    a = GameInfo(name="A", assets=[AssetCandidate(slot="logo", url="1")])
    b = GameInfo(name="A", assets=[AssetCandidate(slot="logo", url="1"), AssetCandidate(slot="image", url="1")])
    m = merge_infos([a, None, b])
    assert [(x.slot, x.url) for x in m.assets] == [("logo", "1"), ("image", "1")]
    assert len(a.assets) == 1 and len(b.assets) == 2


def test_rank_assets():
    wide = AssetCandidate(slot="image", url="header", width=460, height=215)
    shot = AssetCandidate(slot="image", url="shot", width=1920, height=1080)
    square = AssetCandidate(slot="image", url="sq", width=500, height=500)
    assert [a.url for a in rank_assets([wide, square, shot], "image")] == ["shot", "header", "sq"]
    solid = AssetCandidate(slot="logo", url="solid", width=800, height=400)
    clear = AssetCandidate(slot="logo", url="clear", transparent=True, width=100, height=50)
    assert [a.url for a in rank_assets([solid, clear], "logo")] == ["clear", "solid"]
    land = AssetCandidate(slot="boxart", url="land", width=800, height=400)
    port = AssetCandidate(slot="boxart", url="port", width=300, height=450)
    assert [a.url for a in rank_assets([land, port], "boxart")] == ["port", "land"]


def test_rank_assets_copes_with_unknown_sizes_and_keeps_order_for_ties():
    unknown = AssetCandidate(slot="boxart", url="unknown")
    port = AssetCandidate(slot="boxart", url="port", width=300, height=450)
    other = AssetCandidate(slot="boxart", url="other")
    assert [a.url for a in rank_assets([unknown, port, other], "boxart")] == ["port", "unknown", "other"]
    assert rank_assets([], "image") == []
