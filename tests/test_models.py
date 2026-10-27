from scrappy.models import AssetCandidate, GameInfo, Match


def test_assets_for_slot():
    info = GameInfo(name="X", assets=[
        AssetCandidate(slot="logo", url="u1"),
        AssetCandidate(slot="image", url="u2"),
        AssetCandidate(slot="logo", url="u3"),
    ])
    assert [a.url for a in info.assets_for("logo")] == ["u1", "u3"]
    assert info.assets_for("video") == []


def test_defaults():
    m = Match(title="T", source="Steam", source_id="1")
    assert m.score == 0.0
    a = AssetCandidate(slot="video", url="v")
    assert a.kind == "file" and a.transparent is False and a.width == 0
