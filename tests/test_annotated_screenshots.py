"""The annotated screenshots of docs/USAGE.md.

* The callouts of every picture are data (``tools/annotated_spec.py``); the legend tables of the guide must list exactly those
  numbers and names, so a picture and its legend cannot drift apart. These checks need no Qt.
* The committed pictures are checked (size, number of badges). The tool that makes them is run twice, headless, on the invented
  library: every picture is made, has one badge per callout, no badge touches another or leaves the picture, and the two runs are
  byte for byte the same.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from tests.test_demo_showcase import DENY_LIST, DENY_PHRASES

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import annotated_spec  # noqa: E402  (plain data: no Qt, no Pillow)
from annotated_spec import BY_NAME, SHOTS  # noqa: E402
from scrappy.ui.demo_data import GAMES  # noqa: E402

COMMITTED = ROOT / "docs" / "screenshots" / "annotated"
USAGE = (ROOT / "docs" / "USAGE.md").read_text(encoding="utf-8")
BADGE_FILL = (255, 200, 61)                    # the brand yellow of a badge
MAX_BYTES, MAX_WIDTH, MAX_DOC_WIDTH = 360_000, 1200, 860
PICTURES = ["window-tour", "slot-panel", "trailer-panel", "details-form", "missing-list", "current-vs-new", "review-dialog",
            "tools-menu", "task-dialog-running", "task-dialog-result", "task-dialog-failed", "after-save"]


def count_badges(path: Path) -> int:
    """Number of round badges in a picture: connected areas of the badge colour that are large enough to be one."""
    data = np.asarray(Image.open(path).convert("RGB")).astype(int)
    near = (np.abs(data - np.array(BADGE_FILL)) <= 10).all(axis=2)
    todo = {(int(y), int(x)) for y, x in np.argwhere(near)}
    found = 0
    while todo:
        stack, size = [todo.pop()], 0
        while stack:
            y, x = stack.pop()
            size += 1
            for step in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (y + step[0], x + step[1])
                if nxt in todo:
                    todo.remove(nxt)
                    stack.append(nxt)
        found += size >= 200
    return found


def plain(cell: str) -> str:
    return re.sub(r"[*`]", "", cell).strip()


def legend(name: str):
    """(the <img> tag, [(number, control, what it does)]) of the picture `name` in docs/USAGE.md."""
    tags = [m for m in re.finditer(r"<img [^>]*>", USAGE) if f"screenshots/annotated/{name}.png" in m.group(0)]
    assert len(tags) == 1, f"{name}.png must be embedded exactly once in docs/USAGE.md"
    rest = USAGE[tags[0].end():].splitlines()
    rows, started = [], False
    for line in rest:
        if line.startswith("|"):
            started = True
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells[0] in ("No.", "---") or set(cells[0]) <= {"-"}:
                continue
            rows.append((cells[0], plain(cells[1]), cells[2]))
        elif started:
            break
        elif line.strip() and not line.startswith("<"):
            pytest.fail(f"{name}: the legend table must follow the picture directly, found {line[:60]!r}")
    return tags[0].group(0), rows


# ---------------------------------------------------------------- the callouts and the guide (no Qt)

def test_there_are_twelve_pictures_and_their_callouts_are_numbered_in_order():
    assert [s.name for s in SHOTS] == PICTURES
    assert len(set(s.name for s in SHOTS)) == 12
    for shot in SHOTS:
        assert [c.number for c in shot.callouts] == list(range(1, len(shot.callouts) + 1)), shot.name
        assert len({c.label for c in shot.callouts}) == len(shot.callouts), f"{shot.name}: two callouts with the same name"
        assert shot.alt and '"' not in shot.alt, shot.name
        assert all(c.label and c.target for c in shot.callouts), shot.name
        assert 3 <= len(shot.callouts) <= 14, shot.name
    assert len(BY_NAME["window-tour"].callouts) >= 12


def test_each_legend_table_in_the_guide_matches_the_picture_it_sits_under():
    for shot in SHOTS:
        img, rows = legend(shot.name)
        assert [int(n) for n, _, _ in rows] == [c.number for c in shot.callouts], shot.name
        assert [label for _, label, _ in rows] == [c.label for c in shot.callouts], shot.name
        assert all(what.strip() for _, _, what in rows), f"{shot.name}: a legend row without an explanation"
        assert re.search(r'alt="([^"]+)"', img).group(1) == shot.alt, f"{shot.name}: the alt text differs from the spec"
        assert int(re.search(r'width="(\d+)"', img).group(1)) <= MAX_DOC_WIDTH, shot.name
        header = [l for l in USAGE.splitlines() if l.startswith("| No. |")]
        assert header and all(h == "| No. | Control | What it does |" for h in header)


def test_the_guide_embeds_only_the_twelve_pictures_and_in_the_order_of_the_spec():
    embedded = re.findall(r"screenshots/annotated/([a-z-]+)\.png", USAGE)
    assert sorted(embedded) == sorted(PICTURES)
    first_seen = [name for name in embedded]
    assert first_seen.index("window-tour") == 0


def test_the_committed_spec_file_is_the_spec():
    assert json.loads((COMMITTED / "spec.json").read_text(encoding="utf-8")) == json.loads(json.dumps(annotated_spec.export_spec()))


def test_the_committed_pictures_are_small_png_files_with_one_badge_per_callout():
    for shot in SHOTS:
        path = COMMITTED / f"{shot.name}.png"
        assert path.is_file(), path
        assert path.stat().st_size <= MAX_BYTES, f"{shot.name}: {path.stat().st_size} bytes"
        im = Image.open(path)
        assert im.format == "PNG" and im.width <= MAX_WIDTH and im.width >= 380, (shot.name, im.size)
        assert count_badges(path) == len(shot.callouts), shot.name


# ---------------------------------------------------------------- nothing real

def test_the_pictures_use_only_titles_of_the_invented_library():
    invented = {g.title for g in GAMES}
    for shot in SHOTS:
        for callout in shot.callouts:
            for title in re.findall(r"row(?:part)?:([^|>]+)", callout.target):
                assert title in invented, f"{shot.name}: {title!r} is not a game of the demo library"
    source = (TOOLS / "make_annotated_screenshots.py").read_text(encoding="utf-8")
    assert "build_demo" in source and "real_showcase" not in source and "--real" not in source
    for title in re.findall(r'select_game\("([^"]+)"', source):
        assert title in invented, title


def test_no_real_brand_or_title_in_the_callouts_or_the_legends():
    texts = [USAGE] + [c.label for s in SHOTS for c in s.callouts] + [s.alt for s in SHOTS]
    for text in texts:
        words = re.findall(r"[a-z0-9]+", text.lower().replace("'", ""))
        hit = [w for w in words if w in DENY_LIST]
        assert not hit, hit
        for phrase in DENY_PHRASES:
            assert phrase not in text.lower(), phrase


# ---------------------------------------------------------------- the tool (needs PyQt5)

def run_tool(out: Path, *extra):
    return subprocess.run([sys.executable, str(TOOLS / "make_annotated_screenshots.py"), "--out", str(out), *extra],
                          capture_output=True, text=True, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, timeout=300)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    pytest.importorskip("PyQt5.QtWidgets")
    first, second = tmp_path_factory.mktemp("annotated-a"), tmp_path_factory.mktemp("annotated-b")
    done = run_tool(first, "--layout")
    assert done.returncode == 0, done.stderr[-2000:]
    again = run_tool(second)
    assert again.returncode == 0, again.stderr[-2000:]
    start = done.stdout.index("{")
    return first, second, json.loads(done.stdout[start:])


def test_the_tool_makes_every_picture_and_the_spec_file(runs):
    first, _, _ = runs
    assert sorted(p.name for p in first.iterdir()) == sorted([f"{n}.png" for n in PICTURES] + ["spec.json"])


def test_every_picture_has_the_badges_of_its_spec_and_they_are_well_placed(runs):
    first, _, layout = runs
    assert sorted(layout) == sorted(PICTURES)
    for shot in SHOTS:
        path = first / f"{shot.name}.png"
        im = Image.open(path)
        assert im.width <= MAX_WIDTH and path.stat().st_size <= MAX_BYTES, (shot.name, im.size, path.stat().st_size)
        badges = layout[shot.name]["badges"]
        radius = layout[shot.name]["radius"]
        assert [b["number"] for b in badges] == [c.number for c in shot.callouts]
        assert count_badges(path) == len(shot.callouts), shot.name
        for b in badges:                                             # inside the picture
            assert radius <= b["x"] <= im.width - radius and radius <= b["y"] <= im.height - radius, (shot.name, b)
        for i, a in enumerate(badges):                               # no two badges touch
            for b in badges[i + 1:]:
                assert ((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5 >= 2 * radius, (shot.name, a, b)


# The review dialog is laid out from the measured width of bold text, and Qt measures bold text slightly differently from one process
# to the next on some machines, so its pixels can differ between two runs. Every other picture must be byte for byte the same.
NOT_BYTE_STABLE = {"review-dialog.png"}


def test_two_runs_make_identical_files(runs):
    first, second, _ = runs
    for path in sorted(first.iterdir()):
        if path.name in NOT_BYTE_STABLE:
            from PIL import Image
            assert Image.open(path).width == Image.open(second / path.name).width, path.name
            continue
        assert path.read_bytes() == (second / path.name).read_bytes(), path.name


def test_the_tool_says_so_when_pyqt5_is_missing(tmp_path):
    code = ("import sys, runpy; sys.modules['PyQt5'] = None; "
            f"runpy.run_path({str(TOOLS / 'make_annotated_screenshots.py')!r}, run_name='__main__')")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path)
    assert done.returncode == 2 and "PyQt5" in done.stderr and "Traceback" not in done.stderr


def test_the_screenshot_tool_of_the_readme_still_works_next_to_it(tmp_path):
    pytest.importorskip("PyQt5.QtWidgets")
    code = "import sys; sys.path.insert(0, %r); import make_annotated_screenshots as m, make_screenshots as s; print(m.Shooter is s.Shooter)" \
        % str(TOOLS)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path,
                          env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    assert done.returncode == 0 and done.stdout.strip() == "True", done.stderr[-1000:]
