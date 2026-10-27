"""What a game has now: ``Controller.load_current``, the text-change filter and the guards that keep a Current candidate
from ever being processed, downloaded or encoded."""
import io
import os
from pathlib import Path

import pytest
from PIL import Image

from scrappy.config import load_config
from scrappy.controller import Controller, ControllerError, changed_fields
from scrappy.models import AssetCandidate
from scrappy.processing.images import make_boxart_png, make_logo_png
from scrappy.saver import SaveError
from scrappy.ui import pipeline

ES = '<?xml version="1.0"?><systemList><system><name>switch</name><extension>.nsp</extension></system></systemList>'


def _big_jpg():
    buf = io.BytesIO()
    Image.new("RGB", (1280, 720), "red").save(buf, "JPEG")
    return buf.getvalue()


JPG = _big_jpg()                                              # larger than the theme size, to tell file from preview
BOX = make_boxart_png(Image.new("RGB", (100, 160), "blue"))[0]
LOGO = make_logo_png(Image.new("RGBA", (60, 20), (255, 255, 255, 255)))
VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2000


class NoYt:
    path = "/nowhere"

    def available(self):
        return False


def game(path, **tags):
    inner = "".join(f"<{k}>{v}</{k}>" for k, v in tags.items())
    return f"<game><path>{path}</path>{inner}</game>"


@pytest.fixture
def lib(tmp_path):
    sw = tmp_path / "roms" / "switch"
    (sw / "images").mkdir(parents=True)
    (sw / "videos").mkdir()
    for name in ("Full Game", "Partial Game", "Empty Game", "Canonical Only", "Odd Names", "Stale Tag", "Corrupt"):
        (sw / f"{name}.nsp").write_text("x")
    img = sw / "images"
    (img / "Full Game-image.jpg").write_bytes(JPG)
    (img / "Full Game-thumb.png").write_bytes(BOX)
    (img / "Full Game-marquee.png").write_bytes(LOGO)
    (sw / "videos" / "Full Game-video.mp4").write_bytes(VIDEO)
    (img / "Partial Game-image.jpg").write_bytes(JPG)
    (img / "Canonical Only-thumb.png").write_bytes(BOX)
    (img / "my own art.png").write_bytes(BOX)                    # a non-standard name that the tag points to
    (img / "Odd Names-thumb.png").write_bytes(BOX)               # and the standard file exists too
    (img / "Stale Tag-thumb.png").write_bytes(BOX)
    (img / "Corrupt-image.jpg").write_bytes(b"old")
    (sw / "gamelist.xml").write_text(
        '<?xml version="1.0"?><gameList>'
        + game("./Full Game.nsp", name="Full Game", desc="A full description.", releasedate="20200102T000000",
               developer="Dev", publisher="Pub", genre="Action", players="2", rating="0.9",
               image="./images/Full Game-image.jpg", thumbnail="./images/Full Game-thumb.png",
               marquee="./images/Full Game-marquee.png", video="./videos/Full Game-video.mp4")
        + game("./Partial Game.nsp", name="Partial Game", desc="  Some text.  ", image="./images/Partial Game-image.jpg")
        + game("./Odd Names.nsp", name="Odd", thumbnail="./images/my own art.png")
        + game("./Stale Tag.nsp", name="Stale", thumbnail="./images/gone.png")
        + game("./Corrupt.nsp", name="Corrupt", image="./images/Corrupt-image.jpg")
        + "</gameList>")
    es = tmp_path / "es.cfg"
    es.write_text(ES)
    cfg = load_config(overrides={"roms_root": str(tmp_path / "roms"), "data_dir": str(tmp_path / "data")})
    return cfg, str(es), sw


def make(lib, probe=lambda p: 83.4):
    cfg, es, _ = lib
    return Controller(cfg, providers=[], ytdlp=NoYt(), es_configs=[es], probe=probe)


def load(lib, rom, **kw):
    return make(lib, **kw).load_current("switch", f"./{rom}.nsp")


# ---------------------------------------------------------------- reading what the game has

def test_a_full_game_has_every_slot_and_its_text(lib):
    cur = load(lib, "Full Game")
    assert set(cur.pictures) == {"logo", "boxart", "image"} and all(cur.pictures.values())
    image = cur.pictures["image"]
    assert image.candidate.current and image.candidate.slot == "image" and image.candidate.url == "current://image"
    assert (image.candidate.width, image.candidate.height) == (1280, 720)       # the real file, not the preview
    assert image.size_bytes == len(JPG) and image.path.endswith("Full Game-image.jpg") and image.standard_exists
    assert (cur.pictures["boxart"].candidate.width, cur.pictures["boxart"].candidate.height) == (396, 640)
    assert (cur.pictures["logo"].candidate.width, cur.pictures["logo"].candidate.height) == (600, 300)
    assert cur.video.candidate.current and cur.video.candidate.slot == "video" and cur.video.size_bytes == len(VIDEO)
    assert cur.video.duration == 83.4 and cur.video.candidate.duration == 83.4
    assert cur.fields == {"name": "Full Game", "desc": "A full description.", "releasedate": "20200102T000000",
                          "developer": "Dev", "publisher": "Pub", "genre": "Action", "players": "2"}


def test_previews_are_small_pngs_of_the_real_pictures(lib):
    cur = load(lib, "Full Game")
    for asset in cur.pictures.values():
        im = Image.open(io.BytesIO(asset.preview))
        assert im.format == "PNG" and max(im.size) <= 640
    big = Image.open(io.BytesIO(cur.pictures["image"].preview))
    assert big.size == (640, 360)                                               # shrunk from 1280x720, same shape


def test_a_partial_game_shows_what_it_has_and_nothing_for_the_rest(lib):
    cur = load(lib, "Partial Game")
    assert cur.pictures["image"] and cur.pictures["logo"] is None and cur.pictures["boxart"] is None and cur.video is None
    assert cur.fields["name"] == "Partial Game" and cur.fields["desc"] == "Some text."     # stripped
    assert cur.fields["developer"] == "" and cur.fields["releasedate"] == ""


def test_a_game_that_is_not_in_the_gamelist_is_empty(lib):
    cur = load(lib, "Empty Game")
    assert cur.pictures == {"logo": None, "boxart": None, "image": None} and cur.video is None
    assert set(cur.fields.values()) == {""}


def test_standard_names_count_even_when_the_gamelist_does_not_mention_them(lib):
    cur = load(lib, "Canonical Only")
    assert cur.pictures["boxart"].path.endswith("Canonical Only-thumb.png") and cur.pictures["boxart"].standard_exists


def test_the_file_a_tag_points_to_wins_over_the_standard_name(lib):
    asset = load(lib, "Odd Names").pictures["boxart"]
    assert asset.path.endswith("my own art.png") and asset.standard_path.endswith("Odd Names-thumb.png")
    assert asset.standard_exists                                                  # saving would overwrite this one too


def test_a_tag_to_a_missing_file_falls_back_to_the_standard_name(lib):
    asset = load(lib, "Stale Tag").pictures["boxart"]
    assert asset.path.endswith("Stale Tag-thumb.png")


def test_a_tag_to_a_file_with_another_name_that_is_the_only_one(lib):
    _, _, sw = lib
    (sw / "images" / "Canonical Only-thumb.png").unlink()
    (sw / "gamelist.xml").write_text('<gameList>' + game("./Canonical Only.nsp", thumbnail="./images/my own art.png") + "</gameList>")
    asset = load(lib, "Canonical Only").pictures["boxart"]
    assert asset.path.endswith("my own art.png") and not asset.standard_exists


def test_an_unreadable_picture_still_counts_as_existing(lib):
    asset = load(lib, "Corrupt").pictures["image"]
    assert asset is not None and asset.preview == b"" and asset.size_bytes == 3
    assert asset.problem and asset.candidate.current and asset.standard_exists
    assert (asset.candidate.width, asset.candidate.height) == (0, 0)


def test_an_empty_file_is_not_a_current_asset(lib):
    _, _, sw = lib
    (sw / "images" / "Partial Game-image.jpg").write_bytes(b"")
    assert load(lib, "Partial Game").pictures["image"] is None


def test_the_video_is_never_read_only_measured(lib):
    seen = []
    cur = load(lib, "Full Game", probe=lambda p: seen.append(p) or 12.0)
    assert seen == [cur.video.path] and cur.video.path.endswith("Full Game-video.mp4") and cur.video.preview == b""


@pytest.mark.parametrize("probe", [lambda p: 0.0, lambda p: (_ for _ in ()).throw(OSError("no ffprobe")),
                                   lambda p: (_ for _ in ()).throw(RuntimeError("weird"))])
def test_a_probe_that_fails_leaves_the_length_unknown(lib, probe):
    cur = load(lib, "Full Game", probe=probe)
    assert cur.video.duration == 0.0 and cur.video.size_bytes == len(VIDEO)


def test_a_damaged_gamelist_is_a_plain_error(lib):
    _, _, sw = lib
    (sw / "gamelist.xml").write_text("<gameList><game>")
    with pytest.raises(ControllerError, match="gamelist.xml"):
        load(lib, "Full Game")


def test_a_missing_gamelist_means_a_game_with_nothing(lib):
    _, _, sw = lib
    (sw / "gamelist.xml").unlink()
    cur = load(lib, "Canonical Only")
    assert cur.pictures["boxart"] and set(cur.fields.values()) == {""}


def test_cancelling_stops_the_load(lib):
    with pytest.raises(InterruptedError):
        make(lib).load_current("switch", "./Full Game.nsp", cancelled=lambda: True)


def test_a_bad_system_name_is_refused(lib):
    with pytest.raises(ControllerError):
        make(lib).load_current("../etc", "./x.nsp")


def test_loading_current_writes_nothing(lib):
    _, _, sw = lib

    def snapshot():
        return sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in sw.rglob("*"))

    before = snapshot()
    load(lib, "Full Game")
    load(lib, "Empty Game")
    assert snapshot() == before


def test_a_non_regular_file_behind_a_tag_is_not_opened(lib):
    _, _, sw = lib
    fifo = sw / "images" / "pipe.png"
    os.mkfifo(fifo)
    (sw / "gamelist.xml").write_text('<gameList>' + game("./Empty Game.nsp", thumbnail="./images/pipe.png") + "</gameList>")
    asset = load(lib, "Empty Game").pictures["boxart"]
    assert asset is None                                                          # not a file with content: no hang, no asset


# ---------------------------------------------------------------- Current can never be processed

def current_candidate(lib, slot="image"):
    cur = load(lib, "Full Game")
    return (cur.video if slot == "video" else cur.pictures[slot]).candidate


def test_a_current_candidate_cannot_be_encoded(lib):
    ctl = make(lib)
    with pytest.raises(ControllerError, match="already|Current"):
        ctl.make_video(current_candidate(lib, "video"), 0, 10)
    forged = AssetCandidate(slot="video", url="https://example.com/a.mp4", kind="hls", current=True)
    with pytest.raises(ControllerError):
        ctl.make_video(forged, 0, 10)                                              # the flag alone is enough


def test_a_current_address_cannot_be_fetched(lib):
    ctl = make(lib)
    for local_ok in (False, True):
        with pytest.raises(ControllerError):
            ctl.fetch_bytes("current://image", local_ok=local_ok)


def test_a_current_candidate_cannot_go_through_the_picture_pipeline(lib):
    cand = current_candidate(lib)
    called = []
    with pytest.raises(ValueError, match="Current"):
        pipeline.load_candidate(lambda *a, **k: called.append(a) or JPG, cand, "image")
    assert not called


# ---------------------------------------------------------------- only changed text is written

def test_changed_fields_keeps_only_real_changes():
    current = {"name": "Cinder Citadel", "desc": "Old", "genre": ""}
    new = {"name": " Cinder Citadel ", "desc": "New", "genre": "Roguelike", "players": "1", "developer": "  "}
    assert changed_fields(new, current) == {"desc": "New", "genre": "Roguelike", "players": "1"}


def test_build_request_with_current_drops_unchanged_fields(lib):
    ctl = make(lib)
    current = {"name": "Cinder Citadel", "desc": "Old"}
    req = ctl.build_request("switch", "./Cinder Citadel.nsp", {"name": "Cinder Citadel", "desc": "New", "genre": "x"}, current=current)
    assert req.fields == {"desc": "New", "genre": "x"}
    assert ctl.build_request("switch", "./Cinder Citadel.nsp", {"name": "Cinder Citadel"}).fields == {"name": "Cinder Citadel"}   # without current: as before


def test_build_request_still_validates_with_current(lib):
    ctl = make(lib)
    with pytest.raises(SaveError):
        ctl.build_request("switch", "./Cinder Citadel.nsp", {"rating": "1"}, current={"rating": "1"})
    with pytest.raises(SaveError):
        ctl.build_request("switch", "./Cinder Citadel.nsp", {"name": 5}, current={})


# ---------------------------------------------------------------- files the gamelist does not point at

def test_tag_linked_tells_whether_the_gamelist_points_at_the_file(lib):
    assert load(lib, "Full Game").pictures["image"].tag_linked and load(lib, "Full Game").video.tag_linked
    assert load(lib, "Odd Names").pictures["boxart"].tag_linked                       # another name, but linked
    assert not load(lib, "Canonical Only").pictures["boxart"].tag_linked              # found by its standard name only
    assert not load(lib, "Stale Tag").pictures["boxart"].tag_linked                   # the tag points at a missing file


def test_a_video_found_by_its_standard_name_is_not_linked(lib):
    _, _, sw = lib
    (sw / "videos" / "Empty Game-video.mp4").write_bytes(VIDEO)
    assert load(lib, "Empty Game").video.tag_linked is False


# ---------------------------------------------------------------- reading stays inside the system folder

def test_tags_that_lead_outside_the_system_folder_are_not_read(lib, tmp_path):
    _, _, sw = lib
    (tmp_path / "outside.png").write_bytes(BOX)
    (sw / "images" / "link.png").symlink_to(tmp_path / "outside.png")
    for value in ("../../outside.png", str(tmp_path / "outside.png"), "./images/../../../outside.png", "./images/link.png"):
        (sw / "gamelist.xml").write_text("<gameList>" + game("./Empty Game.nsp", thumbnail=value) + "</gameList>")
        assert load(lib, "Empty Game").pictures["boxart"] is None, value


def test_a_tag_inside_the_folder_with_dots_is_still_read(lib):
    _, _, sw = lib
    (sw / "gamelist.xml").write_text("<gameList>" + game("./Empty Game.nsp", thumbnail="./images/../images/my own art.png") + "</gameList>")
    assert load(lib, "Empty Game").pictures["boxart"].path.endswith("my own art.png")


# ---------------------------------------------------------------- linking an existing file (no rewrite)

def saved(lib, rom, **kw):
    ctl = make(lib)
    req = ctl.build_request("switch", f"./{rom}.nsp", {}, **kw)
    return ctl.save(req)


def test_link_sets_the_tag_without_touching_the_file(lib):
    _, _, sw = lib
    before = (sw / "images" / "Canonical Only-thumb.png").read_bytes()
    stat_before = (sw / "images" / "Canonical Only-thumb.png").stat().st_mtime_ns
    report = saved(lib, "Canonical Only", link=("thumbnail",))
    assert report.linked == ["thumbnail"] and report.written == [] and report.replaced == []
    assert (sw / "images" / "Canonical Only-thumb.png").read_bytes() == before
    assert (sw / "images" / "Canonical Only-thumb.png").stat().st_mtime_ns == stat_before
    assert "<thumbnail>./images/Canonical Only-thumb.png</thumbnail>" in (sw / "gamelist.xml").read_text()
    row = next(r for r in make(lib).rows("switch") if r.rom_path == "./Canonical Only.nsp")
    assert "thumbnail" not in row.missing


def test_link_repairs_a_dangling_tag_and_a_video(lib):
    _, _, sw = lib
    (sw / "images" / "Stale Tag-marquee.png").write_bytes(LOGO)
    (sw / "videos" / "Stale Tag-video.mp4").write_bytes(VIDEO)
    report = saved(lib, "Stale Tag", link=("thumbnail", "video", "marquee"))
    assert sorted(report.linked) == ["marquee", "thumbnail", "video"] and report.written == []
    text = (sw / "gamelist.xml").read_text()
    assert "<thumbnail>./images/Stale Tag-thumb.png</thumbnail>" in text and "./videos/Stale Tag-video.mp4" in text


def test_linked_files_can_be_undone(lib):
    _, _, sw = lib
    ctl = make(lib)
    ctl.save(ctl.build_request("switch", "./Canonical Only.nsp", {}, link=("thumbnail",)))
    ctl.undo()
    assert "Canonical Only-thumb" not in (sw / "gamelist.xml").read_text()
    assert (sw / "images" / "Canonical Only-thumb.png").exists()                       # the file was never ours to remove


def test_link_needs_a_real_standard_file(lib):
    ctl = make(lib)
    with pytest.raises(SaveError, match="no box art file|nothing to link"):
        ctl.build_request("switch", "./Empty Game.nsp", {}, link=("thumbnail",))
    with pytest.raises(SaveError):
        ctl.build_request("switch", "./Empty Game.nsp", {}, link=("rating",))


def test_a_new_picture_wins_over_a_link_for_the_same_slot(lib):
    _, _, sw = lib
    ctl = make(lib)
    req = ctl.build_request("switch", "./Canonical Only.nsp", {}, thumb_png=BOX, replace=("thumbnail",), link=("thumbnail",))
    assert req.thumb_png == BOX
