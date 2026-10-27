"""Saving and undoing while EmulationStation is running: flush first, reload after, verify, re-apply at most once."""
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scrappy.controller import SaveOutcome
from scrappy.saver import UndoRefused

from .test_controller import JPG, LOGO, SAMPLE_ROM, THUMB, env, make  # noqa: F401 - env is a fixture


class FakeES:
    """EmulationStation: `reload()` is the call to its reload address. The first call is the 'flush'; later calls can make it
    rewrite gamelist.xml from a stale in-memory copy (what the real one did 36 s after our save)."""

    def __init__(self, gamelist: Path, clobbers: int = 0, up: bool = True, reload_ok: bool = True):
        self.gl, self.clobbers_left, self.up, self.reload_ok = gamelist, clobbers, up, reload_ok
        self.stale, self.calls = None, []

    def arm(self, clobbers: int, stale=None):
        self.clobbers_left, self.calls, self.stale = clobbers, [], stale

    def running(self):
        return self.up

    def reload(self):
        data = self.gl.read_bytes()
        self.calls.append(b"Tidepool" in data)                      # did the list carry our entry when we were asked?
        if not self.reload_ok:
            return False
        if len(self.calls) == 1:
            self.stale = self.stale if self.stale is not None else data
        elif self.clobbers_left > 0:
            self.clobbers_left -= 1
            self.gl.write_bytes(self.stale)                      # ES writes its older in-memory list over ours
        return True


def build(env, **es_kw):
    cfg, _ = env
    gl = Path(cfg["roms_root"]) / "switch" / "gamelist.xml"
    fake, sleeps = FakeES(gl, **es_kw), []
    ctl = make(env, es_running=fake.running, es_reload=fake.reload, sleep=sleeps.append)
    return ctl, fake, sleeps, gl


def request(ctl):
    return ctl.build_request("switch", "./" + SAMPLE_ROM, {"name": "Tidepool Tactics: Remastered", "desc": "Scary."},
                             image_jpg=JPG, thumb_png=THUMB, marquee_png=LOGO)


def entry(gl: Path):
    for g in ET.parse(gl).getroot().findall("game"):
        if "Tidepool" in (g.findtext("path") or ""):
            return {c.tag: (c.text or "") for c in g}
    return {}


def has_our_tags(gl: Path) -> bool:
    e = entry(gl)
    return bool(e.get("name") and e.get("image") and e.get("thumbnail") and e.get("marquee"))


def test_es_not_running_means_no_calls_and_no_waiting(env):
    ctl, fake, sleeps, gl = build(env, up=False)
    out = ctl.save(request(ctl))
    assert isinstance(out, SaveOutcome) and has_our_tags(gl)
    assert fake.calls == [] and sleeps == []
    assert not out.es_running and not out.es_reloaded and not out.es_reapplied and out.es_warning == ""


def test_running_es_is_flushed_before_and_reloaded_after_the_save(env):
    ctl, fake, sleeps, gl = build(env)
    out = ctl.save(request(ctl))
    assert fake.calls == [False, True]                           # flush before (no entry yet), reload after (our entry on disk)
    assert sleeps == [2.0, 3.0]
    assert out.es_running and out.es_reloaded and not out.es_reapplied and out.es_warning == ""
    assert has_our_tags(gl)


def test_clobbered_once_is_applied_again_and_reported(env):
    ctl, fake, sleeps, gl = build(env, clobbers=1)
    out = ctl.save(request(ctl))
    assert has_our_tags(gl)                                      # the tags are back
    assert len(fake.calls) == 3                                  # flush, reload (clobbered), reload after the re-apply: never more
    assert out.es_reapplied and out.es_reloaded and out.es_warning == ""
    assert sleeps == [2.0, 3.0, 3.0]
    assert entry(gl)["desc"] == "Scary."


def test_clobbered_twice_gives_a_plain_warning_and_stops(env):
    ctl, fake, sleeps, gl = build(env, clobbers=2)
    out = ctl.save(request(ctl))
    assert len(fake.calls) == 3                                  # exactly one re-apply, no loop
    assert not out.es_reapplied and "Quit EmulationStation" in out.es_warning and "press Save again" in out.es_warning
    assert not has_our_tags(gl)
    assert (Path(env[0]["roms_root"]) / "switch" / "images" / f"{Path(SAMPLE_ROM).stem}-image.jpg").exists()   # the files are in place


def test_reload_that_fails_keeps_the_manual_hint_case(env):
    ctl, fake, sleeps, gl = build(env, reload_ok=False)
    out = ctl.save(request(ctl))
    assert out.es_running and not out.es_reloaded and out.es_warning == "" and not out.es_reapplied
    assert has_our_tags(gl) and sleeps == []                     # nothing to wait for when ES could not be reached


def test_es_rewriting_the_list_but_keeping_our_entry_is_not_a_clobber(env):
    ctl, fake, sleeps, gl = build(env)
    real = fake.reload

    def rewrites_with_play_data():
        ok = real()
        if len(fake.calls) == 2:                                 # after the save ES writes its own version: our entry plus more
            gl.write_text(gl.read_text().replace(
                "</gameList>", "<game><path>./Played.nsp</path><playcount>3</playcount></game></gameList>"))
        return ok

    ctl._es_reload = rewrites_with_play_data
    out = ctl.save(request(ctl))
    assert not out.es_reapplied and out.es_warning == "" and len(fake.calls) == 2 and has_our_tags(gl)


def test_dry_run_never_touches_es(env, tmp_path):
    cfg, _ = env
    cfg = dict(cfg, dry_run_dir=str(tmp_path / "sandbox"))
    from scrappy.config import Config
    gl = Path(cfg["roms_root"]) / "switch" / "gamelist.xml"
    fake, sleeps = FakeES(gl), []
    ctl = make((Config(cfg), env[1]), es_running=fake.running, es_reload=fake.reload, sleep=sleeps.append)
    out = ctl.save(request(ctl))
    assert out.dry_run and fake.calls == [] and sleeps == []


def test_a_wait_never_happens_before_the_save_when_es_is_not_running_later(env):
    ctl, fake, sleeps, gl = build(env)
    states = iter([True, False])                                 # running before the save, gone afterwards
    ctl._es_running = lambda: next(states, False)
    ctl.saver._es_running = lambda: False
    out = ctl.save(request(ctl))
    assert fake.calls == [False] and sleeps == [2.0] and not out.es_reloaded


# ---------------------------------------------------------------- undo

def test_undo_flushes_first_reloads_after_and_verifies(env):
    ctl, fake, sleeps, gl = build(env)
    ctl.save(request(ctl))
    fake.arm(0)
    sleeps.clear()
    entry_ = ctl.undo()
    assert entry_["rom_path"] == "./" + SAMPLE_ROM and entry_["es_reloaded"] and not entry_["es_reapplied"] and entry_["es_warning"] == ""
    assert fake.calls == [True, False]                           # flush while our entry was there, reload after it was gone
    assert sleeps == [2.0, 3.0] and not entry(gl)


def test_undo_clobbered_once_is_restored_again(env):
    ctl, fake, sleeps, gl = build(env)
    ctl.save(request(ctl))
    saved = gl.read_bytes()
    fake.arm(1, stale=saved)                                     # ES still holds the list that has our tags
    out = ctl.undo()
    assert out["es_reapplied"] and out["es_warning"] == "" and len(fake.calls) == 3
    assert not entry(gl)                                         # our entry is gone again


def test_undo_clobbered_twice_warns_and_stops(env):
    ctl, fake, sleeps, gl = build(env)
    ctl.save(request(ctl))
    fake.arm(2, stale=gl.read_bytes())
    out = ctl.undo()
    assert len(fake.calls) == 3 and not out["es_reapplied"] and "Quit EmulationStation" in out["es_warning"]


def test_undo_after_a_reapplied_save_undoes_both_entries(env):
    ctl, fake, sleeps, gl = build(env, clobbers=1)
    original = gl.read_bytes()
    out = ctl.save(request(ctl))
    assert out.es_reapplied
    fake.arm(0)
    undone = ctl.undo()
    assert undone["rom_path"] == "./" + SAMPLE_ROM and not entry(gl)
    images = Path(env[0]["roms_root"]) / "switch" / "images"
    assert not list(images.glob("Tidepool*"))                       # the files of the first save are gone too
    assert ET.fromstring(gl.read_bytes()).tag == ET.fromstring(original).tag
    assert ctl.undo() is None                                    # nothing left: the re-apply was part of the same save


def test_undo_with_nothing_to_undo_does_not_bother_es(env):
    ctl, fake, sleeps, gl = build(env)
    assert ctl.undo() is None
    assert fake.calls == []


def test_undo_refused_still_raises_so_the_window_can_ask(env):
    ctl, fake, sleeps, gl = build(env, up=False)
    ctl.save(request(ctl))
    gl.write_text(gl.read_text().replace("</gameList>", "<game><path>./Other.nsp</path></game></gameList>"))
    with pytest.raises(UndoRefused):
        ctl.undo()
    assert ctl.undo(force=True)["forced"]
