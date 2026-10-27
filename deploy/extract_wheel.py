#!/usr/bin/env python3
"""Unpacks the vendored Pillow wheel on the Batocera. Standard library only (no pip, no unzip on the box).

    python3 extract_wheel.py WHEEL OUTDIR EXPECTED_SHA256 MANIFEST

* the wheel's sha256 must equal EXPECTED_SHA256 (the pin in deploy/vendor.txt), otherwise nothing is extracted;
* every member name is validated first: absolute paths, '..', backslashes, drive letters, NUL bytes and symlinks make the
  whole wheel be refused (zip-slip), nothing is written;
* only the PIL package, pillow.libs and the pillow-*.dist-info folder are extracted; every other member is ignored;
* OUTDIR must not exist yet (the installer extracts into a fresh staging folder);
* MANIFEST receives "sha256  vendor/<relative path>" lines for every extracted file (checkable with sha256sum -c from
  the app folder after OUTDIR has been moved to <app folder>/vendor).
Exit code 0 = extracted, 1 = refused (the reason is printed).
"""
import hashlib
import os
import re
import stat
import sys
import zipfile

ALLOWED_TOP = re.compile(r"^(PIL|pillow\.libs|pillow-[0-9][A-Za-z0-9.!+_-]*\.dist-info)/")
MAX_MEMBERS = 5000
MAX_TOTAL_BYTES = 120 * 1024 * 1024        # the real wheel unpacks to about 25 MB
MANIFEST_PREFIX = "vendor/"


class WheelError(Exception):
    """The wheel is refused; the message says why."""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_member_name(name):
    if not name or "\x00" in name:
        raise WheelError("unsafe member name (empty or NUL): %r" % name)
    if name.startswith("/") or "\\" in name or re.match(r"^[A-Za-z]:", name):
        raise WheelError("unsafe member path (absolute or backslash): %r" % name)
    if any(part == ".." for part in name.split("/")):
        raise WheelError("unsafe member path (..): %r" % name)


def extract(wheel, outdir, expected_sha, manifest_path):
    """Returns the list of relative file names written. Raises WheelError."""
    got = sha256_file(wheel)
    if got != expected_sha.lower():
        raise WheelError("wheel sha256 %s does not match the pin %s" % (got, expected_sha))
    if os.path.lexists(outdir):
        raise WheelError("%s already exists" % outdir)
    try:
        zf = zipfile.ZipFile(wheel)
    except (zipfile.BadZipFile, OSError) as exc:
        raise WheelError("not a readable zip file: %s" % exc)
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise WheelError("too many members (%d)" % len(infos))
        wanted = []
        for info in infos:
            check_member_name(info.filename)                 # ALL names are validated, also the ones we ignore
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise WheelError("symlink member refused: %r" % info.filename)
            if ALLOWED_TOP.match(info.filename) and not info.filename.endswith("/"):
                wanted.append(info)
        if not any(i.filename.startswith("PIL/") for i in wanted):
            raise WheelError("the wheel has no PIL package")
        if sum(i.file_size for i in wanted) > MAX_TOTAL_BYTES:
            raise WheelError("unpacked size is unreasonably large")
        root = os.path.realpath(outdir)
        os.makedirs(outdir)
        written = []
        for info in sorted(wanted, key=lambda i: i.filename):
            target = os.path.realpath(os.path.join(outdir, *info.filename.split("/")))
            if not target.startswith(root + os.sep):
                raise WheelError("member escapes the target folder: %r" % info.filename)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                remaining = MAX_TOTAL_BYTES
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise WheelError("member %r is larger than it claims" % info.filename)
                    dst.write(chunk)
            is_lib = ".so" in os.path.basename(info.filename) or info.filename.endswith(".dylib")
            os.chmod(target, 0o755 if is_lib else 0o644)
            written.append(info.filename)
    with open(manifest_path, "w") as m:
        for rel in written:
            m.write("%s  %s%s\n" % (sha256_file(os.path.join(outdir, *rel.split("/"))), MANIFEST_PREFIX, rel))
    return written


def main(argv):
    if len(argv) != 5:
        print("usage: extract_wheel.py WHEEL OUTDIR EXPECTED_SHA256 MANIFEST", file=sys.stderr)
        return 1
    try:
        written = extract(argv[1], argv[2], argv[3], argv[4])
    except WheelError as exc:
        print("wheel refused: %s" % exc, file=sys.stderr)
        return 1
    print("vendor: %d files extracted from %s" % (len(written), os.path.basename(argv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
