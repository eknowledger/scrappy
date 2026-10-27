"""The documents, templates and workflows: they must be true, complete, and say nothing they should not.

These tests read files only. They run in a few seconds, need no network and no Qt. They check that every link and anchor
resolves, that every test named in the architecture document exists, and that nothing personal is in the tree.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

DOC_FILES = ["README.md", "CHANGELOG.md", "ROADMAP.md", "CONTRIBUTING.md", "SECURITY.md", "COMPATIBILITY.md",
             "docs/USAGE.md", "docs/ARCHITECTURE.md", "docs/KNOWN-LIMITATIONS.md", "docs/PROVIDERS.md", "docs/screenshots/README.md",
             ".github/PULL_REQUEST_TEMPLATE.md"]

SIX = ["01-library.png", "02-current.png", "03-search-results.png", "04-review-dialog.png", "05-saved.png", "06-trailer.png"]

NOTICE = ("Trademarks and artwork. The screenshots in this repository show the names, box art and in-game pictures of commercial "
          "video games and consoles. All of them belong to their respective owners and are shown only to illustrate how this tool "
          "works. This project is not affiliated with, endorsed by or sponsored by any of them. The artwork is not part of this "
          "project's source code, is not covered by its MIT licence, and is not needed to run the program. If you are a rights "
          "holder and want an image removed, open an issue at https://github.com/eknowledger/scrappy/issues and it will be removed "
          "promptly.")


def text(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def flat(rel: str) -> str:
    return " ".join(text(rel).split())


def tracked_text_files():
    """Every file of the checkout that is text, without the environment and caches (git is not needed)."""
    skip = {".venv", ".git", "__pycache__", ".pytest_cache", "build", "dist", "node_modules"}
    for path in sorted(REPO.rglob("*")):
        if not path.is_file() or skip & set(path.relative_to(REPO).parts):
            continue
        if path.suffix in (".png", ".pyc", ".whl", ".mp4", ".jpg", ".ico"):
            continue
        yield path


# ---------------------------------------------------------------- headings and anchors (GitHub's rules)

def slug(heading: str) -> str:
    h = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)           # a link keeps its text
    h = h.replace("`", "").replace("*", "").strip().lower()
    h = re.sub(r"[^\w\- ]", "", h)                                     # drop punctuation, keep letters, digits, _ and -
    return h.replace(" ", "-")


def anchors(rel: str) -> set:
    found, seen, fenced = set(), {}, False
    for line in text(rel).splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        m = None if fenced else re.match(r"#{1,6}\s+(.*?)\s*#*\s*$", line)
        if m:
            base = slug(m.group(1))
            count = seen.get(base, 0)
            seen[base] = count + 1
            found.add(base if count == 0 else f"{base}-{count}")
    return found


def link_targets(rel: str):
    body = text(rel)
    body = re.sub(r"```.*?```", "", body, flags=re.S)                  # not inside code blocks
    body = re.sub(r"`[^`\n]*`", "", body)                              # not inside code spans
    for target in re.findall(r"\]\(([^)\s]+)\)", body):
        yield target
    for target in re.findall(r"""(?:src|href)="([^"]+)\"""", body):
        yield target
    for target in re.findall(r"""srcset="([^"]+)\"""", body):
        yield target


def test_every_relative_link_and_anchor_resolves():
    checked = 0
    for rel in DOC_FILES:
        base = (REPO / rel).parent
        for target in link_targets(rel):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path, _, frag = target.partition("#")
            dest = (base / path).resolve() if path else (REPO / rel)
            assert dest.exists(), f"{rel}: broken link {target}"
            if frag and dest.suffix == ".md":
                assert frag in anchors(dest.relative_to(REPO).as_posix()), f"{rel}: no heading for the anchor in {target}"
            checked += 1
    assert checked >= 40


def test_files_that_the_docs_point_at_exist():
    ref = re.compile(r"`((?:deploy|docs|tools|tests|scrappy|assets|\.github)/[A-Za-z0-9_./-]+)`")
    missing = []
    for rel in DOC_FILES:
        for target in ref.findall(text(rel)):
            if target.endswith("/") or "*" in target:
                continue
            if not (REPO / target).exists():
                missing.append(f"{rel}: {target}")
    assert missing == []


def test_every_test_the_docs_name_exists():
    defined = set()
    for path in (REPO / "tests").glob("test_*.py"):
        defined |= set(re.findall(r"^\s*def (test_\w+)\(", path.read_text(encoding="utf-8"), re.M))
    modules = {p.stem for p in (REPO / "tests").glob("test_*.py")}
    missing = []
    for rel in DOC_FILES:
        for span in re.findall(r"`([^`\n]+)`", text(rel)):
            for name in re.findall(r"\btest_[A-Za-z0-9_]+", span):
                if name not in defined and name not in modules:
                    missing.append(f"{rel}: {name}")
    assert missing == []
    assert len(re.findall(r"`test_\w+`", text("docs/ARCHITECTURE.md"))) >= 100          # the invariants and defect tables cite many


# ---------------------------------------------------------------- the licence and the notice

def test_license_is_mit_with_the_authors_name():
    body = text("LICENSE")
    assert body.startswith("MIT License")
    assert re.search(r"^Copyright \(c\) 2026 Ahmed Elmalt$", body, re.M)
    assert "Permission is hereby granted, free of charge" in body


def test_readme_carries_the_trademark_notice_once_verbatim_and_the_screenshot_folder_keeps_its_own():
    assert flat("README.md").count(NOTICE) == 1
    assert flat("docs/screenshots/README.md").count(NOTICE.replace("in this repository", "in this folder")) == 1      # (its own wording)
    readme = text("README.md")
    license_part = " ".join(readme[readme.index("\n## License"):].split())
    assert NOTICE in license_part
    for needed in ("MIT", "PyQt5", "GPLv3", "not** bundled", "yt-dlp", "FFmpeg", "own licences"):
        assert needed in license_part, needed


# ---------------------------------------------------------------- the readme

def test_readme_is_focused_and_complete():
    readme = text("README.md")
    assert len(readme.splitlines()) < 300
    assert readme.lstrip().startswith('<div align="center">')
    assert 'srcset="assets/logo-dark.svg"' in readme and 'src="assets/logo.svg"' in readme
    assert "# Scrappy" in readme
    assert "**Fills in the artwork, trailers and descriptions your scraper missed.**" in readme
    for badge in ("tests.yml/badge.svg", "build.yml/badge.svg", "license-MIT", "python-3.11", "verified%20on-Batocera%20v41",
                  "status-alpha"):
        assert badge in readme, badge
    for heading in ("What it does", "Why", "How it works, in plain words", "Status and compatibility", "Install on Batocera",
                    "Using it", "Safety", "What it contacts", "Demo mode, and the screenshots", "Tests", "Documentation",
                    "Credits", "License"):
        assert f"\n## {heading}\n" in readme, heading
    assert "deploy/install.sh --dry-run" in readme and "python -m scrappy.app --demo" in readme
    assert "x86_64" in readme and "aarch64" in readme and "migrated automatically" in readme
    assert "docs/PROVIDERS.md" in readme and "game-asset-fixer" not in readme
    assert "assets/how-it-works.svg" in readme and "assets/how-it-works-dark.svg" in readme


def test_badges_in_a_group_sit_on_one_source_line():
    lines = [l for l in text("README.md").splitlines() if "img.shields.io" in l or "badge.svg" in l]
    assert len(lines) == 2
    assert lines[0].count("](") == 6 and lines[1].count("](") == 6        # three badges per line, each an image link and a link


def test_the_six_screenshots_the_readme_shows_exist():
    readme = text("README.md")
    for name in SIX:
        assert f"docs/screenshots/{name}" in readme, name
        data = (REPO / "docs" / "screenshots" / name).read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) > 5000, name


def test_the_diagrams_exist_in_both_colour_schemes_and_are_well_formed_xml():
    import xml.dom.minidom
    for name in ("how-it-works.svg", "how-it-works-dark.svg", "logo.svg", "logo-dark.svg"):
        assert xml.dom.minidom.parseString(text(f"assets/{name}")).documentElement.tagName == "svg", name
    assert "#f4f5f8" in text("assets/how-it-works.svg") and "#161b22" in text("assets/how-it-works-dark.svg")


def test_what_it_contacts_lists_every_host_in_the_code():
    """Every host named in the program, the installer or the tools has a line in the README's table (or a wildcard that covers it)."""
    readme = text("README.md")
    table = readme[readme.index("## What it contacts"):readme.index("## Demo mode")]
    hosts = set()
    for folder in ("scrappy", "tools", "deploy"):
        for path in (REPO / folder).rglob("*"):
            if not path.is_file() or path.suffix not in (".py", ".sh", ".txt") or "__pycache__" in path.parts:
                continue
            if path.name.startswith("demo"):
                continue
            for host in re.findall(r"https?://([A-Za-z0-9.-]+)", path.read_text(encoding="utf-8")):
                if not host.endswith((".invalid", ".test", ".example", ".local")) and host not in ("169.254.169.254", "example.com"):
                    hosts.add(host.lower())
    assert {"store.steampowered.com", "api.steampowered.com", "github.com", "files.pythonhosted.org", "duckduckgo.com",
            "i.ytimg.com", "www.youtube.com", "127.0.0.1"} <= hosts
    missing = []
    for host in sorted(hosts):
        covered = host in table or any(f"*.{host.split('.', 1)[1]}" in table for _ in [0] if "." in host)
        covered = covered or any(host.endswith(m) for m in re.findall(r"\*(\.[a-z0-9.-]+)", table))
        if not covered:
            missing.append(host)
    assert missing == []


def test_numbers_the_docs_state_match_the_code():
    from scrappy import config, download, saver
    from scrappy.processing import images
    arch, usage = text("docs/ARCHITECTURE.md"), text("docs/USAGE.md")
    assert f"{download.MAX_IMAGE_BYTES:,}" in arch and f"{int(download.READ_TIMEOUT)} s" in arch
    assert f"whole download {int(download.TOTAL_DEADLINE)} s" in arch and f"{download.MAX_REDIRECTS} redirects" in arch
    assert f"newest {saver.JOURNAL_KEEP}" in arch
    for size in (images.IMAGE_SIZE, images.BOX_SIZE, images.LOGO_SIZE):
        assert f"{size[0]}x{size[1]}" in text("README.md") and f"{size[0]}x{size[1]}" in arch
    for key, value in config.DEFAULTS.items():
        assert f"`{key}`" in usage, f"setting {key} is not in docs/USAGE.md"
        if isinstance(value, int) and not isinstance(value, bool):
            assert f"| `{key}` | `{value}` |" in usage, f"default of {key}"
    for key in config.DEFAULTS:
        assert f"`{key}`" in arch, f"setting {key} is not in docs/ARCHITECTURE.md"


# ---------------------------------------------------------------- the architecture document

def test_architecture_document_has_its_sections_in_order_and_its_size():
    body = text("docs/ARCHITECTURE.md")
    expected = ["Scope", "Terminology", "System overview", "Repository layout", "The contract with Batocera", "Components",
                "Control flow", "Invariants, and where they are enforced", "Lifecycle and teardown", "Limits and budgets",
                "How to change it", "Packaging, deployment and CI", "Testing strategy", "Defect register",
                "Limitations and non-goals", "Contributing", "Appendix"]
    found = re.findall(r"^## (\d+)\. (.*)$", body, re.M)
    assert [(int(n), t) for n, t in found] == list(enumerate(expected, 1))
    assert 800 <= len(body.splitlines()) <= 1250
    for needed in ("**Audience.**", "**Contents**", "### 11.5", "frontend profile", "Not implemented"):
        assert needed in body, needed
    # the recipes the README and ROADMAP link to
    for frag in ("111-add-a-provider", "115-support-another-frontend-a-frontend-profile", "12-packaging-deployment-and-ci"):
        assert frag in anchors("docs/ARCHITECTURE.md"), frag
    for recipe in ("Add a provider", "Add a media slot", "Add a system", "Add a setting", "Add a picture processing step"):
        assert f"Add a {recipe[len('Add a '):]}" in body, recipe


def test_mermaid_diagrams_use_conservative_syntax():
    """No renderer is available offline, so check the mistakes that break a diagram: unknown type, unbalanced subgraphs,
    unquoted odd characters in labels, a reserved word as a node id."""
    body = text("docs/ARCHITECTURE.md")
    blocks = re.findall(r"```mermaid\n(.*?)```", body, re.S)
    assert len(blocks) >= 7
    for block in blocks:
        first = block.splitlines()[0].strip()
        assert first in ("sequenceDiagram", "graph TB", "graph LR", "graph TD"), first
        if first.startswith("graph"):
            opens = sum(1 for l in block.splitlines() if l.strip().startswith("subgraph "))
            closes = sum(1 for l in block.splitlines() if l.strip() == "end")
            assert opens == closes, block
            for line in block.splitlines():
                assert not re.match(r"\s*end\s*[\[\(\{]", line), f"'end' used as a node id: {line}"
                for label in re.findall(r"\[([^\]]*)\]", line):
                    assert label.startswith('"') and label.endswith('"'), f"unquoted label: {line}"
                for label in re.findall(r"\{([^}]*)\}", line):
                    assert label.startswith('"') and label.endswith('"'), f"unquoted label: {line}"
                assert ";" not in line and "%%" not in line, line
        else:
            alt = sum(1 for l in block.splitlines() if l.strip().startswith(("alt ", "loop ", "opt ")))
            ends = sum(1 for l in block.splitlines() if l.strip() == "end")
            assert alt == ends, block
            for line in block.splitlines():
                assert ";" not in line and "#" not in line, line


def test_architecture_invariants_table_cites_real_code():
    body = text("docs/ARCHITECTURE.md")
    section = body[body.index("## 8. Invariants"):body.index("## 9. Lifecycle")]
    rows = [l for l in section.splitlines() if l.startswith("| ") and not l.startswith("| Invariant") and not l.startswith("|---")]
    assert len(rows) >= 25
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert len(cells) == 3 and all(cells), row
        assert re.search(r"`test_\w+", cells[2]) or "No test" in cells[2], row


# ---------------------------------------------------------------- the other documents

def test_changelog_first_release():
    body = text("CHANGELOG.md")
    assert "## [0.1.0] - 2026-10-26" in body
    assert "First public release" in body
    assert body.count("game-asset-fixer") == 1 and "migrated by the installer" in body
    assert "### Added" in body and "### Known limitations" in body
    assert "keepachangelog.com" in body


def test_roadmap_has_the_planned_items_and_what_is_not_planned():
    body = text("ROADMAP.md")
    for item in ("Wikidata", "Image search inside the program", "pasted link", "batch queue", "SteamGridDB and IGDB",
                 "does not depend on an external player", "frontend profile", "RetroBat", "More architectures and Python versions"):
        assert item.lower() in body.lower(), item
    assert "## Not planned" in body


def test_security_policy():
    body = text("SECURITY.md")
    assert "https://github.com/eknowledger/scrappy/security/advisories/new" in body
    for section in ("## Threat model", "### What it executes", "### What it writes, and where", "### The public-host rule (SSRF)",
                    "## The installer", "## Turning things off, summary"):
        assert section in body, section
    for program in ("ffmpeg", "ffprobe", "yt-dlp", "mpv", "vlc", "ffplay", "xdg-open", "chrome", "zenity", "xmessage"):
        assert f"`{program}`" in body or program in body, program
    for needed in ("files.pythonhosted.org", "root", "--dry-run", "--purge", "dry_run_dir", "127.0.0.1:1234", "O_NOFOLLOW"):
        assert needed in body, needed


def test_compatibility_page_has_its_lists_and_the_report_recipe():
    body = text("COMPATIBILITY.md")
    for heading in ("## Verified", "## Tests only", "## Not verified", "## Not supported", "## Report your setup"):
        assert heading in body, heading
    for needed in ("Batocera v41", "x86_64", "aarch64", "RetroBat", "tools/probe_environment.py", "tools/box_selftest.py", "VERSION"):
        assert needed in body, needed


def test_contributing_guide():
    body = text("CONTRIBUTING.md")
    for needed in ("requirements-dev.txt PyQt5", "pytest", "Test first", "No personal data", "No third-party artwork", "MIT license",
                   "docs/ARCHITECTURE.md"):
        assert needed in body, needed


def test_the_old_name_appears_only_where_the_migration_is_explained():
    """The pre-release name `game-asset-fixer` (and its package `gaf`) may be named in exactly four places of the documents: the
    installer code and tests (not checked here), one sentence of the changelog, one sentence of the usage guide, and the installer
    section of the architecture document that describes the migration. Everywhere else the documents say "a pre-release version"."""
    old = re.compile(r"Game Asset Fixer|game-asset-fixer|GameAssetFixer|\bgaf\b|GAF_", re.IGNORECASE)
    migration = re.compile(r"migrat|\.old-|gaf\.app|launcher log|pre-release")          # the lines that explain the migration
    arch = text("docs/ARCHITECTURE.md")
    section = arch[arch.index("### 7.9 The installer"):arch.index("### 7.10 Failure paths")]
    bad = []
    for rel in DOC_FILES:
        for number, line in enumerate(text(rel).splitlines(), 1):
            if not old.search(line):
                continue
            if rel == "CHANGELOG.md" and "migrated by the installer" in line:
                continue
            if rel == "docs/ARCHITECTURE.md" and line in section and migration.search(line):
                continue
            if rel == "docs/USAGE.md" and "is migrated automatically by the installer" in line:       # the one neutral sentence
                continue
            bad.append(f"{rel}:{number}: {line.strip()[:100]}")
    assert bad == []
    assert text("CHANGELOG.md").count("game-asset-fixer") == 1
    assert text("docs/USAGE.md").count("game-asset-fixer") == 1
    assert not (REPO / "docs" / "plans").exists() and not (REPO / "docs" / "BACKLOG.md").exists()
    for rel in DOC_FILES:
        assert "docs/plans" not in text(rel) and "BACKLOG" not in text(rel), rel
    assert "Scrappy" in text("docs/USAGE.md") and "/userdata/system/scrappy" in text("docs/USAGE.md")


# ---------------------------------------------------------------- the provider plan

def test_the_provider_document_exists_is_linked_and_describes_the_real_contract():
    body = text("docs/PROVIDERS.md")
    assert "docs/PROVIDERS.md" in text("README.md") and "PROVIDERS.md" in text("docs/ARCHITECTURE.md")
    assert "PROVIDERS.md" in text("docs/KNOWN-LIMITATIONS.md") and "PROVIDERS.md" in text("ROADMAP.md")
    for name in ("Provider", "ProviderError", "describe_error", "Match", "GameInfo", "AssetCandidate", "search_matches", "merge_infos",
                 "rank_assets", "TRUSTED_VIDEO_HOST_SUFFIXES", "ASSET_HOST_SUFFIXES", "ALLOWED_FORMATS"):
        assert f"`{name}" in body, name
    # every code name the document relies on really exists in the code
    sources = "\n".join((REPO / "scrappy" / rel).read_text(encoding="utf-8") for rel in
                        ("providers/base.py", "models.py", "search.py", "controller.py", "providers/steam.py", "processing/images.py"))
    for name in ("class Provider", "class ProviderError", "def describe_error", "class Match", "class GameInfo", "class AssetCandidate",
                 "def search_matches", "def merge_infos", "def rank_assets", "TRUSTED_VIDEO_HOST_SUFFIXES", "ASSET_HOST_SUFFIXES",
                 "ALLOWED_FORMATS", "def search(self, title", "def fetch(self, match"):
        assert name in sources, name
    # the plan is marked as a plan, its steps come in the order of the roadmap, and the roadmap lists it first
    assert "None of it is implemented" in body and "Part 2. The plan (not implemented)" in body
    order = [body.index(f"### 2.{n} ") for n in range(1, 6)]
    assert order == sorted(order)
    for needed in ("libretro thumbnails", "Wikidata", "SteamGridDB", "IGDB", "provider_api", "capabilities", "Declarative providers",
                   "/userdata/system/scrappy/providers/", "manifest", "trust model", "curated index", "Security considerations",
                   "Open questions"):
        assert needed.lower() in body.lower(), needed
    roadmap = text("ROADMAP.md")
    assert roadmap.index("## More sources") < roadmap.index("## Near") < roadmap.index("## Next")
    limits = flat("docs/KNOWN-LIMITATIONS.md")
    assert "built into the program" in limits and "plugin system for more sources is planned" in limits
    assert "Only Steam and YouTube" not in limits


# ---------------------------------------------------------------- personal data

def test_no_personal_data_in_the_repository():
    """Examples must be neutral: no home directory and no address of a real machine. The needles are assembled from pieces so this
    file does not match itself."""
    forbidden = {
        "a macOS home directory": re.compile("/Users" + "/"),
        "a Linux home directory of a person": re.compile(r"/home/(?!user\b|me\b|pi\b|runner\b)\w+"),
    }
    hits = []
    for path in tracked_text_files():
        body = path.read_text(encoding="utf-8", errors="replace")
        for what, needle in forbidden.items():
            if needle.search(body):
                hits.append(f"{path.relative_to(REPO)}: {what}")
    assert hits == []


def test_nothing_in_the_tree_exposes_a_private_library_or_a_setup():
    """The documents, code, tests, fixtures and tools use invented titles and neutral statements. Real game names are allowed only in
    the files whose subject is the real-title screenshots. The words are assembled from pieces so this file does not match itself."""
    j = "".join
    titles = re.compile("|".join([j(["pop", "py"]), j(["had", "es II"]), j(["moor", "huhn"]), j(["jet ", "kave"]), j(["dream", "works"]),
                                  j(["play", "time"]), j(["hello ", "neighbor"]), j(["garf", "ield"]), j(["mine", "craft"]),
                                  j(["lui", "gi"]), j(["mar", "io"]), j(["wit", "cher"]), j(["pok", "[eé]mon"]),
                                  j(["EA SPO", "RTS"]), j(["Rudolph", " the"])]), re.IGNORECASE)
    piracy = re.compile("|".join([j(["nsw", "2u"]), j(["nx", "brew"]), j(["tao", "dung"]), j(["tend", "oku"])]), re.IGNORECASE)
    history = re.compile("|".join([j(["49", " ?%"]), j(["acceptance", " run"]), j(["one real", " machine"]), j(["developed", " against"]),
                                   j(["working", " name"]), j(["the ", "owner"]), j(["box ", "owner"]), j(["ryz", "en"]), j(["nuc", "box"]),
                                   j(["his", "ense"]), j(["rade", "on"]), j(["192", r"\.168\.1\.", "74"]), j(["owner", "'s library"])]),
                         re.IGNORECASE)
    title_id = re.compile(r"0100[0-9A-Fa-f]{12}")
    invented_ids = ("0100AAAA", "0100BBBB")
    # files whose subject is the real-title screenshots may name those titles
    titles_ok = {"tools/real_showcase.py", "tools/fetch_demo_art.py", "tools/make_screenshots.py", "tools/make_desktop_screenshot.py",
                 "docs/screenshots/README.md", "tests/test_real_showcase.py", "tests/test_demo_showcase.py"}
    skip = {"tests/test_docs.py"}                       # this file defines the words
    bad = []
    for path in tracked_text_files():
        rel = path.relative_to(REPO).as_posix()
        if rel in skip:
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(body.splitlines(), 1):
            if rel not in titles_ok and titles.search(line):
                bad.append(f"{rel}:{number}: a real title: {line.strip()[:80]}")
            if piracy.search(line):
                bad.append(f"{rel}:{number}: a ROM-site name: {line.strip()[:80]}")
            if history.search(line):
                bad.append(f"{rel}:{number}: private or history wording: {line.strip()[:80]}")
            for found in title_id.findall(line):
                if not found.upper().startswith(invented_ids):
                    bad.append(f"{rel}:{number}: a title id that is not an invented one: {found}")
    assert bad == [], "\n".join(bad[:40])


def test_documents_use_only_example_hosts_and_addresses():
    """In the documents, templates and scripts the only private address is the 192.168.1.100 style example, and the only user@host
    is root@batocera.local. (The tests and tools hold private ranges on purpose: they test that those are refused.)"""
    private = re.compile(r"\b(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)\b")
    userhost = re.compile(r"\b([a-z][a-z0-9_-]*)@([A-Za-z0-9.-]+)\b")
    bad = []
    files = [REPO / rel for rel in DOC_FILES] + sorted((REPO / ".github").rglob("*.yml")) + \
        [REPO / "deploy" / "install.sh", REPO / "deploy" / "scrappy.sh", REPO / "deploy" / "scrappy.desktop"]
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for address in private.findall(line):
                if address != "192.168.1.100":
                    bad.append(f"{path.relative_to(REPO)}:{number}: {address}")
            for user, host in userhost.findall(line):
                if "." not in host:                                    # 'actions/checkout@<sha>' is not a user at a host
                    continue
                if (user, host) not in {("root", "batocera.local"), ("root", "192.168.1.100"), ("pi", "10.0.0.7")} \
                        and not host.endswith((".invalid", "example.com", "github.com", "users.noreply.github.com")):
                    bad.append(f"{path.relative_to(REPO)}:{number}: {user}@{host}")
    assert bad == []


# ---------------------------------------------------------------- .github

def test_github_templates():
    bug = text(".github/ISSUE_TEMPLATE/bug_report.yml")
    assert bug.startswith("name:") and "\nbody:\n" in bug
    for field in ("cat /userdata/system/scrappy/VERSION", "tools/probe_environment.py", "/userdata/system/logs/scrappy.log",
                  "/userdata/system/scrappy/logs/scrappy.log", "uname -m", "required: true"):
        assert field in bug, field
    assert len(re.findall(r"^\s+- type: ", bug, re.M)) >= 6
    setup = text(".github/ISSUE_TEMPLATE/setup_report.yml")
    assert setup.startswith("name: I tried it on my setup")
    for field in ("tools/box_selftest.py", "RetroBat", "uname -m", "COMPATIBILITY.md"):
        assert field in setup, field
    assert text(".github/ISSUE_TEMPLATE/feature.yml").startswith("name: Feature request")
    config = text(".github/ISSUE_TEMPLATE/config.yml")
    for url in ("https://github.com/eknowledger/scrappy/discussions", "https://github.com/eknowledger/scrappy/security/advisories/new",
                "https://github.com/eknowledger/scrappy/blob/main/COMPATIBILITY.md"):
        assert url in config, url
    pr = text(".github/PULL_REQUEST_TEMPLATE.md")
    for needed in ("pytest", "Docs updated", "download.py", "No personal data", "Python 3.11", "No third-party artwork"):
        assert needed in pr, needed


def test_workflows_are_pinned_least_privilege_and_complete():
    tests, build = text(".github/workflows/tests.yml"), text(".github/workflows/build.yml")
    pins = {
        "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1",
        "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97  # v7.0.0",
    }
    for body in (tests, build):
        for line in re.findall(r"uses: (\S+)(.*)", body):
            name, _, sha = line[0].partition("@")
            assert re.fullmatch(r"[0-9a-f]{40}", sha), f"{name} is not pinned to a commit"
            assert f"{sha}{line[1]}".strip() == pins[name], name
        assert "permissions:\n  contents: read" in body
        assert "concurrency:" in body and "cancel-in-progress: true" in body
        assert "secrets." not in body and "pull_request_target" not in body
    assert '["3.11", "3.12"]' in tests and "ubuntu-latest" in tests
    assert "sudo apt-get install -y -qq libegl1 libgl1 libxkbcommon0 libdbus-1-3 libfontconfig1 libglib2.0-0 ffmpeg" in tests
    assert "pip install -r requirements-dev.txt PyQt5" in tests and "QT_QPA_PLATFORM: offscreen" in tests
    assert "python -m pytest -v -rs" in tests
    assert "! grep -qi \"skipped\"" in tests and "tests/test_ui_qt.py" in tests                    # the window tests may not be skipped
    assert re.search(r"required:\n    name: unit tests\n    needs: test\n    if: always\(\)", tests)
    assert "fail-fast: false" in tests
    assert "shellcheck deploy/install.sh deploy/scrappy.sh" in build
    assert "bash -n deploy/install.sh" in build and "sh -n deploy/scrappy.sh" in build
    assert "python tools/make_screenshots.py --out /tmp/shots" in build and "-eq 6" in build
    assert re.search(r"required:\n    name: build checks\n    needs: \[scripts, screenshots\]", build)


def test_dependabot_groups_and_caps_both_ecosystems():
    body = text(".github/dependabot.yml")
    assert body.count('package-ecosystem: "github-actions"') == 1 and body.count('package-ecosystem: "pip"') == 1
    assert body.count("open-pull-requests-limit: 3") == 2 and body.count("groups:") == 2
    assert body.count('interval: "weekly"') == 2


def test_requirements_do_not_pull_in_qt():
    assert "PyQt5" not in text("requirements-dev.txt")


# ---------------------------------------------------------------- the code stays within what Batocera v41 runs

def test_python_sources_parse_as_python_311():
    """Batocera v41 has Python 3.11. This catches syntax newer than that (a partial check: it cannot see library calls)."""
    for folder in ("scrappy", "tools", "deploy", "tests"):
        for path in sorted((REPO / folder).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 11))


def test_the_launcher_and_installer_are_executable_scripts_with_a_shebang():
    for rel in ("deploy/install.sh", "deploy/scrappy.sh"):
        assert text(rel).startswith("#!/bin/bash"), rel
        mode = (REPO / rel).stat().st_mode
        assert mode & 0o100, f"{rel} must be executable"


def test_a_checkout_has_no_stray_environment_files():
    names = {p.name for p in REPO.iterdir()}
    for stray in ("dist", "build", ".env", "config.json"):
        assert stray not in names, stray
    if (REPO / ".git").exists():
        done = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True)
        if done.returncode == 0:
            tracked = [f for f in done.stdout.split("\0") if f]
            assert not [f for f in tracked if f.startswith(("docs/plans/", ".venv/")) or f.endswith((".pyc", ".DS_Store"))]
