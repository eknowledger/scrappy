import os
import re
import xml.etree.ElementTree as ET

import pytest

from scrappy.gamelist import Gamelist, backup_file

SAMPLE = """<?xml version="1.0"?>
<gameList>
\t<game id="1">
\t\t<path>./Marmalade Moon [0100AAAA00000800][v65536].nsp</path>
\t\t<name>Marmalade Moon</name>
\t\t<desc>old description</desc>
\t\t<image>./images/Marmalade Moon [0100AAAA00000800][v65536]-image.jpg</image>
\t\t<playcount>7</playcount>
\t\t<favorite>true</favorite>
\t\t<customtag>keep me</customtag>
\t\t<scrap name="ScreenScraper" date="20250820T074156" />
\t</game>
</gameList>
"""


def make(tmp_path, text=SAMPLE):
    p = tmp_path / "gamelist.xml"
    p.write_text(text, encoding="utf-8")
    return p


def test_find_ignores_leading_dot_slash(tmp_path):
    gl = Gamelist(make(tmp_path))
    assert gl.find("./Marmalade Moon [0100AAAA00000800][v65536].nsp") is not None
    assert gl.find("Marmalade Moon [0100AAAA00000800][v65536].nsp") is not None
    assert gl.find("./Other.nsp") is None


def test_get_returns_fields(tmp_path):
    gl = Gamelist(make(tmp_path))
    got = gl.get("./Marmalade Moon [0100AAAA00000800][v65536].nsp")
    assert got["name"] == "Marmalade Moon" and got["playcount"] == "7"


def test_upsert_existing_preserves_everything_else(tmp_path):
    p = make(tmp_path)
    gl = Gamelist(p)
    gl.upsert("./Marmalade Moon [0100AAAA00000800][v65536].nsp", {"desc": "new description", "genre": "Horror"})
    gl.save()
    root = ET.parse(p).getroot()
    g = root.find("game")
    assert g.findtext("desc") == "new description"
    assert g.findtext("genre") == "Horror"
    assert g.findtext("playcount") == "7"
    assert g.findtext("favorite") == "true"
    assert g.findtext("customtag") == "keep me"
    assert g.findtext("name") == "Marmalade Moon"
    assert g.find("scrap").get("name") == "Scrappy"
    assert len(root.findall("game")) == 1


def test_upsert_new_game_appends_and_reparses(tmp_path):
    p = make(tmp_path)
    gl = Gamelist(p)
    gl.upsert("./Tidepool Tactics/Tidepool Tactics.nsp", {"name": "Tidepool Tactics: Remastered", "desc": "Scary & <fun>"})
    gl.save()
    root = ET.parse(p).getroot()
    games = root.findall("game")
    assert len(games) == 2
    new = games[1]
    assert new.findtext("path") == "./Tidepool Tactics/Tidepool Tactics.nsp"
    assert new.findtext("desc") == "Scary & <fun>"
    assert games[0].findtext("playcount") == "7"


def test_save_is_atomic_and_has_declaration(tmp_path):
    p = make(tmp_path)
    gl = Gamelist(p)
    gl.upsert("./X.nsp", {"name": "X"})
    gl.save()
    assert p.read_bytes().startswith(b'<?xml version="1.0"?>')
    assert [f.name for f in tmp_path.iterdir()] == ["gamelist.xml"]


def test_new_gamelist_when_file_missing(tmp_path):
    p = tmp_path / "gamelist.xml"
    gl = Gamelist(p)
    gl.upsert("./A.nsp", {"name": "A"})
    gl.save()
    assert ET.parse(p).getroot().find("game").findtext("name") == "A"


def test_backup_file_and_pruning(tmp_path):
    sysdir = tmp_path / "snes"
    sysdir.mkdir()
    p = make(sysdir)
    other = tmp_path / "gba"
    other.mkdir()
    op = make(other)
    bdir = tmp_path / "bk"
    other_made = [backup_file(op, bdir, keep=2) for _ in range(3)]
    made = [backup_file(p, bdir, keep=2) for _ in range(3)]
    assert made[0].name == "snes-gamelist-original.xml"
    assert all(m.exists() for m in made)
    mine = sorted(f for f in bdir.iterdir() if f.name.startswith("snes-gamelist-"))
    assert len(mine) == 3
    oldest_ts = sorted(m for m in mine if "original" not in m.name)[0]
    fourth = backup_file(p, bdir, keep=2)
    mine = sorted(f for f in bdir.iterdir() if f.name.startswith("snes-gamelist-"))
    assert len(mine) == 3
    assert not oldest_ts.exists()
    assert made[0].exists() and fourth.exists()
    assert fourth.read_text(encoding="utf-8") == SAMPLE
    assert all(f.exists() for f in other_made)
    assert len([f for f in bdir.iterdir() if f.name.startswith("gba-gamelist-")]) == 3


def test_backup_with_glob_characters_in_system_dir(tmp_path):
    sysdir = tmp_path / "Games [USA]"
    sysdir.mkdir()
    p = make(sysdir)
    bdir = tmp_path / "bk"
    for _ in range(5):
        backup_file(p, bdir, keep=2)
    files = list(bdir.iterdir())
    assert len(files) == 3
    assert sum(1 for f in files if f.stem.endswith("-original")) == 1


def test_backup_keep_must_be_positive(tmp_path):
    p = make(tmp_path)
    with pytest.raises(ValueError):
        backup_file(p, tmp_path / "bk", keep=0)
    assert not (tmp_path / "bk").exists() or not list((tmp_path / "bk").iterdir())


def test_backup_returned_path_exists_and_is_utc_named(tmp_path):
    p = make(tmp_path)
    bdir = tmp_path / "bk"
    for _ in range(4):
        assert backup_file(p, bdir, keep=1).exists()
    ts = [f for f in bdir.iterdir() if not f.stem.endswith("-original")]
    assert len(ts) == 1
    assert re.fullmatch(r".+-\d{8}-\d{6}-\d{6}\.xml", ts[0].name)


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_upsert_skips_empty_values(tmp_path, empty):
    p = make(tmp_path)
    gl = Gamelist(p)
    gl.upsert("./Marmalade Moon [0100AAAA00000800][v65536].nsp", {"desc": empty, "genre": empty})
    gl.save()
    assert p.read_bytes() == SAMPLE.encode("utf-8")


def test_upsert_skips_empty_but_writes_others(tmp_path):
    gl = Gamelist(make(tmp_path))
    g = gl.upsert("./Marmalade Moon [0100AAAA00000800][v65536].nsp", {"desc": "", "genre": "Horror"})
    assert g.findtext("desc") == "old description"
    assert g.findtext("genre") == "Horror"
    assert g.find("scrap").get("name") == "Scrappy"


def test_upsert_rejects_non_str(tmp_path):
    gl = Gamelist(make(tmp_path))
    with pytest.raises(TypeError, match="playcount"):
        gl.upsert("./Marmalade Moon [0100AAAA00000800][v65536].nsp", {"playcount": 3})
    with pytest.raises(TypeError, match="playcount"):
        gl.upsert("./New.nsp", {"playcount": 3})
    assert gl.find("./New.nsp") is None


def test_upsert_new_game_with_nothing_to_write_still_creates_path(tmp_path):
    gl = Gamelist(make(tmp_path))
    g = gl.upsert("./New.nsp", {"desc": ""})
    assert g.findtext("path") == "./New.nsp"
    assert g.find("desc") is None


def test_save_failure_leaves_original_and_no_tmp(tmp_path, monkeypatch):
    p = make(tmp_path)
    before = p.read_bytes()
    gl = Gamelist(p)
    gl.upsert("./X.nsp", {"name": "X"})

    def boom(*a, **k):
        raise OSError("disk on fire")

    monkeypatch.setattr("scrappy.gamelist.os.replace", boom)
    with pytest.raises(OSError):
        gl.save()
    monkeypatch.undo()
    assert p.read_bytes() == before
    assert [f.name for f in tmp_path.iterdir()] == ["gamelist.xml"]


def test_save_keeps_symlink(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    target = make(real_dir)
    link_dir = tmp_path / "link"
    link_dir.mkdir()
    link = link_dir / "gamelist.xml"
    link.symlink_to(target)
    gl = Gamelist(link)
    gl.upsert("./X.nsp", {"name": "X"})
    gl.save()
    assert link.is_symlink()
    assert b"<name>X</name>" in target.read_bytes()
    assert [f.name for f in link_dir.iterdir()] == ["gamelist.xml"]
    assert [f.name for f in real_dir.iterdir()] == ["gamelist.xml"]


def test_save_preserves_file_mode(tmp_path):
    p = make(tmp_path)
    os.chmod(p, 0o640)
    gl = Gamelist(p)
    gl.upsert("./X.nsp", {"name": "X"})
    gl.save()
    assert (p.stat().st_mode & 0o777) == 0o640


def test_round_trip_is_byte_identical(tmp_path):
    p = make(tmp_path)
    Gamelist(p).save()
    assert p.read_bytes() == SAMPLE.encode("utf-8")


def _freeze_date(data: bytes) -> str:
    return re.sub(r'date="\d{8}T\d{6}"', 'date="DATE"', data.decode("utf-8"))


def test_new_game_bytes_match_emulationstation_layout(tmp_path):
    p = make(tmp_path)
    gl = Gamelist(p)
    gl.upsert("./New Game.nsp", {"name": "New", "desc": "A & B"})
    gl.save()
    expected = SAMPLE.replace(
        "</gameList>\n",
        "\t<game>\n"
        "\t\t<path>./New Game.nsp</path>\n"
        "\t\t<name>New</name>\n"
        "\t\t<desc>A &amp; B</desc>\n"
        '\t\t<scrap name="Scrappy" date="DATE" />\n'
        "\t</game>\n"
        "</gameList>\n",
    )
    assert _freeze_date(p.read_bytes()) == _freeze_date(expected.encode("utf-8"))


def test_new_game_into_missing_file_bytes(tmp_path):
    p = tmp_path / "gamelist.xml"
    gl = Gamelist(p)
    gl.upsert("./A.nsp", {"name": "A"})
    gl.save()
    expected = (
        '<?xml version="1.0"?>\n'
        "<gameList>\n"
        "\t<game>\n"
        "\t\t<path>./A.nsp</path>\n"
        "\t\t<name>A</name>\n"
        '\t\t<scrap name="Scrappy" date="DATE" />\n'
        "\t</game>\n"
        "</gameList>\n"
    )
    assert _freeze_date(p.read_bytes()) == expected


def test_existing_game_update_bytes(tmp_path):
    p = make(tmp_path)
    gl = Gamelist(p)
    gl.upsert("./Marmalade Moon [0100AAAA00000800][v65536].nsp", {"desc": "new", "genre": "Horror"})
    gl.save()
    expected = SAMPLE.replace("old description", "new").replace(
        '\t\t<scrap name="ScreenScraper" date="20250820T074156" />\n',
        '\t\t<scrap name="Scrappy" date="DATE" />\n'
        "\t\t<genre>Horror</genre>\n",  # new tags are appended after existing children
    )
    assert _freeze_date(p.read_bytes()) == expected


def test_backup_systems_with_overlapping_names_and_stray_files(tmp_path):
    a = tmp_path / "switch"
    b = tmp_path / "switch-gamelist"
    a.mkdir()
    b.mkdir()
    pa, pb = make(a), make(b)
    bdir = tmp_path / "bk"
    bdir.mkdir()
    stray = bdir / "switch-gamelist-notes.xml"
    stray.write_text("mine", encoding="utf-8")
    made_b = [backup_file(pb, bdir, keep=1) for _ in range(2)]
    made_a = [backup_file(pa, bdir, keep=1) for _ in range(5)]
    assert made_a[0].name == "switch-gamelist-original.xml"
    assert made_b[0].name == "switch-gamelist-gamelist-original.xml"
    assert made_a[0].exists() and made_b[0].exists() and made_b[1].exists()
    assert stray.read_text(encoding="utf-8") == "mine"
    a_files = [f for f in bdir.iterdir() if f.name.startswith("switch-gamelist-2") or f == made_a[0]]
    assert len(a_files) == 2  # original + 1 timestamped
    for _ in range(3):
        backup_file(pb, bdir, keep=1)
    assert made_a[0].exists() and made_a[-1].exists() and stray.exists()
    b_files = [f for f in bdir.iterdir() if f.name.startswith("switch-gamelist-gamelist-")]
    assert len(b_files) == 2


def test_backup_is_atomic_and_never_leaves_a_partial_copy(tmp_path, monkeypatch):
    p = make(tmp_path)
    bk = tmp_path / "bk"

    def boom(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("scrappy.gamelist.os.fsync", boom)
    with pytest.raises(OSError):
        backup_file(p, bk)
    assert list(bk.iterdir()) == []                      # no half-written "original" and no stray .tmp
    monkeypatch.undo()
    first = backup_file(p, bk)
    assert first.name.endswith("-original.xml") and first.read_bytes() == p.read_bytes()
    assert [f.name for f in bk.iterdir()] == [first.name]


def test_save_and_backup_do_not_follow_planted_tmp_symlinks(tmp_path):
    p = make(tmp_path)
    victim = tmp_path / "victim"
    victim.write_bytes(b"precious")
    (tmp_path / "gamelist.xml.tmp").symlink_to(victim)
    bk = tmp_path / "bk"
    bk.mkdir()
    (bk / f"{tmp_path.name}-gamelist-original.xml.tmp").symlink_to(victim)
    dest = backup_file(p, bk)
    gl = Gamelist(p)
    gl.upsert("./Marmalade Moon [0100AAAA00000800][v65536].nsp", {"desc": "x"})
    gl.save()
    assert victim.read_bytes() == b"precious" and dest.read_bytes() == SAMPLE.encode()
    assert ET.parse(p).getroot().find("game").findtext("desc") == "x"


def test_to_bytes_is_exactly_what_save_writes(tmp_path):
    p = make(tmp_path)
    gl = Gamelist(p)
    gl.upsert("./Z.nsp", {"name": "Z"})
    expected = gl.to_bytes()
    gl.save()
    assert p.read_bytes() == expected


def test_backup_pruning_tolerates_a_file_that_vanished(tmp_path, monkeypatch):
    from pathlib import Path
    p = make(tmp_path)
    bk = tmp_path / "bk"
    for _ in range(3):
        backup_file(p, bk, keep=2)
    real = Path.unlink

    def vanished(self, missing_ok=False):
        real(self, missing_ok=True)                  # somebody else deleted it first
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", vanished)
    backup_file(p, bk, keep=2)
    assert len([f for f in bk.iterdir() if not f.name.endswith("-original.xml")]) == 2
