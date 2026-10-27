import errno
import os
import shutil

import pytest

from scrappy.atomic import atomic_replace, create_temp, fsync_dir

TOLERATED = [errno.EPERM, errno.EINVAL, errno.ENOTSUP, errno.ENOSYS]
FATAL = [errno.EIO, errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)]


def boom(code):
    def raiser(*a, **k):
        raise OSError(code, os.strerror(code))
    return raiser


def write(target, data=b"new"):
    atomic_replace(target, lambda f: f.write(data))


@pytest.mark.parametrize("code", TOLERATED)
@pytest.mark.parametrize("what", ["copymode", "chmod", "fsync"])
def test_filesystems_without_modes_or_fsync_are_tolerated(tmp_path, monkeypatch, code, what):
    target = tmp_path / "x"
    target.write_bytes(b"old")
    if what == "copymode":
        monkeypatch.setattr(shutil, "copymode", boom(code))
    elif what == "chmod":
        monkeypatch.setattr(os, "chmod", boom(code))
    else:
        monkeypatch.setattr(os, "fsync", boom(code))
    write(target)
    atomic_replace(target, lambda f: f.write(b"newer"), mode=0o600)
    assert target.read_bytes() == b"newer" and os.listdir(tmp_path) == ["x"]


@pytest.mark.parametrize("code", FATAL)
@pytest.mark.parametrize("what", ["copymode", "chmod", "fsync"])
def test_real_io_errors_still_propagate_and_leave_the_old_file(tmp_path, monkeypatch, code, what):
    target = tmp_path / "x"
    target.write_bytes(b"old")
    monkeypatch.setattr({"copymode": shutil, "chmod": os, "fsync": os}[what], what, boom(code))
    with pytest.raises(OSError) as ei:
        atomic_replace(target, lambda f: f.write(b"new"), mode=0o600 if what == "chmod" else None)
    assert ei.value.errno == code
    assert target.read_bytes() == b"old" and os.listdir(tmp_path) == ["x"]


def test_fsync_dir_is_tolerant_but_not_blind(tmp_path, monkeypatch):
    fsync_dir(tmp_path)
    fsync_dir(tmp_path / "does-not-exist")
    monkeypatch.setattr(os, "fsync", boom(errno.EINVAL))
    fsync_dir(tmp_path)
    monkeypatch.setattr(os, "fsync", boom(errno.ENOSPC))
    with pytest.raises(OSError):
        fsync_dir(tmp_path)


def test_temp_name_has_a_fixed_length_whatever_the_target_is_called(tmp_path):
    for name in ("a", "b" * 250, "游" * 85):
        fd, tmp = create_temp(tmp_path / name)
        os.close(fd)
        assert len(tmp.name.encode()) < 40 and tmp.parent == tmp_path and tmp.name.endswith(".tmp")
        tmp.unlink()


@pytest.mark.parametrize("name", ["c" * 255, "游" * 85])
def test_targets_with_maximum_length_names_can_be_replaced(tmp_path, name):
    target = tmp_path / name
    write(target, b"1")
    write(target, b"2")
    assert target.read_bytes() == b"2" and len(os.listdir(tmp_path)) == 1
