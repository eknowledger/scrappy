import json
import os
import re
import stat

import pytest

import scrappy.config as config_mod
from scrappy.config import DEFAULTS, ConfigError, data_dir, load_config, save_settings
from scrappy.providers.base import ProviderError, describe_error

SECRETISH = re.compile(r"key|token|secret|passw|credential", re.I)


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_when_there_is_no_file(tmp_path):
    cfg = load_config(path=tmp_path / "nope.json")
    assert dict(cfg) == DEFAULTS and cfg.warnings == []


def test_defaults_never_hold_secrets():
    assert not [k for k in DEFAULTS if SECRETISH.search(k)]


def test_values_from_the_file_win_and_unknown_keys_are_kept(tmp_path):
    p = write(tmp_path / "c.json", json.dumps({"max_video_seconds": 60, "default_video_seconds": 30, "future_setting": {"a": [1]}}))
    cfg = load_config(path=p)
    assert cfg["max_video_seconds"] == 60 and cfg["default_video_seconds"] == 30
    assert cfg["future_setting"] == {"a": [1]} and cfg["search_timeout"] == DEFAULTS["search_timeout"]


@pytest.mark.parametrize("content", [
    "{not json", "", "[1, 2]", "42", "null", '"text"', "{" * 5000,
])
def test_corrupt_or_wrong_shaped_file_gives_defaults_and_a_warning(tmp_path, content):
    cfg = load_config(path=write(tmp_path / "c.json", content))
    assert dict(cfg) == DEFAULTS or content == ""        # an empty file is simply "nothing configured"
    assert content == "" or cfg.warnings


def test_non_utf8_file_gives_defaults(tmp_path):
    p = tmp_path / "c.json"
    p.write_bytes(b'{"roms_root": "\xff\xfe"}')
    cfg = load_config(path=p)
    assert dict(cfg) == DEFAULTS and cfg.warnings


def test_unreadable_file_and_directory_give_defaults(tmp_path):
    folder = tmp_path / "c.json"
    folder.mkdir()
    cfg = load_config(path=folder)
    assert dict(cfg) == DEFAULTS and cfg.warnings


def test_huge_file_is_not_read(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"x": "' + "a" * 2_000_000 + '"}')
    cfg = load_config(path=p)
    assert dict(cfg) == DEFAULTS and cfg.warnings


@pytest.mark.parametrize("key, bad", [
    ("max_video_seconds", "lots"), ("max_video_seconds", True), ("max_video_seconds", -5), ("max_video_seconds", 0),
    ("max_video_seconds", 100000), ("max_video_seconds", float("nan")), ("max_video_seconds", None),
    ("default_video_seconds", [90]), ("search_timeout", 0), ("search_timeout", "20"),
    ("video_bitrate", "-f x"), ("video_bitrate", "1500k; rm"), ("video_bitrate", 1500), ("video_bitrate", ""),
    ("roms_root", ""), ("roms_root", 5), ("data_dir", None), ("roms_root", "bad\x00path"),
    ("dry_run_dir", 5), ("youtube_search_timeout", "x"),
    ("roms_root", "roms"), ("roms_root", "./roms"), ("roms_root", "~/roms"), ("data_dir", "relative/dir"),
    ("data_dir", "~"), ("dry_run_dir", "sandbox"), ("dry_run_dir", "~/x"),
    ("intro_seen", "yes"), ("intro_seen", 1), ("intro_seen", None),
])
def test_invalid_value_falls_back_to_that_keys_default_only(tmp_path, key, bad):
    text = json.dumps({key: bad, "search_timeout": 33} if key != "search_timeout" else {key: bad, "max_video_seconds": 50},
                      allow_nan=True)
    cfg = load_config(path=write(tmp_path / "c.json", text))
    assert cfg[key] == DEFAULTS[key]
    assert any(key in w for w in cfg.warnings)
    other = "search_timeout" if key != "search_timeout" else "max_video_seconds"
    assert cfg[other] == (33 if other == "search_timeout" else 50)       # the rest of the file still applies


def test_default_clip_length_never_exceeds_the_maximum(tmp_path):
    cfg = load_config(path=write(tmp_path / "c.json", json.dumps({"max_video_seconds": 40, "default_video_seconds": 90})))
    assert cfg["default_video_seconds"] == 40 and cfg["max_video_seconds"] == 40


def test_valid_bitrates_and_numbers_are_accepted(tmp_path):
    cfg = load_config(path=write(tmp_path / "c.json", json.dumps(
        {"video_bitrate": "2M", "search_timeout": 7.5, "dry_run_dir": "/tmp/x", "max_video_seconds": 200})))
    assert cfg["video_bitrate"] == "2M" and cfg["search_timeout"] == 7.5 and cfg["dry_run_dir"] == "/tmp/x"
    assert not cfg.warnings


def test_overrides_win_over_the_file_and_must_be_valid(tmp_path):
    p = write(tmp_path / "c.json", json.dumps({"roms_root": "/from/file"}))
    assert load_config(path=p, overrides={"roms_root": "/from/test"})["roms_root"] == "/from/test"
    for bad in ({"max_video_seconds": "x"}, {"roms_root": "relative"}):
        with pytest.raises(ConfigError) as caught:
            load_config(path=p, overrides=bad)
        assert isinstance(caught.value, ValueError) and not describe_error(caught.value).startswith("ValueError")


def test_config_file_is_looked_for_in_the_overridden_data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    write(d / "config.json", json.dumps({"search_timeout": 9}))
    assert load_config(overrides={"data_dir": str(d)})["search_timeout"] == 9



def test_secret_looking_keys_are_dropped_from_the_loaded_config_but_stay_on_disk(tmp_path):
    p = write(tmp_path / "c.json", json.dumps({"steam_api_key": "abc", "Auth_Token": "t", "search_timeout": 7}))
    cfg = load_config(path=p)
    assert "steam_api_key" not in cfg and "Auth_Token" not in cfg and cfg["search_timeout"] == 7
    assert any("steam_api_key" in w and "secret" in w.lower() for w in cfg.warnings)
    assert "abc" not in repr(dict(cfg)) and not any("abc" in w for w in cfg.warnings)
    save_settings({"search_timeout": 9}, path=p)                       # saving other settings must not lose them
    assert json.loads(p.read_text()) == {"steam_api_key": "abc", "Auth_Token": "t", "search_timeout": 9}


def test_secrets_cannot_be_saved_into_config_json(tmp_path):
    p = tmp_path / "c.json"
    with pytest.raises(ConfigError, match="secret"):
        save_settings({"api_key": "abc"}, path=p)
    assert not p.exists()


def test_load_never_creates_or_writes_anything(tmp_path):
    load_config(path=tmp_path / "sub" / "c.json")
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------- saving

def test_save_creates_the_file_and_keeps_unknown_keys(tmp_path):
    p = tmp_path / "data" / "c.json"
    save_settings({"search_timeout": 12}, path=p)
    assert json.loads(p.read_text()) == {"search_timeout": 12}
    p.write_text(json.dumps({"future": [1, 2], "search_timeout": 5}))
    save_settings({"max_video_seconds": 45}, path=p)
    assert json.loads(p.read_text()) == {"future": [1, 2], "search_timeout": 5, "max_video_seconds": 45}
    assert [f.name for f in p.parent.iterdir()] == ["c.json"]            # no temp files left behind


def test_save_rejects_invalid_values_and_writes_nothing(tmp_path):
    p = write(tmp_path / "c.json", json.dumps({"search_timeout": 5}))
    with pytest.raises(ValueError, match="max_video_seconds"):
        save_settings({"max_video_seconds": "x", "search_timeout": 9}, path=p)
    assert json.loads(p.read_text()) == {"search_timeout": 5}


def test_save_none_removes_a_key(tmp_path):
    p = write(tmp_path / "c.json", json.dumps({"dry_run_dir": "/x", "other": 1}))
    save_settings({"dry_run_dir": None}, path=p)
    assert json.loads(p.read_text()) == {"other": 1}


def test_save_keeps_the_mode_of_an_existing_file(tmp_path):
    p = write(tmp_path / "c.json", "{}")
    os.chmod(p, 0o640)
    save_settings({"search_timeout": 9}, path=p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o640


def test_save_over_a_corrupt_file_keeps_a_copy_of_it(tmp_path):
    p = write(tmp_path / "c.json", "{broken but precious")
    save_settings({"search_timeout": 9}, path=p)
    assert json.loads(p.read_text()) == {"search_timeout": 9}
    assert (tmp_path / "c.json.corrupt").read_text() == "{broken but precious"


def test_save_failure_is_a_plain_provider_error(tmp_path):
    blocker = write(tmp_path / "file", "x")
    with pytest.raises(ProviderError, match="settings"):
        save_settings({"search_timeout": 9}, path=blocker / "c.json")


def test_save_never_writes_defaults_or_overrides_it_was_not_given(tmp_path):
    p = tmp_path / "c.json"
    cfg = load_config(path=p, overrides={"roms_root": "/test/only"})
    save_settings({"search_timeout": 9}, path=p)
    assert json.loads(p.read_text()) == {"search_timeout": 9} and cfg["roms_root"] == "/test/only"


# ---------------------------------------------------------------- work folders

def test_data_dir_creates_named_subfolders(tmp_path):
    cfg = {"data_dir": str(tmp_path / "d")}
    assert data_dir(cfg, "cache") == tmp_path / "d" / "cache" and (tmp_path / "d" / "cache").is_dir()


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "../x", "a\\b", "/abs", None, 3])
def test_data_dir_refuses_names_that_could_leave_the_folder(tmp_path, name):
    with pytest.raises(ConfigError) as caught:
        data_dir({"data_dir": str(tmp_path)}, name)
    assert isinstance(caught.value, ValueError) and not describe_error(caught.value).startswith("ValueError")


def test_data_dir_failure_is_a_plain_provider_error(tmp_path):
    blocker = write(tmp_path / "file", "x")
    with pytest.raises(ProviderError, match="work folder"):
        data_dir({"data_dir": str(blocker)}, "cache")


def test_config_write_goes_through_atomic_replace(tmp_path, monkeypatch):
    calls = []
    real = config_mod.atomic_replace

    def spy(target, fill, **kw):
        calls.append(target)
        return real(target, fill, **kw)

    monkeypatch.setattr(config_mod, "atomic_replace", spy)
    p = tmp_path / "c.json"
    save_settings({"search_timeout": 9}, path=p)
    assert calls == [p] and json.loads(p.read_text()) == {"search_timeout": 9}


@pytest.mark.parametrize("updates", [{"future": object()}, {"future": {1, 2}}, {5: "x"}, {"search_timeout": [1]},
                                     {"future": float("nan")}, {None: 1}])
def test_save_with_unsuitable_values_is_a_plain_error_not_a_type_error(tmp_path, updates):
    p = write(tmp_path / "c.json", json.dumps({"search_timeout": 5}))
    with pytest.raises(ConfigError) as caught:
        save_settings(updates, path=p)
    text = describe_error(caught.value)
    assert not text.startswith(("TypeError", "ValueError")) and json.loads(p.read_text()) == {"search_timeout": 5}


def test_intro_seen_is_off_by_default_and_a_file_can_turn_it_on(tmp_path):
    assert DEFAULTS["intro_seen"] is False and load_config(path=tmp_path / "missing.json")["intro_seen"] is False
    cfg = load_config(path=write(tmp_path / "c.json", json.dumps({"intro_seen": True, "future_setting": 1})))
    assert cfg["intro_seen"] is True and cfg["future_setting"] == 1 and not cfg.warnings


def test_remembering_the_tip_keeps_every_other_key_of_the_file(tmp_path):
    p = write(tmp_path / "c.json", json.dumps({"search_timeout": 33, "future_setting": {"a": [1]}}))
    save_settings({"intro_seen": True}, path=p)
    assert json.loads(p.read_text()) == {"search_timeout": 33, "future_setting": {"a": [1]}, "intro_seen": True}
    with pytest.raises(ConfigError):
        save_settings({"intro_seen": "yes"}, path=p)
