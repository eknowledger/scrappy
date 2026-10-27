import os

import pytest

from scrappy.gamelist import Gamelist
from scrappy.roms import asset_status, rom_extensions, scan_roms

ES_CFG = """<?xml version="1.0"?>
<systemList>
  <system><name>switch</name><extension>.nsp .NSP .xci</extension></system>
  <system><name>windows</name><extension>.wine .pc</extension></system>
</systemList>
"""


def test_rom_extensions(tmp_path):
    cfg = tmp_path / "es_systems.cfg"
    cfg.write_text(ES_CFG)
    assert rom_extensions("switch", [str(cfg)]) == {".nsp", ".xci"}
    assert rom_extensions("windows", [str(cfg)]) == {".wine", ".pc"}
    assert rom_extensions("nope", [str(cfg)]) == set()


def test_rom_extensions_merges_configs_and_skips_bad_ones(tmp_path):
    base = tmp_path / "es_systems.cfg"
    base.write_text(ES_CFG)
    extra = tmp_path / "es_systems_custom.cfg"
    extra.write_text("<systemList><system><name>switch</name><extension>.nsz</extension></system></systemList>")
    broken = tmp_path / "broken.cfg"
    broken.write_text("<systemList><oops>")
    cfgs = [str(base), str(broken), str(tmp_path / "missing.cfg"), str(extra)]
    assert rom_extensions("switch", cfgs) == {".nsp", ".xci", ".nsz"}


def test_scan_roms(tmp_path):
    (tmp_path / "A.nsp").write_text("x")
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "ignored.nsp").write_text("x")
    (tmp_path / "Sub").mkdir()
    (tmp_path / "Sub" / "B.nsp").write_text("x")
    (tmp_path / "Folder.wine").mkdir()
    (tmp_path / "Folder.wine" / "inside.nsp").write_text("x")
    (tmp_path / ".hidden.nsp").write_text("x")
    (tmp_path / "deep" / "deeper").mkdir(parents=True)
    (tmp_path / "deep" / "deeper" / "C.nsp").write_text("x")
    got = scan_roms(tmp_path, {".nsp", ".wine"}, max_depth=2)
    assert got == ["./A.nsp", "./Folder.wine", "./Sub/B.nsp"]
    assert "./deep/deeper/C.nsp" in scan_roms(tmp_path, {".nsp", ".wine"})      # default depth 3 reaches it


def test_scan_roms_is_sorted_case_insensitively_and_skips_hidden_dirs(tmp_path):
    for name in ("b.nsp", "C.nsp", "a.nsp", "notes.txt"):
        (tmp_path / name).write_text("x")
    (tmp_path / ".Trash").mkdir()
    (tmp_path / ".Trash" / "gone.nsp").write_text("x")
    assert scan_roms(tmp_path, {".nsp"}) == ["./a.nsp", "./b.nsp", "./C.nsp"]


def test_scan_roms_matches_extensions_case_insensitively(tmp_path):
    (tmp_path / "Upper.NSP").write_text("x")
    assert scan_roms(tmp_path, {".nsp"}) == ["./Upper.NSP"]


def test_scan_roms_missing_dir_is_empty(tmp_path):
    assert scan_roms(tmp_path / "nope", {".nsp"}) == []


def test_scan_roms_follows_symlinked_dirs(tmp_path):
    share = tmp_path / "share"
    (share / "Game One").mkdir(parents=True)
    (share / "Game One" / "one.nsp").write_text("x")
    system = tmp_path / "switch"
    system.mkdir()
    (system / "switch").symlink_to(share, target_is_directory=True)   # like /userdata/roms/switch/switch
    assert scan_roms(system, {".nsp"}) == ["./switch/Game One/one.nsp"]


def test_scan_roms_survives_symlink_cycles(tmp_path):
    system = tmp_path / "switch"
    (system / "Sub").mkdir(parents=True)
    (system / "Sub" / "B.nsp").write_text("x")
    (system / "Sub" / "loop").symlink_to(system, target_is_directory=True)   # points at an ancestor
    (system / "self").symlink_to(system / "self", target_is_directory=True)  # points at itself
    assert scan_roms(system, {".nsp"}, max_depth=10) == ["./Sub/B.nsp"]


def test_scan_roms_skips_broken_symlinks(tmp_path):
    (tmp_path / "ok.nsp").write_text("x")
    (tmp_path / "dangling.nsp").symlink_to(tmp_path / "missing.nsp")
    assert scan_roms(tmp_path, {".nsp"}) == ["./ok.nsp"]


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions as non-root")
def test_scan_roms_ignores_unreadable_dirs(tmp_path):
    (tmp_path / "A.nsp").write_text("x")
    locked = tmp_path / "Locked"
    locked.mkdir()
    (locked / "B.nsp").write_text("x")
    locked.chmod(0)
    try:
        assert scan_roms(tmp_path, {".nsp"}) == ["./A.nsp"]
    finally:
        locked.chmod(0o755)


def test_asset_status(tmp_path):
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "A-image.jpg").write_bytes(b"jpg")
    xml = tmp_path / "gamelist.xml"
    xml.write_text(
        '<?xml version="1.0"?><gameList><game><path>./A.nsp</path><desc>d</desc>'
        "<image>./images/A-image.jpg</image><marquee>./images/A-marquee.png</marquee></game></gameList>"
    )
    st = asset_status(tmp_path, Gamelist(xml), "./A.nsp")
    assert st["desc"] is True and st["image"] is True
    assert st["marquee"] is False          # tag present but file missing
    assert st["thumbnail"] is False and st["video"] is False
    assert st["complete"] is False
    none = asset_status(tmp_path, Gamelist(xml), "./B.nsp")
    assert not any(none[k] for k in ("desc", "image", "thumbnail", "marquee", "video"))


def test_asset_status_complete_when_everything_present(tmp_path):
    (tmp_path / "images").mkdir()
    (tmp_path / "videos").mkdir()
    for rel in ("images/A-image.jpg", "images/A-thumb.png", "images/A-marquee.png", "videos/A-video.mp4"):
        (tmp_path / rel).write_bytes(b"x")
    xml = tmp_path / "gamelist.xml"
    xml.write_text(
        '<?xml version="1.0"?><gameList><game><path>./A.nsp</path><desc>d</desc>'
        "<image>./images/A-image.jpg</image><thumbnail>./images/A-thumb.png</thumbnail>"
        "<marquee>./images/A-marquee.png</marquee><video>./videos/A-video.mp4</video></game></gameList>"
    )
    st = asset_status(tmp_path, Gamelist(xml), "./A.nsp")
    assert st == {"desc": True, "image": True, "thumbnail": True, "marquee": True, "video": True, "complete": True}


def test_asset_status_blank_values_and_directories_do_not_count(tmp_path):
    (tmp_path / "images" / "A-image.jpg").mkdir(parents=True)     # a directory is not an image file
    xml = tmp_path / "gamelist.xml"
    xml.write_text(
        '<?xml version="1.0"?><gameList><game><path>./A.nsp</path><desc>  </desc>'
        "<image>./images/A-image.jpg</image><video/></game></gameList>"
    )
    st = asset_status(tmp_path, Gamelist(xml), "./A.nsp")
    assert st["desc"] is False and st["image"] is False and st["video"] is False


def test_asset_status_zero_byte_media_counts_as_missing(tmp_path):
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "A-image.jpg").write_bytes(b"")              # left behind by a failed write
    (tmp_path / "images" / "A-thumb.png").write_bytes(b"x")
    xml = tmp_path / "gamelist.xml"
    xml.write_text(
        '<?xml version="1.0"?><gameList><game><path>./A.nsp</path><desc>d</desc>'
        "<image>./images/A-image.jpg</image><thumbnail>./images/A-thumb.png</thumbnail></game></gameList>"
    )
    st = asset_status(tmp_path, Gamelist(xml), "./A.nsp")
    assert st["image"] is False and st["thumbnail"] is True
