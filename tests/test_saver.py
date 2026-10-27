import errno
import os
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import scrappy.saver as saver_mod
from scrappy.gamelist import Gamelist
from scrappy.providers.base import ProviderError, describe_error
from scrappy.roms import asset_status
from scrappy.saver import (SaveError, SaveReport, SaveRequest, Saver, StaleGamelist, UndoRefused,
                       emulationstation_running, save_game, undo_last)

SAMPLE = """<?xml version="1.0"?>
<gameList>
\t<game id="1">
\t\t<path>./Tidepool Tactics.nsp</path>
\t\t<name>Tidepool gibberish</name>
\t\t<playcount>3</playcount>
\t\t<favorite>true</favorite>
\t</game>
</gameList>
"""
STEM = "Tidepool Tactics"


def setup(tmp_path):
    system = tmp_path / "switch"
    system.mkdir()
    (system / "gamelist.xml").write_text(SAMPLE, encoding="utf-8")
    return system, tmp_path / "backups", tmp_path / "journal"


def req(system, **kw):
    base = dict(system_dir=system, rom_path="./Tidepool Tactics.nsp",
                fields={"name": "Tidepool Tactics: Remastered", "desc": "Scary."},
                image_jpg=b"JPG", thumb_png=b"THUMB", marquee_png=b"LOGO")
    base.update(kw)
    return SaveRequest(**base)


def make_saver(tmp_path, **kw):
    kw.setdefault("es_running", lambda: False)
    return Saver(tmp_path / "backups", tmp_path / "journal", **kw)


def snapshot(root):
    """Every file and folder below root with its content, to prove nothing changed."""
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for d in dirs:
            out[os.path.join(dirpath, d)] = None
        for f in files:
            full = os.path.join(dirpath, f)
            out[full] = open(full, "rb").read()
    return out


def game(system, rom="./Tidepool Tactics.nsp"):
    return Gamelist(system / "gamelist.xml").get(rom)


# ------------------------------------------------------------------ basic behaviour (module functions)

def test_save_writes_files_and_xml_and_keeps_play_data(tmp_path):
    system, backups, journal = setup(tmp_path)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"MP4")
    report = save_game(req(system, video_mp4=video), backups, journal)
    assert (system / "images" / f"{STEM}-image.jpg").read_bytes() == b"JPG"
    assert (system / "images" / f"{STEM}-thumb.png").read_bytes() == b"THUMB"
    assert (system / "images" / f"{STEM}-marquee.png").read_bytes() == b"LOGO"
    assert (system / "videos" / f"{STEM}-video.mp4").read_bytes() == b"MP4"
    g = ET.parse(system / "gamelist.xml").getroot().find("game")
    assert g.findtext("name") == "Tidepool Tactics: Remastered" and g.findtext("desc") == "Scary."
    assert g.findtext("image") == f"./images/{STEM}-image.jpg"
    assert g.findtext("video") == f"./videos/{STEM}-video.mp4"
    assert g.findtext("playcount") == "3" and g.findtext("favorite") == "true"
    assert report.backup is not None and report.backup.exists() and len(report.written) == 4
    assert isinstance(report, SaveReport) and report.backup_taken is True
    assert not list(system.rglob("*.tmp"))
    assert asset_status(system, Gamelist(system / "gamelist.xml"), "./Tidepool Tactics.nsp")["image"] is True


def test_existing_files_are_kept_unless_replace_is_requested(tmp_path):
    system, backups, journal = setup(tmp_path)
    (system / "images").mkdir()
    old = system / "images" / f"{STEM}-image.jpg"
    old.write_bytes(b"OLD")
    r1 = save_game(req(system), backups, journal)
    assert old.read_bytes() == b"OLD" and str(old) in r1.kept
    r2 = save_game(req(system, replace={"image"}), backups, journal)
    assert old.read_bytes() == b"JPG" and str(old) in r2.replaced


def test_undo_restores_everything(tmp_path):
    system, backups, journal = setup(tmp_path)
    original = (system / "gamelist.xml").read_bytes()
    (system / "images").mkdir()
    old = system / "images" / f"{STEM}-image.jpg"
    old.write_bytes(b"OLD")
    save_game(req(system, replace={"image"}), backups, journal)
    assert old.read_bytes() == b"JPG"
    undone = undo_last(journal)
    assert undone is not None and undone["rom_path"] == "./Tidepool Tactics.nsp"
    assert (system / "gamelist.xml").read_bytes() == original
    assert old.read_bytes() == b"OLD"
    assert not (system / "images" / f"{STEM}-thumb.png").exists()
    assert undo_last(journal) is None                       # nothing left to undo


def test_save_into_system_without_gamelist_then_undo_removes_it(tmp_path):
    system = tmp_path / "newsys"
    system.mkdir()
    r = save_game(req(system, rom_path="./A.nsp"), tmp_path / "bk", tmp_path / "jr")
    assert (system / "gamelist.xml").exists() and r.backup is None
    assert (system / "images" / "A-image.jpg").exists() and (system / "images").is_dir()
    undo_last(tmp_path / "jr")
    assert not (system / "gamelist.xml").exists()
    assert not (system / "images").exists()                 # the folder we created goes away again


def test_unrelated_games_and_unknown_tags_survive(tmp_path):
    system, backups, journal = setup(tmp_path)
    (system / "gamelist.xml").write_text(SAMPLE.replace("</gameList>", "\t<game><path>./Other.nsp</path>"
                                                        "<customtag>x</customtag></game>\n</gameList>"), encoding="utf-8")
    save_game(req(system), backups, journal)
    other = Gamelist(system / "gamelist.xml").get("./Other.nsp")
    assert other == {"path": "./Other.nsp", "customtag": "x"}


def test_games_in_subfolders_follow_media_naming(tmp_path):
    system, backups, journal = setup(tmp_path)
    save_game(req(system, rom_path="./Sub Folder/Game [01].nsp"), backups, journal)
    assert (system / "images" / "Game [01]-image.jpg").read_bytes() == b"JPG"
    got = game(system, "./Sub Folder/Game [01].nsp")
    assert got["path"] == "./Sub Folder/Game [01].nsp" and got["image"] == "./images/Game [01]-image.jpg"


def test_fields_only_no_media(tmp_path):
    system, backups, journal = setup(tmp_path)
    r = save_game(SaveRequest(system_dir=system, rom_path="./Tidepool Tactics.nsp", fields={"desc": "Only text"}),
                  backups, journal)
    assert r.written == [] and game(system)["desc"] == "Only text"
    assert not (system / "images").exists()


def test_symlinked_gamelist_stays_a_symlink(tmp_path):
    system, backups, journal = setup(tmp_path)
    real = tmp_path / "real-gamelist.xml"
    (system / "gamelist.xml").rename(real)
    (system / "gamelist.xml").symlink_to(real)
    save_game(req(system), backups, journal)
    assert (system / "gamelist.xml").is_symlink() and "Scary." in real.read_text()
    undo_last(journal)
    assert (system / "gamelist.xml").is_symlink() and real.read_text() == SAMPLE


# ------------------------------------------------------------------ zero-byte files

def test_zero_byte_media_is_written_over_without_replace_and_restored_empty_by_undo(tmp_path):
    system, backups, journal = setup(tmp_path)
    (system / "images").mkdir()
    empty = system / "images" / f"{STEM}-image.jpg"
    empty.write_bytes(b"")
    r = save_game(req(system), backups, journal)
    assert empty.read_bytes() == b"JPG" and str(empty) not in r.kept
    undo_last(journal)
    assert empty.exists() and empty.read_bytes() == b""


# ------------------------------------------------------------------ one backup per session

def test_one_backup_per_session_and_a_new_one_after_an_external_change(tmp_path):
    system, backups, journal = setup(tmp_path)
    s = make_saver(tmp_path)
    r1 = s.save(req(system))
    r2 = s.save(req(system, rom_path="./Two.nsp"))
    r3 = s.save(req(system, rom_path="./Three.nsp"))
    assert r1.backup_taken and not r2.backup_taken and not r3.backup_taken
    assert r2.backup == r1.backup and r3.backup == r1.backup           # still reported, nothing new made
    assert len(list(backups.iterdir())) == 1
    assert next(backups.iterdir()).read_bytes() == SAMPLE.encode()      # holds the file as it was before the session
    gl = system / "gamelist.xml"
    gl.write_text(gl.read_text().replace("<playcount>3", "<playcount>9"))   # EmulationStation rewrote it
    r4 = s.save(req(system, rom_path="./Four.nsp"))
    assert r4.backup_taken and r4.backup != r1.backup and len(list(backups.iterdir())) == 2
    assert "<playcount>9" in r4.backup.read_text()                      # the new backup has ES's change
    s.new_session()
    assert s.save(req(system, rom_path="./Five.nsp")).backup_taken


def test_different_systems_each_get_their_own_backup(tmp_path):
    system, backups, journal = setup(tmp_path)
    other = tmp_path / "snes"
    other.mkdir()
    (other / "gamelist.xml").write_text(SAMPLE)
    s = make_saver(tmp_path)
    assert s.save(req(system)).backup_taken and s.save(req(other)).backup_taken
    assert len(list(backups.iterdir())) == 2


# ------------------------------------------------------------------ stale-tree guard

def _meddling_gamelist(times):
    """Gamelist whose first `times` upserts are followed by 'EmulationStation' rewriting the file on disk."""
    state = {"left": times}

    class Meddling(Gamelist):
        def upsert(self, rom_path, fields):
            out = super().upsert(rom_path, fields)
            if state["left"] > 0:
                state["left"] -= 1
                text = self.path.read_text()
                self.path.write_text(text.replace("<playcount>3", f"<playcount>{9 - state['left']}") + " ")
            return out
    return Meddling


def test_changed_gamelist_is_reloaded_and_our_changes_are_applied_on_top(tmp_path, monkeypatch):
    system, backups, journal = setup(tmp_path)
    monkeypatch.setattr(saver_mod, "Gamelist", _meddling_gamelist(1))
    r = make_saver(tmp_path).save(req(system))
    got = game(system)
    assert got["playcount"] == "9" and got["desc"] == "Scary." and got["image"].endswith("-image.jpg")   # ES's edit kept
    # the journal's "before" is what we actually replaced, so undo gives back ES's version, not the stale one
    undo_last(journal)
    assert game(system)["playcount"] == "9" and "desc" not in game(system)
    assert r.backup.exists()


def test_gamelist_that_keeps_changing_raises_stale_and_leaves_nothing_behind(tmp_path, monkeypatch):
    system, backups, journal = setup(tmp_path)
    monkeypatch.setattr(saver_mod, "Gamelist", _meddling_gamelist(99))
    with pytest.raises(StaleGamelist) as ei:
        make_saver(tmp_path).save(req(system))
    assert "emulationstation" in str(ei.value).lower()
    assert not (system / "images").exists()                              # media rolled back, folder removed
    assert "desc" not in game(system)                                     # our edit was never written
    assert not journal.exists() or not list(journal.iterdir())
    assert isinstance(ei.value, ProviderError) and describe_error(ei.value) == str(ei.value)


def test_gamelist_that_cannot_be_parsed_is_never_overwritten(tmp_path):
    system, backups, journal = setup(tmp_path)
    gl = system / "gamelist.xml"
    gl.write_text("<gameList><game>")
    with pytest.raises(SaveError, match="gamelist.xml"):
        make_saver(tmp_path).save(req(system))
    assert gl.read_text() == "<gameList><game>" and not (system / "images").exists()


def test_emulationstation_running_scans_proc(tmp_path):
    assert emulationstation_running(tmp_path / "no-such-proc") is False
    for pid, comm in (("1", "systemd"), ("77", "bash"), ("self", "python")):
        d = tmp_path / pid
        d.mkdir()
        (d / "comm").write_text(comm + "\n")
    (tmp_path / "notapid").mkdir()
    assert emulationstation_running(tmp_path) is False
    d = tmp_path / "412"
    d.mkdir()
    (d / "comm").write_text("emulationstatio\n")                        # the kernel cuts comm at 15 characters
    assert emulationstation_running(tmp_path) is True


def test_emulationstation_running_matches_cmdline_when_comm_is_unhelpful(tmp_path):
    d = tmp_path / "9"
    d.mkdir()
    (d / "comm").write_text("python3\n")
    (d / "cmdline").write_bytes(b"/usr/bin/emulationstation\x00--no-splash\x00")
    assert emulationstation_running(tmp_path) is True


def test_report_tells_the_ui_whether_emulationstation_is_running(tmp_path):
    system, backups, journal = setup(tmp_path)
    assert make_saver(tmp_path, es_running=lambda: True).save(req(system)).es_running is True

    def broken():
        raise RuntimeError("no /proc")
    assert make_saver(tmp_path, es_running=broken).save(req(system, rom_path="./B.nsp")).es_running is False
    assert "restart" in saver_mod.ES_RESTART_HINT.lower()


# ------------------------------------------------------------------ errors and rollback

def test_disk_full_while_writing_media_rolls_everything_back(tmp_path, monkeypatch):
    system, backups, journal = setup(tmp_path)
    (system / "images").mkdir()
    old = system / "images" / f"{STEM}-thumb.png"
    old.write_bytes(b"OLD")
    before = snapshot(system)
    real = saver_mod.write_atomic
    calls = []

    def flaky(path, data):
        calls.append(path)
        if len(calls) == 3:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(path, data)

    monkeypatch.setattr(saver_mod, "write_atomic", flaky)
    with pytest.raises(SaveError) as ei:
        make_saver(tmp_path).save(req(system, replace={"thumbnail"}))
    assert "disk is full" in str(ei.value).lower()
    assert snapshot(system) == before                                    # no half-written media, old art intact
    assert not journal.exists() or not list(journal.iterdir())


def test_gamelist_write_failure_rolls_media_back(tmp_path, monkeypatch):
    system, backups, journal = setup(tmp_path)
    before = snapshot(system)

    def boom(self):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(Gamelist, "save", boom)
    with pytest.raises(SaveError, match="permission"):
        make_saver(tmp_path).save(req(system))
    assert snapshot(system) == before


def test_journal_failure_after_gamelist_was_written_restores_the_gamelist(tmp_path, monkeypatch):
    system, backups, journal = setup(tmp_path)
    before = snapshot(system)

    def boom(self, *a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(Saver, "_write_journal", boom)
    with pytest.raises(SaveError):
        make_saver(tmp_path).save(req(system))
    assert snapshot(system) == before


def test_missing_system_folder_is_an_error_not_created(tmp_path):
    with pytest.raises(SaveError, match="folder"):
        make_saver(tmp_path).save(req(tmp_path / "nope"))
    assert not (tmp_path / "nope").exists()


def test_images_that_is_a_file_gives_a_clear_error_and_changes_nothing(tmp_path):
    system, backups, journal = setup(tmp_path)
    (system / "images").write_bytes(b"i am a file")
    before = snapshot(system)
    with pytest.raises(SaveError) as ei:
        make_saver(tmp_path).save(req(system))
    assert "images" in str(ei.value)
    assert snapshot(system) == before


def test_read_only_system_folder_gives_a_permission_message(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores permissions")
    system, backups, journal = setup(tmp_path)
    system.chmod(0o555)
    try:
        with pytest.raises(SaveError, match="(?i)permission"):
            make_saver(tmp_path).save(req(system))
    finally:
        system.chmod(0o755)
    assert (system / "gamelist.xml").read_text() == SAMPLE and not (system / "images").exists()


def test_unreadable_video_file_changes_nothing(tmp_path):
    system, backups, journal = setup(tmp_path)
    before = snapshot(system)
    with pytest.raises(SaveError, match="(?i)video"):
        make_saver(tmp_path).save(req(system, video_mp4=tmp_path / "missing.mp4"))
    assert snapshot(system) == before
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(SaveError, match="(?i)video"):
        make_saver(tmp_path).save(req(system, video_mp4=empty))
    assert snapshot(system) == before


def test_empty_picture_bytes_are_refused(tmp_path):
    system, backups, journal = setup(tmp_path)
    before = snapshot(system)
    with pytest.raises(SaveError, match="(?i)image"):
        make_saver(tmp_path).save(req(system, image_jpg=b""))
    assert snapshot(system) == before


def test_bad_field_values_never_reach_the_disk(tmp_path):
    system, backups, journal = setup(tmp_path)
    before = snapshot(system)
    with pytest.raises(TypeError):
        make_saver(tmp_path).save(req(system, fields={"desc": 5}))
    assert snapshot(system) == before


@pytest.mark.parametrize("tag", ["path", "scrap", "image", "playcount", "a b", "x><y", ""])
def test_only_text_fields_are_accepted(tmp_path, tag):
    system, backups, journal = setup(tmp_path)
    before = snapshot(system)
    with pytest.raises(SaveError, match="(?i)field"):
        make_saver(tmp_path).save(req(system, fields={tag: "x"}))
    assert snapshot(system) == before


def test_characters_xml_cannot_hold_are_removed_so_the_gamelist_stays_readable(tmp_path):
    system, backups, journal = setup(tmp_path)
    make_saver(tmp_path).save(req(system, fields={"desc": "Bad\x00 \x0bbytes\ud800 and ’ fine \n ok"}))
    assert game(system)["desc"] == "Bad bytes and ’ fine \n ok"


# ------------------------------------------------------------------ never write outside the system folder

@pytest.mark.parametrize("rom", ["../Evil.nsp", "./a/../../Evil.nsp", "/etc/Evil.nsp", "", "./", "./x/..", "./A\x00.nsp",
                                 ".\\..\\Evil.nsp"])
def test_unsafe_rom_paths_are_refused(tmp_path, rom):
    system, backups, journal = setup(tmp_path)
    before = snapshot(tmp_path)
    with pytest.raises(SaveError, match="(?i)game path"):
        make_saver(tmp_path).save(req(system, rom_path=rom))
    assert snapshot(tmp_path) == before


def test_symlinked_media_folder_pointing_elsewhere_is_refused(tmp_path):
    system, backups, journal = setup(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (system / "images").symlink_to(elsewhere)
    with pytest.raises(SaveError, match="(?i)outside"):
        make_saver(tmp_path).save(req(system))
    assert list(elsewhere.iterdir()) == [] and (system / "gamelist.xml").read_text() == SAMPLE


def test_symlinked_media_file_pointing_elsewhere_is_refused(tmp_path):
    system, backups, journal = setup(tmp_path)
    (system / "images").mkdir()
    victim = tmp_path / "victim.jpg"
    victim.write_bytes(b"precious")
    (system / "images" / f"{STEM}-image.jpg").symlink_to(victim)
    with pytest.raises(SaveError, match="(?i)outside"):
        make_saver(tmp_path).save(req(system, replace={"image"}))
    assert victim.read_bytes() == b"precious"


def test_media_folder_symlinked_inside_the_system_folder_is_fine(tmp_path):
    system, backups, journal = setup(tmp_path)
    (system / "art").mkdir()
    (system / "images").symlink_to(system / "art")
    make_saver(tmp_path).save(req(system))
    assert (system / "art" / f"{STEM}-image.jpg").read_bytes() == b"JPG"


def test_system_folder_reached_through_a_symlink_is_fine(tmp_path):
    system, backups, journal = setup(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(system)
    make_saver(tmp_path).save(req(link))
    assert (system / "images" / f"{STEM}-image.jpg").exists()


# ------------------------------------------------------------------ journal and undo

def test_undo_walks_back_one_save_at_a_time(tmp_path):
    system, backups, journal = setup(tmp_path)
    s = make_saver(tmp_path)
    s.save(req(system, rom_path="./A.nsp", fields={"desc": "AAA"}))
    gl_after_a = (system / "gamelist.xml").read_bytes()
    s.save(req(system, rom_path="./B.nsp", fields={"desc": "BBB"}))
    assert s.undo_last()["rom_path"] == "./B.nsp"
    assert (system / "gamelist.xml").read_bytes() == gl_after_a          # A is still there, B is gone
    assert (system / "images" / "A-image.jpg").exists() and not (system / "images" / "B-image.jpg").exists()
    assert s.undo_last()["rom_path"] == "./A.nsp"
    assert (system / "gamelist.xml").read_text() == SAMPLE
    assert s.undo_last() is None
    r = s.save(req(system, rom_path="./C.nsp"))                           # saving after undo still works
    assert not r.backup_taken and game(system, "./C.nsp")["image"] == "./images/C-image.jpg"


def test_undo_refuses_when_the_gamelist_changed_after_our_save(tmp_path):
    system, backups, journal = setup(tmp_path)
    s = make_saver(tmp_path)
    s.save(req(system))
    gl = system / "gamelist.xml"
    gl.write_text(gl.read_text().replace("<playcount>3", "<playcount>12"))     # ES wrote it on exit
    changed = gl.read_bytes()
    with pytest.raises(UndoRefused, match="(?i)changed"):
        s.undo_last()
    assert gl.read_bytes() == changed and (system / "images" / f"{STEM}-image.jpg").exists()   # nothing touched
    entry = s.undo_last(force=True)
    assert entry is not None and gl.read_text() == SAMPLE
    assert any(p.read_bytes() == changed for p in journal.rglob("*discarded*"))     # even forced, nothing is lost
    assert not (system / "images").exists()


def test_undo_refuses_when_the_gamelist_vanished(tmp_path):
    system, backups, journal = setup(tmp_path)
    s = make_saver(tmp_path)
    s.save(req(system))
    (system / "gamelist.xml").unlink()
    with pytest.raises(UndoRefused):
        s.undo_last()
    assert s.undo_last(force=True) is not None and (system / "gamelist.xml").read_text() == SAMPLE


def test_undo_leaves_media_the_user_changed_since(tmp_path):
    system, backups, journal = setup(tmp_path)
    (system / "images").mkdir()
    old = system / "images" / f"{STEM}-image.jpg"
    old.write_bytes(b"OLD")
    s = make_saver(tmp_path)
    s.save(req(system, replace={"image"}))
    old.write_bytes(b"USER'S NEWER ART")
    (system / "images" / f"{STEM}-marquee.png").write_bytes(b"USER LOGO")
    entry = s.undo_last()
    assert old.read_bytes() == b"USER'S NEWER ART"
    assert (system / "images" / f"{STEM}-marquee.png").read_bytes() == b"USER LOGO"
    assert sorted(os.path.basename(p) for p in entry["left_alone"]) == [f"{STEM}-image.jpg", f"{STEM}-marquee.png"]
    assert not (system / "images" / f"{STEM}-thumb.png").exists()          # untouched-since files are removed
    assert (system / "gamelist.xml").read_text() == SAMPLE


def test_replaced_files_are_preserved_in_the_journal_folder(tmp_path):
    system, backups, journal = setup(tmp_path)
    (system / "images").mkdir()
    (system / "images" / f"{STEM}-image.jpg").write_bytes(b"OLD ART")
    r = make_saver(tmp_path).save(req(system, replace={"image"}))
    assert [p.read_bytes() for p in (r.journal / "replaced").iterdir()] == [b"OLD ART"]


def test_undo_can_be_repeated_after_a_failure_part_way(tmp_path, monkeypatch):
    system, backups, journal = setup(tmp_path)
    s = make_saver(tmp_path)
    s.save(req(system))
    real = saver_mod.Path.unlink
    state = {"fail": True}

    def flaky_unlink(self, *a, **k):
        if state["fail"] and self.name.endswith("-marquee.png"):
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(self, *a, **k)

    monkeypatch.setattr(saver_mod.Path, "unlink", flaky_unlink)
    with pytest.raises(SaveError):
        s.undo_last()
    assert (system / "gamelist.xml").read_text() == SAMPLE               # restored first
    state["fail"] = False
    assert s.undo_last() is not None                                       # the retry is not refused and finishes the job
    assert not (system / "images").exists() and s.undo_last() is None


def test_undo_reports_a_missing_before_copy(tmp_path):
    system, backups, journal = setup(tmp_path)
    r = make_saver(tmp_path).save(req(system))
    (r.journal / "gamelist.before.xml").unlink()
    with pytest.raises(SaveError, match="(?i)cannot undo"):
        make_saver(tmp_path).undo_last()
    assert "Scary." in (system / "gamelist.xml").read_text()


def test_journal_entries_sort_in_save_order_even_in_the_same_microsecond(tmp_path, monkeypatch):
    system, backups, journal = setup(tmp_path)
    monkeypatch.setattr(saver_mod.time, "time_ns", lambda: 1_700_000_000_000_000_000)
    s = make_saver(tmp_path)
    for name in ("A", "B", "C"):
        s.save(req(system, rom_path=f"./{name}.nsp"))
    assert [s.undo_last()["rom_path"] for _ in range(3)] == ["./C.nsp", "./B.nsp", "./A.nsp"]


# ------------------------------------------------------------------ kept files that the gamelist does not mention

def test_kept_file_that_the_gamelist_does_not_reference_gets_linked(tmp_path):
    system, backups, journal = setup(tmp_path)
    (system / "images").mkdir()
    (system / "images" / f"{STEM}-image.jpg").write_bytes(b"OLD")
    r = make_saver(tmp_path).save(req(system))
    assert game(system)["image"] == f"./images/{STEM}-image.jpg" and r.linked == ["image"]
    assert (system / "images" / f"{STEM}-image.jpg").read_bytes() == b"OLD"


def test_kept_file_does_not_overwrite_a_tag_that_points_to_a_real_file(tmp_path):
    system, backups, journal = setup(tmp_path)
    (system / "gamelist.xml").write_text(SAMPLE.replace("<name>", "<image>./custom/path.jpg</image><name>"))
    (system / "custom").mkdir()
    (system / "custom" / "path.jpg").write_bytes(b"MINE")
    (system / "images").mkdir()
    (system / "images" / f"{STEM}-image.jpg").write_bytes(b"OLD")
    r = make_saver(tmp_path).save(req(system))
    assert game(system)["image"] == "./custom/path.jpg" and r.linked == []
    assert (system / "custom" / "path.jpg").read_bytes() == b"MINE"


def test_keep_with_a_tag_pointing_to_another_existing_file_writes_nothing_for_that_slot(tmp_path):
    system, backups, journal = setup(tmp_path)
    (system / "gamelist.xml").write_text(SAMPLE.replace("<name>", "<image>./media/old.png</image><name>"))
    (system / "media").mkdir()
    (system / "media" / "old.png").write_bytes(b"USER ART")
    r = make_saver(tmp_path).save(req(system, image_jpg=b"NEW", thumb_png=None, marquee_png=None))
    assert not (system / "images").exists()                              # no orphan canonical file
    assert game(system)["image"] == "./media/old.png" and r.written == []
    assert r.kept == [str(system / "media" / "old.png")] and r.linked == []
    assert (system / "media" / "old.png").read_bytes() == b"USER ART"


def test_zero_byte_canonical_file_does_not_override_a_tag_pointing_to_real_art(tmp_path):
    system, backups, journal = setup(tmp_path)
    (system / "gamelist.xml").write_text(SAMPLE.replace("<name>", "<image>./media/old.png</image><name>"))
    (system / "media").mkdir()
    (system / "media" / "old.png").write_bytes(b"USER ART")
    (system / "images").mkdir()
    (system / "images" / f"{STEM}-image.jpg").write_bytes(b"")
    r = make_saver(tmp_path, ).save(req(system, thumb_png=None, marquee_png=None))
    assert (system / "images" / f"{STEM}-image.jpg").read_bytes() == b"" and r.written == []
    assert game(system)["image"] == "./media/old.png"


@pytest.mark.parametrize("tagline", ["<image>./media/gone.png</image>", "<image>./media/empty.png</image>", "<image>  </image>", ""])
def test_canonical_file_is_linked_when_the_tag_is_empty_dangling_or_points_to_an_empty_file(tmp_path, tagline):
    system, backups, journal = setup(tmp_path)
    (system / "gamelist.xml").write_text(SAMPLE.replace("<name>", tagline + "<name>"))
    (system / "media").mkdir()
    (system / "media" / "empty.png").write_bytes(b"")
    (system / "images").mkdir()
    (system / "images" / f"{STEM}-image.jpg").write_bytes(b"OLD")
    r = make_saver(tmp_path).save(req(system, thumb_png=None, marquee_png=None))
    assert game(system)["image"] == f"./images/{STEM}-image.jpg" and r.linked == ["image"]
    assert (system / "images" / f"{STEM}-image.jpg").read_bytes() == b"OLD"


def test_replace_overwrites_the_canonical_file_and_points_the_tag_at_it_leaving_other_art_alone(tmp_path):
    system, backups, journal = setup(tmp_path)
    (system / "gamelist.xml").write_text(SAMPLE.replace("<name>", "<image>./media/old.png</image><name>"))
    (system / "media").mkdir()
    (system / "media" / "old.png").write_bytes(b"USER ART")
    (system / "images").mkdir()
    canon = system / "images" / f"{STEM}-image.jpg"
    canon.write_bytes(b"OLD CANON")
    s = make_saver(tmp_path)
    r = s.save(req(system, thumb_png=None, marquee_png=None, replace={"image"}))
    assert canon.read_bytes() == b"JPG" and game(system)["image"] == f"./images/{STEM}-image.jpg"
    assert (system / "media" / "old.png").read_bytes() == b"USER ART"
    assert [p.read_bytes() for p in (r.journal / "replaced").iterdir()] == [b"OLD CANON"]
    s.undo_last()
    assert canon.read_bytes() == b"OLD CANON" and game(system)["image"] == "./media/old.png"


# ------------------------------------------------------------------ journal pruning

def _journal_entries(journal):
    return sorted(p.name for p in journal.iterdir() if p.is_dir() and p.name.isdigit())


def test_journal_keeps_only_the_newest_entries(tmp_path):
    system, backups, journal = setup(tmp_path)
    s = make_saver(tmp_path, journal_keep=3)
    for i in range(6):
        s.save(req(system, rom_path=f"./G{i}.nsp"))
    assert len(_journal_entries(journal)) == 3
    assert [s.undo_last()["rom_path"] for _ in range(3)] == ["./G5.nsp", "./G4.nsp", "./G3.nsp"]
    assert s.undo_last() is None


def test_undone_entries_are_pruned_too_and_orphan_folders_are_never_deleted(tmp_path):
    system, backups, journal = setup(tmp_path)
    orphan = journal / "0000000000000001"
    orphan.mkdir(parents=True)
    (orphan / "replaced").mkdir()
    (orphan / "replaced" / "x").write_bytes(b"someone's old art")
    s = make_saver(tmp_path, journal_keep=2)
    for i in range(5):
        s.save(req(system, rom_path=f"./G{i}.nsp"))
    for _ in range(2):
        s.undo_last()
    for i in range(5):
        s.save(req(system, rom_path=f"./H{i}.nsp"))
    done = [p for p in (journal / "undone").iterdir()]
    assert len(done) <= 2
    assert (orphan / "replaced" / "x").exists()                          # no journal.json: not ours to delete
    assert s.undo_last()["rom_path"] == "./H4.nsp"


def test_pruning_never_removes_the_latest_entry_and_failures_never_fail_the_save(tmp_path, monkeypatch):
    system, backups, journal = setup(tmp_path)
    s = make_saver(tmp_path, journal_keep=1)
    s.save(req(system, rom_path="./A.nsp"))

    def boom(*a, **k):
        raise OSError(errno.EACCES, "nope")

    monkeypatch.setattr(saver_mod.shutil, "rmtree", boom)
    r = s.save(req(system, rom_path="./B.nsp"))                           # pruning A fails; the save is still a success
    assert r.written and len(_journal_entries(journal)) == 2
    monkeypatch.undo()
    assert s.undo_last()["rom_path"] == "./B.nsp"


def test_journal_keep_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        make_saver(tmp_path, journal_keep=0)
    assert saver_mod.JOURNAL_KEEP == 20


# ------------------------------------------------------------------ temp files and confinement at write time

def test_planted_tmp_symlinks_are_not_followed(tmp_path):
    system, backups, journal = setup(tmp_path)
    (system / "images").mkdir()
    victims = {}
    for name in (f"images/{STEM}-image.jpg.tmp", f"images/{STEM}-thumb.png.tmp", "gamelist.xml.tmp"):
        v = tmp_path / ("victim-" + str(len(victims)))
        v.write_bytes(b"precious")
        (system / name).symlink_to(v)
        victims[name] = v
    # the backup copy's old predictable name, too
    backups.mkdir()
    bv = tmp_path / "victim-backup"
    bv.write_bytes(b"precious")
    (backups / "switch-gamelist-original.xml.tmp").symlink_to(bv)
    r = make_saver(tmp_path).save(req(system))
    assert (system / "images" / f"{STEM}-image.jpg").read_bytes() == b"JPG" and "Scary." in (system / "gamelist.xml").read_text()
    assert all(v.read_bytes() == b"precious" for v in list(victims.values()) + [bv])
    assert r.backup.read_bytes() == SAMPLE.encode()


def test_media_folder_swapped_for_an_outside_symlink_during_the_save_is_caught_at_write_time(tmp_path, monkeypatch):
    system, backups, journal = setup(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real = Saver._make_dirs

    def swapping(folder, txn):
        real(folder, txn)
        if folder.name == "images" and folder.is_dir() and not folder.is_symlink():
            folder.rmdir()
            folder.symlink_to(elsewhere)

    monkeypatch.setattr(Saver, "_make_dirs", staticmethod(swapping))
    with pytest.raises(SaveError, match="(?i)outside"):
        make_saver(tmp_path).save(req(system))
    assert list(elsewhere.iterdir()) == [] and (system / "gamelist.xml").read_text() == SAMPLE


# ------------------------------------------------------------------ file modes

def test_replaced_media_keeps_its_mode_and_undo_restores_it(tmp_path):
    system, backups, journal = setup(tmp_path)
    (system / "images").mkdir()
    old = system / "images" / f"{STEM}-image.jpg"
    old.write_bytes(b"OLD")
    old.chmod(0o640)
    s = make_saver(tmp_path)
    s.save(req(system, replace={"image"}))
    assert old.read_bytes() == b"JPG" and (old.stat().st_mode & 0o777) == 0o640
    old.chmod(0o600)
    s.undo_last()
    assert old.read_bytes() == b"OLD" and (old.stat().st_mode & 0o777) == 0o640


# ------------------------------------------------------------------ journal before the rename, undo failures

class PowerCut(BaseException):
    pass


def test_journal_exists_before_gamelist_is_replaced_and_is_final_afterwards(tmp_path, monkeypatch):
    system, backups, journal = setup(tmp_path)
    seen = {}
    real = Gamelist.save

    def spy(self):
        seen["entries"] = [json_state(p) for p in journal.glob("*/journal.json")]
        return real(self)

    monkeypatch.setattr(Gamelist, "save", spy)
    r = make_saver(tmp_path).save(req(system))
    assert seen["entries"] == ["in-progress"] and json_state(r.journal / "journal.json") == "done"


def json_state(p):
    import json
    return json.loads(p.read_text())["state"]


@pytest.mark.parametrize("cut_at", ["before_rename", "after_rename"])
def test_a_power_cut_leaves_a_journal_that_undo_can_use(tmp_path, monkeypatch, cut_at):
    system, backups, journal = setup(tmp_path)
    real_save = Gamelist.save

    def cut_save(self):
        if cut_at == "before_rename":
            raise PowerCut()
        real_save(self)
        raise PowerCut()

    monkeypatch.setattr(Gamelist, "save", cut_save)
    monkeypatch.setattr(Saver, "_rollback", lambda self, txn: [])        # the process died: no rollback happens
    with pytest.raises(PowerCut):
        make_saver(tmp_path).save(req(system))
    monkeypatch.undo()
    assert (system / "images" / f"{STEM}-image.jpg").exists()
    entry = Saver(None, journal).undo_last()
    assert entry is not None and (system / "gamelist.xml").read_text() == SAMPLE and not (system / "images").exists()


def test_failure_finalising_the_journal_restores_the_gamelist(tmp_path, monkeypatch):
    system, backups, journal = setup(tmp_path)
    before = snapshot(system)
    real = Saver._write_journal
    calls = []

    def second_fails(self, jdir, entry):
        calls.append(entry["state"])
        if entry["state"] == "done":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(self, jdir, entry)

    monkeypatch.setattr(Saver, "_write_journal", second_fails)
    with pytest.raises(SaveError):
        make_saver(tmp_path).save(req(system))
    assert calls == ["media-in-progress", "in-progress", "done"] and snapshot(system) == before


def test_failure_moving_the_journal_entry_away_is_a_plain_error_and_undo_can_be_retried(tmp_path, monkeypatch):
    system, backups, journal = setup(tmp_path)
    s = make_saver(tmp_path)
    s.save(req(system))

    def boom(*a, **k):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(saver_mod.shutil, "move", boom)
    with pytest.raises(SaveError, match="(?i)permission"):
        s.undo_last()
    monkeypatch.undo()
    assert s.undo_last() is not None and (system / "gamelist.xml").read_text() == SAMPLE
# ------------------------------------------------------------------ dry run

def test_dry_run_changes_nothing_in_the_real_folders(tmp_path):
    system, backups, journal = setup(tmp_path)
    (system / "images").mkdir()
    (system / "images" / f"{STEM}-thumb.png").write_bytes(b"OLD")
    before = snapshot(tmp_path)
    dry = tmp_path / "dry"
    s = Saver(backups, journal, dry_run_dir=dry, es_running=lambda: False)
    r = s.save(req(system, replace={"thumbnail"}))
    after = {k: v for k, v in snapshot(tmp_path).items() if not k.startswith(str(dry))}
    assert after == before and r.dry_run is True
    assert (dry / "switch" / "images" / f"{STEM}-image.jpg").read_bytes() == b"JPG"
    assert (dry / "switch" / "images" / f"{STEM}-thumb.png").read_bytes() == b"THUMB"
    assert Gamelist(dry / "switch" / "gamelist.xml").get("./Tidepool Tactics.nsp")["desc"] == "Scary."
    assert Gamelist(dry / "switch" / "gamelist.xml").get("./Tidepool Tactics.nsp")["playcount"] == "3"
    assert all(str(p).startswith(str(dry)) for p in r.written + r.kept) and r.backup.is_relative_to(dry)
    assert s.undo_last()["rom_path"] == "./Tidepool Tactics.nsp"      # undo works inside the sandbox too
    assert (dry / "switch" / "gamelist.xml").read_text() == SAMPLE
    assert (dry / "switch" / "images" / f"{STEM}-thumb.png").read_bytes() == b"OLD"


def test_dry_run_folder_inside_the_system_folder_is_refused(tmp_path):
    system, backups, journal = setup(tmp_path)
    before = snapshot(tmp_path)
    with pytest.raises(SaveError, match="(?i)dry"):
        Saver(backups, journal, dry_run_dir=system / "dry", es_running=lambda: False).save(req(system))
    assert snapshot(tmp_path) == before


# ------------------------------------------------------------------ threads

def test_concurrent_saves_are_serialised_and_none_is_lost(tmp_path):
    system, backups, journal = setup(tmp_path)
    s = make_saver(tmp_path)
    errors = []

    def work(i):
        try:
            s.save(req(system, rom_path=f"./G{i}.nsp", fields={"desc": f"d{i}"}))
        except BaseException as e:      # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    gl = Gamelist(system / "gamelist.xml")
    assert all(gl.get(f"./G{i}.nsp")["desc"] == f"d{i}" for i in range(12))
    assert len(list(backups.iterdir())) == 1
    assert len([p for p in journal.iterdir() if p.is_dir()]) == 12
    for _ in range(12):
        assert s.undo_last() is not None
    assert (system / "gamelist.xml").read_text() == SAMPLE


def test_savers_do_not_share_state(tmp_path):
    system, backups, journal = setup(tmp_path)
    a, b = make_saver(tmp_path), make_saver(tmp_path)
    assert a.save(req(system)).backup_taken
    assert not a.save(req(system, rom_path="./X.nsp")).backup_taken
    assert b.save(req(system, rom_path="./Y.nsp")).backup_taken is True     # b has no session yet: it takes its own


# ------------------------------------------------------------------ NTFS / FUSE drives, long names

def _boom(code):
    def raiser(*a, **k):
        raise OSError(code, os.strerror(code))
    return raiser


@pytest.mark.parametrize("code", [errno.EPERM, errno.EINVAL, errno.ENOTSUP, errno.ENOSYS])
@pytest.mark.parametrize("what", ["copymode", "chmod", "fsync"])
def test_save_replace_and_undo_work_on_filesystems_that_refuse_chmod_and_fsync(tmp_path, monkeypatch, code, what):
    system, backups, journal = setup(tmp_path)
    (system / "images").mkdir()
    old = system / "images" / f"{STEM}-image.jpg"
    old.write_bytes(b"OLD")
    old.chmod(0o640)
    monkeypatch.setattr({"copymode": saver_mod.shutil, "chmod": saver_mod.os, "fsync": saver_mod.os}[what], what, _boom(code))
    s = make_saver(tmp_path)
    r = s.save(req(system, replace={"image"}))
    assert old.read_bytes() == b"JPG" and r.replaced == [str(old)]
    assert s.undo_last() is not None
    assert old.read_bytes() == b"OLD" and (system / "gamelist.xml").read_text() == SAMPLE


def test_fsync_failing_with_a_real_error_at_any_point_rolls_everything_back(tmp_path, monkeypatch):
    real = os.fsync
    calls = []

    def run(root, fail_at):
        root.mkdir()
        system, backups, journal = setup(root)
        (system / "images").mkdir()
        (system / "images" / f"{STEM}-thumb.png").write_bytes(b"OLD")
        video = root / "clip.mp4"
        video.write_bytes(b"MP4")
        before = snapshot(system)
        calls.clear()

        def counting(fd):
            calls.append(1)
            if len(calls) == fail_at:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(fd)

        monkeypatch.setattr(os, "fsync", counting)
        s = Saver(backups, journal, es_running=lambda: False)
        try:
            s.save(req(system, video_mp4=video, replace={"thumbnail"}))
            outcome = "saved"
        except SaveError as e:
            outcome = str(e)
        monkeypatch.setattr(os, "fsync", real)
        return outcome, snapshot(system), before, journal

    outcome, *_ = run(tmp_path / "count", 0)
    assert outcome == "saved"
    total = len(calls)
    assert total >= 8
    for k in range(1, total + 1):
        outcome, after, before, journal = run(tmp_path / f"k{k}", k)
        assert "disk is full" in outcome.lower(), (k, outcome)
        assert after == before, k
        assert not list(journal.rglob("journal.json")), k


def test_a_bare_eperm_from_the_target_is_reported_as_no_permission(tmp_path, monkeypatch):
    system, backups, journal = setup(tmp_path)
    before = snapshot(system)
    real = os.replace

    def deny(src, dst, *a, **k):
        if str(dst).endswith("-image.jpg"):
            raise OSError(errno.EPERM, "Operation not permitted")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", deny)
    with pytest.raises(SaveError, match="(?i)no permission"):
        make_saver(tmp_path).save(req(system))
    assert snapshot(system) == before


@pytest.mark.parametrize("stem", ["A" * 240, "游" * 81], ids=["ascii-250-bytes", "cjk-255-bytes"])
def test_media_with_names_near_the_255_byte_limit_can_be_saved_replaced_and_undone(tmp_path, stem):
    system, backups, journal = setup(tmp_path)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"MP4")
    rom = f"./{stem}.nsp"
    s = make_saver(tmp_path)
    s.save(req(system, rom_path=rom, video_mp4=video))
    image = system / "images" / f"{stem}-image.jpg"
    assert image.read_bytes() == b"JPG" and (system / "images" / f"{stem}-marquee.png").exists()
    s.save(req(system, rom_path=rom, image_jpg=b"NEWER", replace={"image"}))
    assert image.read_bytes() == b"NEWER"
    s.undo_last()
    assert image.read_bytes() == b"JPG"
    s.undo_last()
    assert not (system / "images").exists() and (system / "gamelist.xml").read_text() == SAMPLE


# ------------------------------------------------------------------ dry run sees the same art as the real run

def test_dry_run_reports_the_same_as_the_real_run_for_art_named_by_a_tag(tmp_path):
    outs = {}
    for label in ("real", "dry"):
        root = tmp_path / label
        root.mkdir()
        system, backups, journal = setup(root)
        (system / "gamelist.xml").write_text(SAMPLE.replace("<name>", "<image>./media/mine.png</image><name>"))
        (system / "media").mkdir()
        (system / "media" / "mine.png").write_bytes(b"MINE")
        kw = dict(dry_run_dir=root / "dry") if label == "dry" else {}
        r = Saver(backups, journal, es_running=lambda: False, **kw).save(req(system, thumb_png=None))
        base = (root / "dry" / "switch") if label == "dry" else system
        outs[label] = ([os.path.relpath(p, base) for p in r.kept], sorted(os.path.relpath(p, base) for p in r.written),
                       r.linked, Gamelist(base / "gamelist.xml").get("./Tidepool Tactics.nsp")["image"])
    assert outs["dry"] == outs["real"]
    assert outs["dry"][0] == ["media/mine.png"] and outs["dry"][3] == "./media/mine.png"
    assert (tmp_path / "dry" / "dry" / "switch" / "media" / "mine.png").read_bytes() == b"MINE"
    assert not (tmp_path / "dry" / "dry" / "switch" / "images" / f"{STEM}-image.jpg").exists()


# ------------------------------------------------------------------ a crash in the middle of the media writes

@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("crash_at", [0, 1, 2, 3])
def test_a_crash_mid_media_leaves_a_journal_that_undoes_exactly(tmp_path, monkeypatch, crash_at, when):
    system, backups, journal = setup(tmp_path)
    (system / "images").mkdir()
    old = system / "images" / f"{STEM}-thumb.png"
    old.write_bytes(b"OLD THUMB")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"MP4")
    count = [0]
    real_write, real_copy = saver_mod.write_atomic, saver_mod._atomic_copy

    def maybe_crash(path, call):
        if Path(path).parent.name not in ("images", "videos") or Path(path).parent.parent != system:
            return call()
        if count[0] == crash_at and when == "before":
            raise PowerCut()
        out = call()
        if count[0] == crash_at:
            raise PowerCut()
        count[0] += 1
        return out

    monkeypatch.setattr(saver_mod, "write_atomic", lambda path, data: maybe_crash(path, lambda: real_write(path, data)))
    monkeypatch.setattr(saver_mod, "_atomic_copy",
                        lambda src, dst, mode=None: maybe_crash(dst, lambda: real_copy(src, dst, mode=mode)))
    monkeypatch.setattr(Saver, "_rollback", lambda self, txn: [])           # the process died: nothing is rolled back
    with pytest.raises(PowerCut):
        make_saver(tmp_path).save(req(system, video_mp4=video, replace={"thumbnail"}))
    monkeypatch.undo()
    gl = system / "gamelist.xml"
    gl.write_text(gl.read_text().replace("<playcount>3", "<playcount>4"))     # ES rewrote it meanwhile: not ours to undo
    changed = gl.read_bytes()
    entry = Saver(None, journal).undo_last()
    assert entry is not None
    assert old.read_bytes() == b"OLD THUMB"
    assert not (system / "images" / f"{STEM}-image.jpg").exists() and not (system / "images" / f"{STEM}-marquee.png").exists()
    assert not (system / "videos").exists()
    assert gl.read_bytes() == changed


# ------------------------------------------------------------------ orphan journal folders

def test_orphan_journal_folders_do_not_count_toward_the_keep_window(tmp_path):
    system, backups, journal = setup(tmp_path)
    s = make_saver(tmp_path, journal_keep=2)
    for i in range(4):
        s.save(req(system, rom_path=f"./G{i}.nsp"))
    for n in range(5):
        (journal / f"9999999999999{n:03d}").mkdir()                      # crashed saves: no journal.json
    s.save(req(system, rom_path="./G4.nsp"))
    assert [s.undo_last()["rom_path"] for _ in range(2)] == ["./G4.nsp", "./G3.nsp"]
    assert s.undo_last() is None
    assert len([p for p in journal.iterdir() if p.name.startswith("9999")]) == 5


# ------------------------------------------------------------------ a journal that moved (the program was renamed)

def test_undo_still_works_after_the_journal_folder_was_moved(tmp_path):
    """journal.json records the absolute path of the saved copy of every replaced file. When the data folder is renamed
    (the installer moves the journal of the old name into the new data folder) those paths are stale, so undo must find
    the copy next to journal.json instead of reporting it as gone."""
    system, backups, journal = setup(tmp_path)
    original = (system / "gamelist.xml").read_bytes()
    (system / "images").mkdir()
    old = system / "images" / f"{STEM}-image.jpg"
    old.write_bytes(b"OLD")
    save_game(req(system, replace={"image"}), backups, journal)
    assert old.read_bytes() == b"JPG"
    moved = tmp_path / "renamed-data" / "journal"
    moved.parent.mkdir()
    journal.rename(moved)
    recorded = next(moved.glob("*/journal.json")).read_text()
    assert str(journal) in recorded and not journal.exists()           # the record really points at the old place
    undone = undo_last(moved)
    assert undone is not None
    assert (system / "gamelist.xml").read_bytes() == original
    assert old.read_bytes() == b"OLD"
    assert not (system / "images" / f"{STEM}-thumb.png").exists()
    assert (moved / "undone").is_dir() and undo_last(moved) is None


def test_undo_reports_a_saved_copy_that_is_gone_from_both_places(tmp_path):
    system, backups, journal = setup(tmp_path)
    (system / "images").mkdir()
    (system / "images" / f"{STEM}-image.jpg").write_bytes(b"OLD")
    save_game(req(system, replace={"image"}), backups, journal)
    moved = tmp_path / "renamed-data" / "journal"
    moved.parent.mkdir()
    journal.rename(moved)
    for copy in moved.glob("*/replaced/*"):
        copy.unlink()
    with pytest.raises(SaveError, match="saved copy of the old file"):
        undo_last(moved)
    assert (system / "images" / f"{STEM}-image.jpg").read_bytes() == b"JPG"       # nothing was touched


def test_undo_does_not_restore_a_moved_copy_that_is_not_the_saved_file(tmp_path):
    """A stale record falls back to the copy beside journal.json only if it is byte for byte what was saved."""
    system, backups, journal = setup(tmp_path)
    (system / "images").mkdir()
    old = system / "images" / f"{STEM}-image.jpg"
    old.write_bytes(b"OLD")
    save_game(req(system, replace={"image"}), backups, journal)
    moved = tmp_path / "renamed-data" / "journal"
    moved.parent.mkdir()
    journal.rename(moved)
    for copy in moved.glob("*/replaced/*"):
        copy.write_bytes(b"SOMETHING ELSE")
    with pytest.raises(SaveError, match="saved copy of the old file"):
        undo_last(moved)
    assert old.read_bytes() == b"JPG"
