"""The Scrappy window.

Everything slow runs in a worker (``self.jobs``); this class only builds the screen, starts jobs and shows their results.
Rules: a slot never raises (``safe``), web text is shown as plain text, a result that arrives for something the user has
already moved away from is dropped, and the status line at the bottom always says what is going on.
"""
from __future__ import annotations

import logging
import shutil

from PyQt5 import QtCore, QtGui, QtWidgets

from ..config import DEFAULTS, save_settings
from ..controller import ES_BY_HAND, ES_RELOAD_URL
from ..dates import to_gamelist
from ..models import AssetCandidate
from ..providers.base import describe_error
from ..providers.youtube import InstallResult
from ..saver import ES_RESTART_HINT, SaveError, UndoRefused
from ..search import rank_assets
from ..titles import clean_title
from . import logic, pipeline
from .jobs import safe, set_reporter
from .launch import launch
from .plan import build_plan
from .review import ReviewDialog
from .task_dialog import TaskDialog, TaskOutcome
from .widgets import AssetPanel, FoundHint, VideoPanel, ask_choice, hand_made_candidate, plain_label, to_qimage
from .workers import ensure_dispatcher, new_group

log = logging.getLogger("scrappy.ui")

PLAYER_CHECK_MS = 1500                     # a player that exits non-zero within this time is reported with its last error line
BROWSERS = ("chrome", "xdg-open")          # tried in this order; on the Batocera /usr/bin/chrome is a wrapper script that opens a tab
YES_NO = (("yes", "Yes", "accept"), ("no", "No", "reject"))
READING = "Reading what this game has now…"
INTRO_TIP = "Tip: the Tools menu (top left) installs the YouTube downloader and reloads game lists."      # shown once (intro_seen)
ES_RELOADED_TEXT = "EmulationStation reloaded its game lists."
# the text boxes in the order of the screen: (gamelist tag, name shown to the user)
TEXT_BOXES = (("name", "title"), ("releasedate", "release date"), ("developer", "developer"), ("publisher", "publisher"),
              ("genre", "genre"), ("players", "players"), ("desc", "description"))


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, controller, notices=()):
        super().__init__()
        ensure_dispatcher()
        self.ctl = controller
        self.rows, self.current_row, self.results, self.info = [], None, None, None
        self.current, self.current_images = None, {}           # what the game has now (CurrentAssets) and its ready previews
        self.found_text, self._autofilled = {}, {}             # details found by the search, and boxes that follow them
        self._loaded_text = {}                                 # tag -> what the box showed when Current was read (box form)
        self.notices = [str(n) for n in notices if n]
        self.system_problem = ""
        self._player = None
        self._review_token = 0                                 # changes whenever the screen is reset for another game
        self._task_dialog = None                               # the Tools dialog on screen, if any (only one at a time)
        self.jobs = new_group(self._jobs_changed)
        set_reporter(lambda text: self.status(text, error=True))
        self.setWindowTitle("Scrappy")
        self._build()
        self._fit_to_screen()
        self.refresh_tool_hints()
        self.load_systems()

    def _fit_to_screen(self) -> None:
        screen = QtWidgets.QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.resize(*logic.window_size(area.width(), area.height()))
        else:
            self.resize(1400, 860)

    def cfg_value(self, key):
        return self.ctl.cfg.get(key, DEFAULTS[key])

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        page = QtWidgets.QVBoxLayout(central)
        page.setSpacing(4)
        page.setContentsMargins(6, 4, 6, 4)
        self.notice_label = plain_label("", lines=1)
        self.notice_label.setStyleSheet("QLabel { background: #fff4ce; color: #4d3b00; padding: 4px; }")
        self.notice_area = QtWidgets.QScrollArea()             # a long notice scrolls instead of pushing the window taller
        self.notice_area.setWidgetResizable(True)
        self.notice_area.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.notice_area.setWidget(self.notice_label)
        self.notice_area.hide()
        page.addWidget(self.notice_area)
        outer = QtWidgets.QHBoxLayout()
        page.addLayout(outer, 1)

        # left: system, filter, games
        left = QtWidgets.QVBoxLayout()
        self.system_box = QtWidgets.QComboBox()
        self.system_box.setAccessibleName("System")
        self.filter_box = QtWidgets.QComboBox()
        self.filter_box.setAccessibleName("Which games to list")
        self.filter_box.addItems(["All games", "Missing something"])
        self.list = QtWidgets.QListWidget()
        self.list.setAccessibleName("Games")
        self.list.setTextElideMode(QtCore.Qt.ElideMiddle)
        self.list.setWordWrap(True)                            # a long "(missing: ...)" note wraps instead of being cut off
        self.list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        system_label = QtWidgets.QLabel("S&ystem")
        system_label.setBuddy(self.system_box)
        left.addWidget(system_label)
        left.addWidget(self.system_box)
        left.addWidget(self.filter_box)
        left.addWidget(self.list, 1)

        # centre: search bar and the four review panels
        center = QtWidgets.QVBoxLayout()
        bar = QtWidgets.QHBoxLayout()
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("Game name to search for (Enter to search)")
        self.search_edit.setAccessibleName("Game name to search for")
        self.search_btn = QtWidgets.QPushButton("&Search")
        self.match_box = QtWidgets.QComboBox()
        self.match_box.setAccessibleName("Which game is it")
        self.match_box.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.match_box.setMinimumContentsLength(20)
        bar.addWidget(self.search_edit, 3)
        bar.addWidget(self.search_btn)
        bar.addWidget(self.match_box, 3)
        self.logo_panel = AssetPanel("Logo (transparent)", "logo", self.jobs, self._loader("logo"), logo_tools=True,
                                   local_factory=self.local_candidate, guard=self.need_game)
        self.box_panel = AssetPanel("Box art", "boxart", self.jobs, self._loader("boxart"), local_factory=self.local_candidate,
                                 guard=self.need_game)
        self.image_panel = AssetPanel("Image", "image", self.jobs, self._loader("image"), local_factory=self.local_candidate,
                                   guard=self.need_game)
        self.video_panel = VideoPanel(self.cfg_value("default_video_seconds"), self.cfg_value("max_video_seconds"),
                                      self.jobs, self.ctl.fetch_bytes, self.preview_video, local_factory=self.local_candidate,
                                      guard=self.need_game)
        grid = QtWidgets.QGridLayout()
        grid.addWidget(self.logo_panel, 0, 0)
        grid.addWidget(self.box_panel, 0, 1)
        grid.addWidget(self.image_panel, 1, 0)
        grid.addWidget(self.video_panel, 1, 1)
        for column in (0, 1):
            grid.setColumnStretch(column, 1)
        center.addLayout(bar)
        center.addLayout(grid, 1)

        # right: details and the action buttons
        right = QtWidgets.QVBoxLayout()
        form = QtWidgets.QFormLayout()
        self.name_edit, self.date_edit = QtWidgets.QLineEdit(), QtWidgets.QLineEdit()
        self.dev_edit, self.pub_edit = QtWidgets.QLineEdit(), QtWidgets.QLineEdit()
        self.genre_edit, self.players_edit = QtWidgets.QLineEdit(), QtWidgets.QLineEdit()
        self.date_edit.setPlaceholderText("YYYY-MM-DD")
        self.desc_edit = QtWidgets.QPlainTextEdit()
        self.desc_edit.setAccessibleName("Description")
        self.desc_edit.setTabChangesFocus(True)
        self._scratch = {"line": QtWidgets.QLineEdit(), "desc": QtWidgets.QPlainTextEdit()}     # (never shown: see _box_form)
        self.edits = {"name": self.name_edit, "releasedate": self.date_edit, "developer": self.dev_edit,
                      "publisher": self.pub_edit, "genre": self.genre_edit, "players": self.players_edit, "desc": self.desc_edit}
        self.hints = {tag: FoundHint(name) for tag, name in TEXT_BOXES}
        for label, tag in (("Title", "name"), ("Release date", "releasedate"), ("Developer", "developer"),
                           ("Publisher", "publisher"), ("Genre", "genre"), ("Players", "players")):
            self.edits[tag].setAccessibleName(label)
            cell = QtWidgets.QWidget()
            column = QtWidgets.QVBoxLayout(cell)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(1)
            column.addWidget(self.edits[tag])
            column.addWidget(self.hints[tag])
            form.addRow(label, cell)
        right.addLayout(form)
        right.addWidget(QtWidgets.QLabel("Description"))
        right.addWidget(self.desc_edit, 1)
        right.addWidget(self.hints["desc"])
        self.use_all_btn = QtWidgets.QPushButton("&Use all found details")
        self.use_all_btn.setToolTip("Replace every detail that has a 'found:' line with the found value")
        self.use_all_btn.hide()
        right.addWidget(self.use_all_btn)
        self.save_btn = QtWidgets.QPushButton("Save")
        self.save_btn.setToolTip("Shows exactly what will be written, then asks you to confirm (Ctrl+S)")
        self.save_btn.setShortcut("Ctrl+S")
        self.skip_btn = QtWidgets.QPushButton("Skip")
        self.skip_btn.setToolTip("Go to the next game without saving")
        self.undo_btn = QtWidgets.QPushButton("Undo last save")
        for b in (self.save_btn, self.skip_btn, self.undo_btn):
            right.addWidget(b)

        outer.addLayout(left, 3)
        outer.addLayout(center, 8)
        outer.addLayout(right, 3)

        # always-visible status line
        row = QtWidgets.QHBoxLayout()
        self.status_label = plain_label("", lines=1)
        self.status_label.setAccessibleName("Status")
        self.busy_bar = QtWidgets.QProgressBar()
        self.busy_bar.setRange(0, 0)                          # endless "working" bar
        self.busy_bar.setMaximumWidth(120)
        self.busy_bar.setTextVisible(False)
        self.busy_bar.hide()
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.reload_btn = QtWidgets.QPushButton("Reload game lists")      # shown only after a save EmulationStation did not take
        self.reload_btn.setToolTip("Ask EmulationStation to reload its game lists, so the new pictures and details show up")
        self.reload_btn.hide()
        row.addWidget(self.status_label, 1)
        row.addWidget(self.reload_btn)
        row.addWidget(self.busy_bar)
        row.addWidget(self.cancel_btn)
        page.addLayout(row)

        tools = self.menuBar().addMenu("&Tools")
        tools.addAction("Install / update YouTube downloader", safe(self.install_ytdlp))
        tools.addAction("Reload game lists in EmulationStation", safe(self.reload_es))
        self.video_panel.install_requested.connect(safe(self.install_ytdlp))
        self.reload_btn.clicked.connect(safe(self.reload_es))

        # signals: every slot is wrapped, so nothing can raise into Qt
        self.system_box.currentTextChanged.connect(safe(self.on_system_changed))
        self.filter_box.currentIndexChanged.connect(safe(self.load_rows))
        self.list.currentRowChanged.connect(safe(self.select_row, 1))
        self.list.itemActivated.connect(safe(self.search_edit.setFocus))
        self.date_edit.textChanged.connect(safe(self.check_date))
        for tag, edit in self.edits.items():
            edit.textChanged.connect(safe(lambda tag=tag: self._update_hint(tag)))
            self.hints[tag].use_clicked.connect(safe(lambda tag=tag: self.use_found(tag)))
        self.use_all_btn.clicked.connect(safe(self.use_all_found))
        self.search_edit.returnPressed.connect(safe(self.do_search))
        self.search_btn.clicked.connect(safe(self.do_search))
        self.match_box.currentIndexChanged.connect(safe(self.on_match_changed, 1))
        for panel in (self.logo_panel, self.box_panel, self.image_panel):
            panel.web_requested.connect(safe(self.open_web_search, 1))
            panel.message.connect(safe(self.status, 1))
        self.video_panel.message.connect(safe(self.status, 1))
        self.logo_panel.text_logo_requested.connect(safe(self.make_text_logo))
        self.save_btn.clicked.connect(safe(self.do_save))
        self.skip_btn.clicked.connect(safe(self.skip))
        self.undo_btn.clicked.connect(safe(self.do_undo))
        self.cancel_btn.clicked.connect(safe(self.cancel_work))
        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+F"), self, safe(self.search_edit.setFocus))
        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+N"), self, safe(self.skip))
        self._refresh_controls()
        self.status("Loading your game folders…")

    # ------------------------------------------------------------------ status, notices, controls
    def status(self, text, error: bool = False) -> None:
        text = logic.one_line(text)
        font = self.status_label.font()
        font.setBold(bool(error))
        self.status_label.setFont(font)
        self.status_label.setText(text)
        self.status_label.setToolTip(logic.plain_tooltip(text))
        (log.warning if error else log.info)("status: %s", text)

    def check_date(self) -> None:
        """Mark a release date that cannot be saved (it would be left out) as soon as it is typed. A date that is stored
        in gamelist.xml as it is and was not edited is not a problem: it is left alone."""
        problem = "" if self._date_untouched() else logic.date_problem(self.date_edit.text())
        self.date_edit.setStyleSheet("QLineEdit { border: 2px solid #c00000; }" if problem else "")
        self.date_edit.setToolTip(logic.plain_tooltip(problem) if problem else "Type 2026-02-18, Feb 18, 2026 or just the year")

    def _update_notice(self) -> None:
        text = "\n".join(self.notices + ([self.system_problem] if self.system_problem else []))
        self.notice_label.setText(text)
        self.notice_label.setToolTip(logic.plain_tooltip(text))
        line = self.notice_label.fontMetrics().lineSpacing()
        self.notice_area.setMinimumHeight(line + 8)
        self.notice_area.setMaximumHeight(line * 3 + 12)
        self.notice_area.setVisible(bool(text))

    def add_notice(self, text: str) -> None:
        if text:
            self.notices.append(str(text))
            self._update_notice()

    def _jobs_changed(self) -> None:
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        running = self.jobs.running
        has_row = self.current_row is not None
        saving = bool(running("save") or running("undo"))
        self.search_btn.setEnabled(has_row and not running("search"))
        self.save_btn.setEnabled(has_row and not saving)
        self.undo_btn.setEnabled(not saving)
        self.busy_bar.setVisible(bool(self.jobs.active()))
        self.cancel_btn.setEnabled(any(running(n) and not running(n).critical for n in ("search", "info", "save")))

    def cancel_work(self) -> None:
        for name in ("search", "info", "save"):
            self.jobs.cancel(name)
        self._refresh_controls()
        self.status("Cancelled.")

    def need_game(self) -> str:
        """'' when pictures may be added (a game is chosen), else the plain sentence to show."""
        return "" if self.current_row is not None else "Choose a game on the left first."

    def local_candidate(self, slot: str, path: str):
        """The 'your file' candidate for a picture the user chose (runs in a worker). The controller registers the file
        address it hands out; an older controller without ``local_candidate`` gets a hand-built one."""
        make = getattr(self.ctl, "local_candidate", None)
        return make(slot, path) if make is not None else hand_made_candidate(slot, path)

    # ------------------------------------------------------------------ processing callbacks (what you see is what is saved)
    def _loader(self, slot: str):
        def load(cand, remove_bg, local_ok, cancelled):
            return pipeline.load_candidate(self.ctl.fetch_bytes, cand, slot, remove_bg, local_ok, cancelled)
        return load

    def make_text_logo(self) -> None:
        title = self.name_edit.text() or self.search_edit.text()

        def work(job):
            processed = pipeline.text_logo(title)
            return processed, to_qimage(processed.data)

        self.jobs.start("textlogo", work, self._bound(lambda r: self.logo_panel.set_direct(r[0], r[1], "generated text logo")),
                        self._bound(lambda m: self.status("Could not make the text logo: " + m, error=True)))

    # ------------------------------------------------------------------ systems / ROM list
    def load_systems(self) -> None:
        self.jobs.start("systems", lambda job: self.ctl.list_systems(), self.on_systems,
                        lambda m: self.status("Could not read the game folders: " + m, error=True))

    def on_systems(self, names) -> None:
        self.system_box.blockSignals(True)
        self.system_box.clear()
        self.system_box.addItems(names)
        self.system_box.blockSignals(False)
        if not names:
            self.status(f"No game folders found in {self.ctl.roms_root}.", error=True)
            return
        self.status("Choose a game on the left." + self._intro_tip())
        self.on_system_changed()

    def on_system_changed(self) -> None:
        self._cancel_review_jobs()
        self.current_row = None
        self.rows = []
        self.list.blockSignals(True)
        self.list.clear()
        self.list.blockSignals(False)
        self.reset_review()
        self.load_rows()

    def load_rows(self) -> None:
        system = self.system_box.currentText()
        if not system:
            return
        only_missing = self.filter_box.currentIndex() == 1
        self.jobs.cancel("rows")
        self.jobs.start("rows", lambda job: (system, self.ctl.rows(system, only_missing=only_missing)), self.on_rows,
                        lambda m: self.status(f"Could not read the games of {system}: {m}", error=True))

    def on_rows(self, result) -> None:
        system, rows = result
        if system != self.system_box.currentText():
            return                                             # the user moved to another system meanwhile
        keep = self.current_row.rom_path if self.current_row else None
        self.rows = rows
        self.list.blockSignals(True)
        self.list.clear()
        target = -1
        for i, r in enumerate(rows):
            item = QtWidgets.QListWidgetItem(logic.row_text(r))
            item.setToolTip(logic.plain_tooltip(r.rom_path))
            self.list.addItem(item)
            if r.rom_path == keep:
                target = i
        if target >= 0:
            self.list.setCurrentRow(target)
        self.list.blockSignals(False)
        self.system_problem = rows[0].problem if rows and rows[0].problem else ""
        self._update_notice()
        if target >= 0:
            self.current_row = rows[target]                    # fresh status; the review on screen is kept
        elif keep is not None:
            self.current_row = None                            # e.g. saved and now hidden by "Missing something"
            self.reset_review()
        if not rows:
            self.status(f"No games to show in {system}." + (" Everything has all its files." if self.filter_box.currentIndex() == 1 else ""))
        self._refresh_controls()

    def select_row(self, i: int) -> None:
        if i < 0 or i >= len(self.rows):
            return
        self._cancel_review_jobs()
        self.current_row = self.rows[i]
        self.search_edit.setText(clean_title(self.current_row.rom_path) or self.current_row.title)
        self.reset_review()
        self.status(READING)
        self.start_current()
        self._refresh_controls()

    def _cancel_review_jobs(self) -> None:
        for name in ("search", "info", "current", "picture", "thumbnail", "textlogo", "localfile"):
            self.jobs.cancel(name)

    def _bound(self, fn):
        """`fn`, but only if the review on screen is still the one that was current when this was called (a result that
        arrives after the user moved to another game or system must not fill the new game's panels)."""
        token = self._review_token

        def call(*args):
            if token == self._review_token:
                fn(*args)
        return call

    def reset_review(self) -> None:
        self._review_token += 1
        for p in (self.logo_panel, self.box_panel, self.image_panel):
            p.clear()
        self.video_panel.reset()
        self.match_box.blockSignals(True)
        self.match_box.clear()
        self.match_box.blockSignals(False)
        self.current, self.current_images, self.found_text, self._autofilled, self._loaded_text = None, {}, {}, {}, {}
        for e in self.edits.values():
            e.clear()
        for hint in self.hints.values():
            hint.clear()
        self._update_use_all()
        self.results = self.info = None

    # ------------------------------------------------------------------ what the game has now
    def start_current(self) -> None:
        """Read the game's own files and details in a worker; the result fills the panels as 'Current'."""
        row, system = self.current_row, self.system_box.currentText()
        if row is None:
            return
        self.jobs.cancel("current")

        def work(job):
            current = self.ctl.load_current(system, row.rom_path, cancelled=job.cancelled)
            # the previews are made here, in the worker: the window only receives ready images
            images = {slot: to_qimage(a.preview) if a is not None and a.preview else None for slot, a in current.pictures.items()}
            return current, images

        self.jobs.start("current", work, self._bound(self.on_current),
                        self._bound(lambda m: self.status("Could not read what this game has now: " + m, error=True)))

    def on_current(self, result) -> None:
        first_time = self.current is None                       # (later reads, after a save or an undo, refresh every box)
        self.current, self.current_images = result
        for panel in (self.logo_panel, self.box_panel, self.image_panel):
            panel.set_current(self.current.pictures[panel.slot], self.current_images.get(panel.slot))
        self.video_panel.set_current(self.current.video)
        self._autofilled = {}
        for tag in self.edits:                                  # the boxes show what the game has now
            shown = logic.display_value(tag, self.current.fields.get(tag, ""))
            if not (first_time and self._edit_text(tag)):       # text typed before Current arrived is the user's: keep it
                self._set_edit(tag, shown)
            self._loaded_text[tag] = self._box_form(tag, shown)
        self.check_date()                                       # (the date box was filled before the snapshot existed)
        self.refresh_found_text()
        row = self.current_row
        missing = f" Missing: {logic.names(row.missing)}." if row is not None and row.missing else ""
        if self.status_label.text() == READING:                 # (a message the user caused in the meantime stays)
            self.status("Showing what this game has now (Current)." + missing + " Press Search (or Enter in the name box) to look for more.")

    def _set_edit(self, tag: str, text: str) -> None:
        edit = self.edits[tag]
        if tag == "desc":
            edit.setPlainText(text)
        else:
            edit.setText(text)

    def _edit_text(self, tag: str) -> str:
        edit = self.edits[tag]
        return (edit.toPlainText() if tag == "desc" else edit.text()).strip()

    def _box_form(self, tag: str, text: str) -> str:
        """`text` as the box would hold it (a text box turns CR/LF, U+2028 and non-breaking spaces into its own forms)."""
        box = self._scratch["desc"] if tag == "desc" else self._scratch["line"]
        if tag == "desc":
            box.setPlainText(text or "")
            return box.toPlainText().strip()
        box.setText(text or "")
        return box.text().strip()

    def _untouched(self, tag: str) -> bool:
        """The box still shows exactly what it showed when Current was read, so there is nothing to write for it."""
        return self.current is not None and self._edit_text(tag) == self._loaded_text.get(tag)

    def _date_untouched(self) -> bool:
        return self._untouched("releasedate")

    def _edit_value(self, tag: str) -> str:
        """What the box would be saved as: the game's own stored value while the box is untouched (whatever it looks like on
        screen), else the text (the date in gamelist form; '' when empty or unreadable)."""
        if self._untouched(tag):
            return self.current.fields.get(tag, "")
        text = self._edit_text(tag)
        return to_gamelist(text) if tag == "releasedate" else text

    def refresh_found_text(self) -> None:
        """Offer the details a search found. Nothing a search found replaces text that is there: each differing value gets a
        'found:' line with a Use button. A box that is empty in gamelist.xml follows the found value until you edit it
        (filling a blank replaces nothing)."""
        if self.current is None:
            return
        for tag in self.edits:
            found = self.found_text.get(tag, "")
            shown = self._edit_text(tag)
            auto = self._autofilled.get(tag)
            if not self.current.fields.get(tag, "") and (not shown or shown == auto):
                wanted = logic.display_value(tag, found)
                if self._box_form(tag, wanted) != shown:
                    self._set_edit(tag, wanted)
                if wanted:
                    self._autofilled[tag] = self._edit_text(tag)        # as the box holds it, so later comparisons match
                else:
                    self._autofilled.pop(tag, None)
            self._update_hint(tag)

    def _update_hint(self, tag: str) -> None:
        found = self.found_text.get(tag, "")
        hint = self.hints[tag]
        same = found == self._edit_value(tag) if tag == "releasedate" else self._box_form(tag, found) == self._edit_text(tag)
        if found and not same:
            hint.show_value(logic.display_value(tag, found))
        else:
            hint.clear()
        self._update_use_all()

    def _update_use_all(self) -> None:
        self.use_all_btn.setVisible(any(not hint.isHidden() for hint in self.hints.values()))

    def use_found(self, tag: str) -> None:
        found = self.found_text.get(tag, "")
        if found:
            self._set_edit(tag, logic.display_value(tag, found))
            self.edits[tag].setFocus()

    def use_all_found(self) -> None:
        for tag in self.edits:
            if not self.hints[tag].isHidden():
                self.use_found(tag)
        self.save_btn.setFocus()

    def clear_found(self) -> None:
        """Forget what the last search offered (pictures, trailers, details); Current stays."""
        self.info = None
        for panel in (self.logo_panel, self.box_panel, self.image_panel):
            panel.set_candidates([])
        self.found_text = {}
        self.refresh_found_text()

    def skip(self) -> None:
        row = self.list.currentRow()
        if row + 1 < self.list.count():
            self.list.setCurrentRow(row + 1)
        else:
            self.status("That was the last one in this list.")

    # ------------------------------------------------------------------ search
    def do_search(self) -> None:
        title = self.search_edit.text().strip()
        if self.current_row is None:
            self.status("Choose a game on the left first.")
            return
        if not title:
            self.status("Type the name of the game first.")
            return
        if self.jobs.running("search"):
            self.status("Still searching… (press Cancel to stop)")
            return
        self.jobs.cancel("info")
        system = self.system_box.currentText()
        self.status(f"Searching for “{title}”… (Steam and YouTube; this can take up to a minute)")
        self.jobs.start("search", lambda job: self.ctl.search(title, system, cancelled=job.cancelled),
                        self._bound(lambda r: self.on_search_done(r, title)), self._bound(lambda m: self.status("Search failed: " + m, error=True)))
        self._refresh_controls()

    def on_search_done(self, results, title: str = "") -> None:
        self.refresh_tool_hints()                              # (the downloader may have been installed or removed by hand)
        if not results.matches:
            self.clear_found()                                 # nothing from the previous search may stay on screen
        self.results = results
        self.match_box.blockSignals(True)
        self.match_box.clear()
        for m in results.matches:
            self.match_box.addItem(logic.match_text(m), m)
        self.match_box.blockSignals(False)
        errs = logic.format_errors(results.errors)
        if not results.matches:
            self.results = results
            self.video_panel.set_candidates(results.videos)
            self.status(f"Nothing found for {title or 'that name'}" + (f" ({errs})" if errs else "")
                        + ". Try another spelling, or use Paste link / Choose file.", error=bool(errs))
            return
        self.status(f"{len(results.matches)} match(es). Check that the right game is chosen." + (f"  Problems: {errs}" if errs else ""))
        self.match_box.setCurrentIndex(0)
        self.on_match_changed(0)

    def on_match_changed(self, index: int) -> None:
        if index < 0 or not self.results:
            return
        match = self.match_box.itemData(index)
        if match is None:
            return
        self.jobs.cancel("info")
        self.status(f"Loading details for “{match.title}”…")
        def current(fn):                                       # also drop it if another match was chosen meanwhile
            def call(*args):
                if self.match_box.itemData(self.match_box.currentIndex()) is match:
                    fn(*args)
            return call

        self.jobs.start("info", lambda job: self.ctl.load_info(match), self._bound(current(self.on_info)),
                        self._bound(current(lambda m: self.status("Could not load details: " + m, error=True))))
        self._refresh_controls()

    def on_info(self, info) -> None:
        self.info = info
        self.found_text = {tag: (value or "").strip() for tag, value in (
            ("name", info.name), ("releasedate", info.release_date), ("developer", info.developer), ("publisher", info.publisher),
            ("genre", info.genre), ("players", info.players), ("desc", info.description))}
        self.refresh_found_text()
        self.logo_panel.set_candidates(rank_assets(info.assets_for("logo"), "logo"))
        self.box_panel.set_candidates(rank_assets(info.assets_for("boxart"), "boxart"))
        self.image_panel.set_candidates(rank_assets(info.assets_for("image"), "image"))
        self.video_panel.set_candidates(info.assets_for("video") + (self.results.videos if self.results else []))
        self.status("Review the pictures and details, then press Save. Use ◀ ▶ to compare new pictures with Current.")

    # ------------------------------------------------------------------ helpers
    def open_web_search(self, slot: str = "logo") -> None:
        url = logic.web_search_url(self.search_edit.text() or self.name_edit.text(), slot)
        found = [path for path in (shutil.which(name) for name in BROWSERS) if path]

        def work(job):
            for program in found:                                  # first one that starts wins; launch() detaches it
                try:
                    launch([program, url], "browser")
                    return True
                except OSError as exc:
                    log.info("could not start %s: %s", program, exc)
            return False

        def done(opened):
            if opened:
                self.status("A web search was opened in your browser. Copy the picture's address and use Paste link….")
            else:
                self._no_browser(url)

        self.jobs.start("web", work, done, lambda _msg: self._no_browser(url))

    def _no_browser(self, url: str) -> None:
        QtWidgets.QApplication.clipboard().setText(url)
        self.status("No web browser could be opened. The search address was copied; paste it into a browser: " + url)

    def _stop_player(self) -> None:
        player, self._player = self._player, None
        if player is not None and player.poll() is None:
            try:
                player.terminate()
            except OSError:
                pass

    def preview_video(self, cand, start, end) -> None:
        ytdlp = self.ctl.ytdlp
        local_ok = cand.url in self.video_panel.local_urls
        if cand.current:                                           # the game's own video: play the file the controller found
            if self.current is None or self.current.video is None:
                return
            cand, start, end, local_ok = AssetCandidate(slot="video", url="file://" + self.current.video.path, kind="file",
                                                        source="current"), 0.0, None, True
        # a file address is played only if the controller issued it for a file the user chose (the panel records those)
        argv, reason = logic.preview_command(cand, start, end, which=shutil.which, ytdlp_path=str(ytdlp.path), ytdlp_ok=ytdlp.available(),
                                             local_ok=local_ok)
        if argv is None:
            self.status(reason, error=True)
            return
        self._stop_player()                                        # also notices a player that exited by itself
        try:
            self._player = launch(argv, "player")
        except OSError as exc:
            self.status(f"Could not start the video player: {exc.strerror or exc}", error=True)
            return
        self.status("Playing the preview in a separate window. Close it when you are done.")
        player = self._player
        QtCore.QTimer.singleShot(PLAYER_CHECK_MS, safe(lambda: self._check_player(player)))

    def _check_player(self, player) -> None:
        """A player that died right after starting (bad option, no display, no codec) says why instead of silence."""
        code = player.poll()
        if player is self._player and code not in (None, 0):
            reason = getattr(player, "last_error", "") or f"it exited with code {code}"
            self.status(f"The video player stopped: {reason}", error=True)

    # ------------------------------------------------------------------ Tools: dialogs for the two slow actions
    def run_task(self, title, message, work, *, cancellable=False, hint="", failed_title="Could not finish",
                 cancelled_text="Cancelled. Nothing was changed.", on_done=None):
        """Show a modal progress dialog that runs `work(ctx)` in the background (see ``task_dialog``). Only one at a time: a
        second request just brings the open one to the front. Returns the dialog, or None when one is open already."""
        old = self._task_dialog
        if old is not None:
            old.raise_()
            old.activateWindow()
            self.status("Please finish the open window first.")
            return None
        dialog = TaskDialog(self, self.jobs, title, message, work, cancellable=cancellable, hint=hint, failed_title=failed_title,
                            cancelled_text=cancelled_text)
        self._task_dialog = dialog
        if on_done is not None:
            dialog.finished_with.connect(safe(on_done, 1))
        dialog.finished.connect(safe(lambda: self._task_closed(dialog)))
        dialog.show()
        dialog.start()
        return dialog

    def _task_closed(self, dialog) -> None:
        if self._task_dialog is dialog:
            self._task_dialog = None
        dialog.deleteLater()
        self._refresh_controls()

    def _downloader_missing(self) -> bool:
        available = getattr(self.ctl.ytdlp, "available", None)
        try:
            return available is not None and not available()
        except Exception:                                      # noqa: BLE001 - a broken check must not break the window
            return False

    def refresh_tool_hints(self) -> None:
        """Show the Install button in the Trailer panel exactly while the YouTube downloader is missing."""
        self.video_panel.set_installer_missing(self._downloader_missing())

    def install_ytdlp(self) -> None:
        tool = self.ctl.ytdlp
        update = not self._downloader_missing()
        verb = "update" if update else "install"

        def work(ctx):
            started = []

            def progress(done, total):
                if not started:                                # the first report: the connection is open, the file is coming
                    started.append(True)
                    ctx.message("Downloading yt-dlp from GitHub…", busy=False)
                ctx.progress(done, total)

            install = getattr(tool, "install", None)
            if install is not None:
                result = install(progress=progress, cancelled=ctx.cancelled, stage=ctx.message)
            else:                                              # a stand-in that only knows update()
                result = InstallResult(tool.update(), "", "", update)
            summary, lines = logic.install_texts(result)
            return TaskOutcome.success(summary, *lines)

        self.status("Updating the YouTube downloader…" if update else "Installing the YouTube downloader…")
        self.run_task(
            "Update the YouTube downloader" if update else "Install the YouTube downloader",
            "Getting yt-dlp from its official GitHub page (about 3 MB)…", work, cancellable=True,
            hint="Check that this machine is online, then try again. The copy you had before (if any) was not touched.",
            failed_title=f"Could not {verb} the YouTube downloader",
            cancelled_text="Cancelled. Nothing was installed." if not update else "Cancelled. The downloader you had was not changed.",
            on_done=lambda outcome: self._ytdlp_done(outcome, verb))

    def _ytdlp_done(self, outcome, verb: str) -> None:
        self.refresh_tool_hints()
        if outcome.ok:
            self.status(" ".join([outcome.summary] + [line for line in outcome.details if not line.startswith("File:")]))
        elif outcome.cancelled:
            self.status(outcome.summary)
        else:
            self.status(f"Could not {verb} the YouTube downloader: {outcome.summary}", error=True)

    def reload_es(self) -> None:
        def work(ctx):
            ctx.message("Asking EmulationStation to reload its game lists…")
            if self.ctl.ask_es_to_reload():
                return TaskOutcome.success(ES_RELOADED_TEXT, "New pictures and details show up in its menus now.")
            return TaskOutcome.failure(
                "Could not reach EmulationStation, so its game lists were not reloaded.", f"Do it by hand: {ES_BY_HAND}.",
                f"No answer from {ES_RELOAD_URL} (GET) within a few seconds. EmulationStation is probably not running on "
                "this machine, or it did not accept the request.")

        self.status("Asking EmulationStation to reload its game lists…")
        self.run_task("Reload game lists in EmulationStation", "Asking EmulationStation to reload its game lists…", work,
                      failed_title="EmulationStation was not reached", on_done=self._reload_done)

    def _reload_done(self, outcome) -> None:
        if outcome.ok:
            self.reload_btn.hide()
            self.status(outcome.summary)
        else:
            self.status(f"Could not reach EmulationStation. Use {ES_BY_HAND}.", error=True)

    # ------------------------------------------------------------------ the one-time tip
    def _intro_tip(self) -> str:
        """' Tip: ...' the first time the window starts (setting `intro_seen`), else ''. Marks it seen at once, so it shows once."""
        if self.cfg_value("intro_seen"):
            return ""
        cfg = self.ctl.cfg
        try:
            cfg["intro_seen"] = True
        except TypeError:                                      # a settings object that cannot be changed: the tip may repeat
            pass
        path = getattr(cfg, "path", None)
        if path is not None and not getattr(cfg, "warnings", None):      # (a file with problems is never rewritten)
            self.jobs.start("settings", lambda job: save_settings({"intro_seen": True}, path=path), lambda _p: None,
                            lambda msg: log.info("could not remember that the tip was shown: %s", msg))
        return " " + INTRO_TIP

    # ------------------------------------------------------------------ save / undo
    def make_plan(self):
        """What a Save would do right now (GUI thread only; nothing is read from disk or encoded here)."""
        panels = (self.logo_panel, self.box_panel, self.image_panel)
        return build_plan(
            self.current_title(), str(self.ctl.system_dir(self.system_box.currentText())), self.current,
            [p.pick() for p in panels], self.video_panel.pick(), {tag: self._edit_value(tag) for tag in self.edits},
            max_seconds=self.cfg_value("max_video_seconds"),
            current_images={slot: image for slot, image in self.current_images.items() if image is not None})

    def current_title(self) -> str:
        row = self.current_row
        return (self.current.fields.get("name") if self.current else "") or (row.title if row else "") or "this game"

    def request_for(self, plan):
        """The SaveRequest for exactly what the plan lists (the video is attached later, by the save job)."""
        data = {item.slot: item.data for item in plan.pictures}
        return self.ctl.build_request(
            self.system_box.currentText(), self.current_row.rom_path, plan.fields, image_jpg=data.get("image"),
            thumb_png=data.get("boxart"), marquee_png=data.get("logo"), replace=plan.replace, current=self.current.fields,
            link=plan.link_tags)

    def review(self, plan) -> bool:
        """Show the review dialog. True only when the user pressed Save in it."""
        dialog = ReviewDialog(plan, self)
        try:
            return dialog.exec_() == QtWidgets.QDialog.Accepted and not plan.is_empty
        finally:
            dialog.deleteLater()

    def do_save(self) -> None:
        if self.current_row is None:
            self.status("Choose a game on the left first.")
            return
        if self.jobs.running("save") or self.jobs.running("undo"):
            return
        if self.current is None:
            self.status("Still reading what this game has now. Wait a moment, then press Save again." if self.jobs.running("current")
                        else "Nothing can be saved because what this game has now could not be read (see the message above). "
                             "Choose the game again.", error=not self.jobs.running("current"))
            return
        if any(p.busy for p in (self.logo_panel, self.box_panel, self.image_panel)):
            self.status("Pictures are still loading. Wait a moment, then press Save again.")
            return
        problem = "" if self._date_untouched() else logic.date_problem(self.date_edit.text())
        if problem:
            self.status(problem, error=True)
            self.date_edit.setFocus()
            self.date_edit.selectAll()
            return
        plan = self.make_plan()
        system, row = self.system_box.currentText(), self.current_row
        try:
            req = None if plan.is_empty else self.request_for(plan)
        except (SaveError, ValueError) as exc:
            self.status("Save failed: " + describe_error(exc), error=True)
            return
        if not self.review(plan):
            self.status("Nothing to save: nothing new was chosen." if plan.is_empty else "Not saved.")
            return
        video = plan.video

        def work(job):
            if video is not None:
                req.video_mp4 = self.ctl.make_video(video.candidate, video.start, video.end, cancelled=job.cancelled)
            if not job.enter_critical():
                raise InterruptedError("cancelled")
            job.notify("Saving…")
            return self.ctl.save(req)

        self.status("Preparing the video, then saving… (this can take a minute)" if video else "Saving…")
        self.jobs.start("save", work, lambda report: self.on_saved(report, system, row.rom_path), self.on_save_failed,
                        on_progress=self.status)
        self._refresh_controls()

    def on_save_failed(self, msg: str) -> None:
        self.status("Save failed: " + msg, error=True)          # SaveError messages already say "Nothing was changed." when true

    def on_saved(self, report, system: str = "", rom_path: str = "") -> None:
        self.status(logic.saved_message(report, ES_RESTART_HINT))
        # EmulationStation runs but did not take the reload: offer to ask again (the status line carries the restart hint)
        self.reload_btn.setVisible(bool(getattr(report, "es_running", False) and not getattr(report, "es_reloaded", False)
                                        and not getattr(report, "es_warning", "")))
        self.load_rows()
        row = self.current_row
        if row is not None and row.rom_path == rom_path and self.system_box.currentText() == system:
            self.start_current()                                # the panels now show what was saved as Current

    def do_undo(self) -> None:
        if self.jobs.running("save") or self.jobs.running("undo"):
            return
        if ask_choice(self, "Undo", "Undo the last save?\n\ngamelist.xml goes back to how it was, and the files that save "
                      "created are removed (files it replaced come back).", YES_NO, "no") != "yes":
            return
        self._start_undo(False)

    def _start_undo(self, force: bool) -> None:
        def work(job):
            if not job.enter_critical():
                raise InterruptedError("cancelled")                 # from here on the window will not close under it
            try:
                return "done", self.ctl.undo(force=force)
            except UndoRefused as exc:                         # gamelist.xml changed after our save (e.g. by EmulationStation)
                return "refused", describe_error(exc)

        self.status("Undoing the last save…")
        self.jobs.start("undo", work, self.on_undone, lambda m: self.status("Undo failed: " + m, error=True))
        self._refresh_controls()

    def on_undone(self, result) -> None:
        kind, value = result
        if kind == "refused":
            if ask_choice(self, "Undo anyway?", value + "\n\nUndo anyway? The current file is kept in the journal folder.",
                          YES_NO, "no") == "yes":
                self._start_undo(True)
            else:
                self.status("Undo cancelled. Nothing was changed.")
            return
        self.status(logic.undo_message(value))
        self.load_rows()
        if self.current_row is not None:
            self.start_current()                                # the panels and boxes show what is on disk again

    # ------------------------------------------------------------------ closing
    def closeEvent(self, event) -> None:
        try:
            if self._task_dialog is not None:                   # a Tools dialog is open: stop its task, drop its result
                self._task_dialog.abort()
                self._task_dialog = None
            if self.jobs.cancel_all():                          # a save is writing files right now: let it finish
                self.status("Saving is in progress. Please wait a moment, then close the window again.")
                event.ignore()
                return
            self._stop_player()
        except Exception as exc:                                # noqa: BLE001
            log.exception("closing: %s", exc)
        event.accept()
