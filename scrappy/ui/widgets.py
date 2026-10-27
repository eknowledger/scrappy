"""Reusable widgets: a checkerboard preview, a picture panel with candidates, and a video panel.

Rules kept in every widget here:
* Text that came from the web (labels, titles, error messages) is shown only through ``QLabel`` set to ``Qt.PlainText``
  or through combo/list items, never as rich text. Message boxes are made plain too (``ask_choice``).
* Anything slow (download, decode, convert) runs in a worker; the GUI thread only receives a small ``QImage``
  (at most the theme's own sizes, or a 320 px thumbnail) and paints it.
* Signal handlers go through ``safe`` and event overrides through ``guarded_event``: an exception is shown in the status
  line instead of aborting the program.
"""
from __future__ import annotations

from collections import OrderedDict

from PyQt5 import QtCore, QtGui, QtWidgets

from ..models import AssetCandidate
from . import logic
from .jobs import guarded_event, safe
from .pipeline import decode_rgba, make_preview
from .plan import PicturePick, VideoPick

MAX_DROPPED = 6                       # items taken from one drop (each starts a background job)
NOUNS = {"logo": "logo", "boxart": "box art", "image": "image"}
CACHE_SIZE = 6                       # processed pictures kept per panel (each is a theme-sized result, well under 1 MB)


class FitLabel(QtWidgets.QLabel):
    """A plain-text, word-wrapped label whose minimum height follows its text, so a long message is never clipped and a short
    one takes one line. (The minimum is only recomputed once the label has a real width.)"""

    def __init__(self, text: str = "", lines: int = 1):
        super().__init__(text)
        self._lines = max(1, lines)
        self.setTextFormat(QtCore.Qt.PlainText)
        self.setWordWrap(True)
        self.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self._fit()

    def _fit(self) -> None:
        line = self.fontMetrics().lineSpacing()
        need = line * self._lines + 4
        if self.width() >= 120:
            rect = self.fontMetrics().boundingRect(0, 0, self.width(), 10000, QtCore.Qt.TextWordWrap, self.text())
            need = max(need, rect.height() + 4)
        if self.minimumHeight() != need:
            self.setMinimumHeight(need)

    def setText(self, text) -> None:
        super().setText(text)
        self._fit()

    @guarded_event
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()


def plain_label(text: str = "", wrap: bool = True, lines: int = 1) -> QtWidgets.QLabel:
    """A label that never interprets its text as HTML and is tall enough for its text (at least `lines` lines)."""
    label = FitLabel(text, lines)
    label.setWordWrap(wrap)
    return label


def ask_choice(parent, title: str, text: str, choices, default: str):
    """Modal question with plain text. `choices`: [(key, label, role)] with role 'accept', 'reject' or 'destructive'.
    Returns the key of the pressed button (the 'reject' one when the box is closed)."""
    roles = {"accept": QtWidgets.QMessageBox.AcceptRole, "reject": QtWidgets.QMessageBox.RejectRole,
             "destructive": QtWidgets.QMessageBox.DestructiveRole}
    box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Question, title, text, QtWidgets.QMessageBox.NoButton, parent)
    box.setTextFormat(QtCore.Qt.PlainText)
    buttons, fallback = {}, None
    for key, label, role in choices:
        button = box.addButton(label, roles[role])
        buttons[button] = key
        if key == default:
            box.setDefaultButton(button)
        if role == "reject":
            box.setEscapeButton(button)
            fallback = key
    box.exec_()
    return buttons.get(box.clickedButton(), fallback)


def show_message(parent, title: str, text: str, error: bool = False) -> None:
    box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Critical if error else QtWidgets.QMessageBox.Information,
                                title, text, QtWidgets.QMessageBox.Ok, parent)
    box.setTextFormat(QtCore.Qt.PlainText)
    box.exec_()


def to_qimage(data: bytes) -> QtGui.QImage:
    """A QImage of small picture bytes, built from raw pixels that Pillow decoded: Qt's own image plugins (libjpeg!) are never
    used. QImage (unlike QPixmap) may be created on a worker thread; ``copy()`` makes it own its pixels."""
    width, height, raw = decode_rgba(data)
    image = QtGui.QImage(raw, width, height, width * 4, QtGui.QImage.Format_RGBA8888).copy()
    if image.isNull():
        raise ValueError("The picture could not be shown.")
    return image


def hand_made_candidate(slot: str, path: str) -> AssetCandidate:
    """Fallback when the controller has no ``local_candidate``: the 'your file' candidate built by hand."""
    return AssetCandidate(slot=slot, url="file://" + path, label=path.rsplit("/", 1)[-1], source="your file")


class CheckerLabel(QtWidgets.QWidget):
    """Shows a picture over a checkerboard so that transparency is visible."""

    def __init__(self, min_size=(160, 80), checker: bool = True):
        super().__init__()
        self.setMinimumSize(*min_size)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self._checker = checker
        self._pixmap = None
        self._scaled = None                    # (size, pixmap)
        self._tile_pixmap = None

    def sizeHint(self):
        return QtCore.QSize(320, 200)

    def set_image(self, image) -> None:
        """`image`: a QImage (already small) or None."""
        self._pixmap = None if image is None else QtGui.QPixmap.fromImage(image)
        self._scaled = None
        self.update()

    def has_image(self) -> bool:
        return self._pixmap is not None

    def image_rect(self) -> QtCore.QRect:
        """Where the picture is drawn (centred, as large as fits)."""
        if self._pixmap is None:
            return QtCore.QRect()
        w, h = logic.fit_within(self._pixmap.width(), self._pixmap.height(), self.width() - 4, self.height() - 4)
        return QtCore.QRect((self.width() - w) // 2, (self.height() - h) // 2, w, h)

    def _tile(self) -> QtGui.QPixmap:
        if self._tile_pixmap is None:
            s = logic.CHECKER_SQUARE
            tile = QtGui.QPixmap(2 * s, 2 * s)
            p = QtGui.QPainter(tile)
            try:
                for ty in range(2):
                    for tx in range(2):
                        g = logic.checker_shade(tx * s, ty * s, s)
                        p.fillRect(tx * s, ty * s, s, s, QtGui.QColor(g, g, g))
            finally:
                p.end()
            self._tile_pixmap = tile
        return self._tile_pixmap

    @guarded_event
    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        try:
            p.fillRect(self.rect(), self.palette().base())
            if self._pixmap is None:
                p.setPen(self.palette().mid().color())
                p.drawRect(self.rect().adjusted(0, 0, -1, -1))
                return
            target = self.image_rect()
            if self._checker:
                p.drawTiledPixmap(target, self._tile())
            if self._scaled is None or self._scaled[0] != target.size():
                scaled = self._pixmap.scaled(target.size(), QtCore.Qt.IgnoreAspectRatio, QtCore.Qt.SmoothTransformation)
                self._scaled = (target.size(), scaled)
            p.drawPixmap(target.topLeft(), self._scaled[1])
        finally:
            p.end()


class FoundHint(QtWidgets.QWidget):
    """The line under a text box that offers the value a search found: 'found: ...' and a Use button. Hidden when the
    search found nothing different. Plain text only; the full value is in the (plain) tooltip."""
    use_clicked = QtCore.pyqtSignal()
    SHOWN_CHARS = 70

    def __init__(self, field_name: str):
        super().__init__()
        self.field_name = field_name
        self.label = QtWidgets.QLabel()
        self.label.setTextFormat(QtCore.Qt.PlainText)
        self.label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        font = self.label.font()
        font.setItalic(True)
        self.label.setFont(font)
        self.button = QtWidgets.QPushButton("Use")
        self.button.setMaximumWidth(56)
        self.button.clicked.connect(safe(self.use_clicked.emit))
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.label, 1)
        lay.addWidget(self.button)
        self.hide()

    def show_value(self, value: str) -> None:
        """`value`: the found text as the box would show it."""
        one_line = logic.one_line(value, self.SHOWN_CHARS)
        self.label.setText("found: " + one_line)
        self.label.setToolTip(logic.plain_tooltip(value))
        self.button.setAccessibleName(f"Use the found {self.field_name}")
        self.button.setToolTip(logic.plain_tooltip(f"Replace the {self.field_name} above with: {value}"))
        self.show()

    def clear(self) -> None:
        self.hide()


class AssetPanel(QtWidgets.QGroupBox):
    """One picture slot: preview of the processed result, a candidate stepper, paste link / choose file / web search.

    `loader(cand, remove_bg, local_ok, cancelled) -> Processed` does the slow part and runs in a worker.

    The candidate list is [Current] + found: Current (what the game has now, ``set_current``) is entry 0 when there is
    one. It is shown from a ready-made image, never goes through the loader, and ``current_output`` is None while it is
    displayed, so it can never be written.
    """
    changed = QtCore.pyqtSignal()
    text_logo_requested = QtCore.pyqtSignal()
    web_requested = QtCore.pyqtSignal(str)
    message = QtCore.pyqtSignal(str)

    def __init__(self, title, slot, jobs, loader, logo_tools=False, local_factory=None, guard=None):
        super().__init__(title)
        self.slot, self.jobs, self.loader = slot, jobs, loader
        self.guard = guard or (lambda: "")                   # returns a plain message when nothing may be added right now
        self.local_factory = local_factory or hand_made_candidate
        self.setAcceptDrops(True)
        self.candidates, self.index = [], -1
        self.out, self.note, self.warning = None, "", ""
        self.loading = False
        self.local_urls = set()                           # addresses the user chose in a dialog or dropped: the only ones read from disk
        self._cache = OrderedDict()                       # (url, remove_bg) -> (Processed, QImage)
        self._job = None
        self._direct = False
        self._direct_cand = None                          # stands for the generated text logo in ``pick``
        self._current = None                              # CurrentAsset of the game, or None
        self._current_image = None                        # its ready preview (QImage, made in a worker)
        self._current_known = False                       # True once the game's files were read (None then means "no file")
        self._image = None                                # the QImage on screen

        self.preview = CheckerLabel()
        self.info = plain_label("", lines=1)
        self.use_box = QtWidgets.QCheckBox("Include")
        self.use_box.setChecked(True)
        self.use_box.toggled.connect(safe(self.changed.emit))
        self.prev_btn, self.next_btn = QtWidgets.QPushButton("◀"), QtWidgets.QPushButton("▶")
        for button, tip in ((self.prev_btn, "Previous candidate"), (self.next_btn, "Next candidate")):
            button.setToolTip(tip)
            button.setAccessibleName(f"{title}: {tip}")
            button.setMaximumWidth(48)
        link, file_, web = (QtWidgets.QPushButton("Paste link…"), QtWidgets.QPushButton("Choose file…"),
                            QtWidgets.QPushButton("Web search"))
        web.setToolTip("Open an image search for this game in a browser, then use Paste link… with the picture's address")
        self.prev_btn.clicked.connect(safe(lambda: self.step(-1)))
        self.next_btn.clicked.connect(safe(lambda: self.step(1)))
        link.clicked.connect(safe(self.add_link))
        file_.clicked.connect(safe(self.add_file))
        web.clicked.connect(safe(lambda: self.web_requested.emit(self.slot)))
        row1 = QtWidgets.QHBoxLayout()
        row1.addWidget(self.prev_btn)
        row1.addWidget(self.next_btn)
        row1.addStretch(1)
        row1.addWidget(self.use_box)
        row2 = QtWidgets.QHBoxLayout()
        for w in (link, file_, web):
            row2.addWidget(w)
        row2.addStretch(1)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(self.preview, 1)
        lay.addWidget(self.info)
        self.remove_bg = None
        if logo_tools:
            self.remove_bg = QtWidgets.QCheckBox("Remove background")
            self.remove_bg.setToolTip("For logos on a solid colour: makes that colour transparent")
            self.remove_bg.toggled.connect(safe(self.reprocess))
            text_btn = QtWidgets.QPushButton("Make text logo")
            text_btn.clicked.connect(safe(self.text_logo_requested.emit))
            row3 = QtWidgets.QHBoxLayout()
            row3.addWidget(self.remove_bg, 1)
            row3.addWidget(text_btn)
            lay.addLayout(row3)
        lay.addLayout(row1)
        lay.addLayout(row2)
        self._show_empty()

    # ----- state
    @property
    def busy(self) -> bool:
        return self.loading

    def remove_bg_on(self) -> bool:
        return bool(self.remove_bg is not None and self.remove_bg.isChecked())

    @property
    def shows_current(self) -> bool:
        """True while the candidate on screen is what the game has now (the generated text logo never is)."""
        return 0 <= self.index < len(self.candidates) and bool(self.candidates[self.index].current) and not self._direct

    @property
    def current_image(self):
        return self._current_image

    def current_output(self):
        """The bytes to save, or None (nothing usable, 'Include' is unticked, or Current is showing: nothing new)."""
        return self.out if self.use_box.isChecked() and not self.shows_current else None

    def pick(self) -> PicturePick:
        """What this panel shows, for the Save plan."""
        cand = self._direct_cand if self._direct else (self.candidates[self.index] if 0 <= self.index < len(self.candidates) else None)
        return PicturePick(slot=self.slot, included=self.use_box.isChecked(), candidate=cand, data=self.out, note=self.note,
                           warning=self.warning, image=self._image)

    # ----- candidates
    def _cancel_load(self) -> None:
        if self._job is not None:
            self._job.cancel()
            self._job = None
        self.loading = False

    def clear(self) -> None:
        """Back to the empty state for the next game, including the per-game choices (Include, Remove background)."""
        self._cancel_load()
        self.candidates, self.index, self._direct, self._direct_cand = [], -1, False, None
        self._current, self._current_image, self._current_known = None, None, False
        for box in (self.use_box, self.remove_bg):
            if box is not None:
                box.blockSignals(True)
                box.setChecked(box is self.use_box)
                box.blockSignals(False)
        self._show_empty()

    def set_current(self, asset, image) -> None:
        """What the game has now (a ``CurrentAsset`` and its ready preview QImage), or None when it has no such file. The
        Current entry goes first; candidates found so far stay behind it, and the panel returns to Current."""
        self._cancel_load()
        self._current, self._current_image, self._current_known = asset, image if asset is not None else None, True
        self.candidates = self._with_current([c for c in self.candidates if not c.current])
        self.index, self._direct = (0 if self.candidates else -1), False
        self._show()

    def _with_current(self, found) -> list:
        return ([self._current.candidate] if self._current is not None else []) + list(found)

    def set_candidates(self, candidates) -> None:
        """Candidates found by a search. They are added behind Current; with a Current the panel stays on it (the view is
        not replaced, step with the arrows), without one it shows the first."""
        self._cancel_load()
        self.candidates, self._direct = self._with_current(candidates), False
        self.index = 0 if self.candidates else -1
        self._show()

    def step(self, delta: int) -> None:
        n = len(self.candidates)
        if n > 1 or (n == 1 and self._direct):
            self.index = (self.index + delta) % n if self.index >= 0 else 0
            self._direct = False
            self._show()

    def add_candidate(self, candidate, local: bool = False) -> None:
        if local:
            self.local_urls.add(candidate.url)
        self.candidates.append(candidate)
        self.index = len(self.candidates) - 1
        self._direct = False
        self._show()

    def _allowed(self) -> bool:
        reason = self.guard()
        if reason:
            self.message.emit(reason)
        return not reason

    def add_link(self) -> None:
        if not self._allowed():
            return
        text, ok = QtWidgets.QInputDialog.getText(self, "Paste link", "Link to a picture (https://… .png / .jpg):")
        if ok and text.strip():
            self.add_link_text(text)

    def add_link_text(self, text: str) -> None:
        if not self._allowed():
            return
        try:
            link = logic.normalize_link(text)
        except ValueError as exc:
            self.message.emit(str(exc))
            return
        self.add_candidate(AssetCandidate(slot=self.slot, url=link, label="your link", source="you"))

    def add_file(self) -> None:
        if not self._allowed():
            return
        path = QtWidgets.QFileDialog.getOpenFileName(self, "Choose a picture", QtCore.QDir.homePath(),
                                                     "Pictures (*.png *.jpg *.jpeg *.webp *.bmp *.gif)")[0]
        if path:
            self.add_file_path(path)

    def add_file_path(self, path: str) -> None:
        """A picture the user chose or dropped. The candidate is made in a worker (the controller checks the file)."""
        if not self._allowed():
            return
        if not path.startswith("/"):
            self.message.emit("Choose a file by its full path.")
            return
        slot, factory = self.slot, self.local_factory
        self.jobs.start("localfile", lambda job: factory(slot, path), lambda cand: self.add_candidate(cand, local=True),
                        lambda msg: self.message.emit("Could not use that file: " + msg))

    @guarded_event
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    @guarded_event
    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) > MAX_DROPPED:
            self.message.emit(f"Only the first {MAX_DROPPED} dropped items were used.")
        for url in urls[:MAX_DROPPED]:
            if url.isLocalFile():
                self.add_file_path(url.toLocalFile())
            elif url.scheme().lower() in ("http", "https"):
                self.add_link_text(url.toString())
        event.acceptProposedAction()

    def set_direct(self, processed, image, label: str = "generated") -> None:
        """Use a ready-made result (the generated text logo) instead of a candidate."""
        self._cancel_load()
        self._direct = True
        self._direct_cand = AssetCandidate(slot=self.slot, url="generated://text-logo", label=label, source="generated")
        self.out, self.note, self.warning = processed.data, processed.note, processed.warning
        self._image = image
        self.preview.set_image(image)
        self.info.setText(f"{label} · {processed.note}")
        self.changed.emit()

    # ----- loading and processing
    def _show_empty(self) -> None:
        self.out, self.note, self.warning = None, "", ""
        self._image = None
        self.preview.set_image(None)
        how = "Search for the game, or use Paste link…, Choose file… or drop a file here."
        self.info.setText(f"Current: none - the game has no {NOUNS.get(self.slot, self.slot)} yet. {how}" if self._current_known
                          else "Nothing found yet. " + how)
        self.changed.emit()

    def _show_current(self, cand) -> None:
        """The game's own file, from the ready preview: no loader, nothing to write."""
        self.out, self.note, self.warning = None, "", ""
        self._image = self._current_image
        self.preview.set_image(self._current_image)
        self.info.setText(logic.current_caption(self._current, self.index, len(self.candidates)))
        self.info.setToolTip(logic.plain_tooltip(self._current.path))
        self.changed.emit()

    def _key(self, cand) -> tuple:
        return (cand.url, self.remove_bg_on())

    def reprocess(self) -> None:
        if not self._direct and 0 <= self.index < len(self.candidates):
            self._show()

    def _show(self) -> None:
        self._cancel_load()
        if self.index < 0 or self.index >= len(self.candidates):
            self._show_empty()
            return
        cand = self.candidates[self.index]
        if cand.current:
            self._show_current(cand)
            return
        self.info.setToolTip("")
        key = self._key(cand)
        if key in self._cache:
            self._cache.move_to_end(key)
            self._apply(cand, *self._cache[key])
            return
        self.out, self.warning = None, ""
        self._image = None
        self.preview.set_image(None)
        self.loading = True
        self.info.setText(f"Loading {cand.label or cand.url[:60]}…")
        remove_bg, local_ok = key[1], cand.url in self.local_urls
        loader = self.loader

        def work(job):
            processed = loader(cand, remove_bg, local_ok, job.cancelled)
            return processed, to_qimage(processed.data)

        self._job = self.jobs.start("picture", work, lambda result, k=key: self._loaded(k, result),
                                    lambda msg, k=key: self._failed(k, msg), label=cand.label or cand.url[:80])
        self.changed.emit()

    def _is_current(self, key) -> bool:
        return 0 <= self.index < len(self.candidates) and self._key(self.candidates[self.index]) == key

    def _loaded(self, key, result) -> None:
        processed, image = result
        self._cache[key] = (processed, image)
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        if self._is_current(key):
            self.loading = False
            self._apply(self.candidates[self.index], processed, image)

    def _failed(self, key, msg: str) -> None:
        if not self._is_current(key):
            return
        self.loading = False
        self.out, self.note, self.warning = None, "", ""
        self._image = None
        self.preview.set_image(None)
        hint = "  Use ◀ ▶ for the next one." if len(self.candidates) > 1 else ""
        label = self.candidates[self.index].label or "picture"
        self.info.setText(f"Could not use this one ({label}): {msg}{hint}")
        self.message.emit(f"Could not use “{label}” for the {NOUNS.get(self.slot, self.slot)}: {msg}")
        self.changed.emit()

    def _apply(self, cand, processed, image) -> None:
        self.loading = False
        self.out, self.note, self.warning = processed.data, processed.note, processed.warning
        self._image = image
        self.preview.set_image(image)
        caption = ("New - " if self._current is not None else "") + logic.candidate_caption(
            cand, processed.note, self.index, len(self.candidates))
        self.info.setText(("⚠ " + processed.warning + "\n" if processed.warning else "") + caption)
        self.changed.emit()


class VideoPanel(QtWidgets.QGroupBox):
    """Trailer choice: candidate list with a thumbnail, start/end, preview in an external player, or a video file of your own.

    The first entry is "Current video" when the game has one (``set_current``); it is the default after a search, shows its
    length and size, and ``selection()`` is None while it is chosen, so it can never be encoded or written."""
    changed = QtCore.pyqtSignal()
    message = QtCore.pyqtSignal(str)
    install_requested = QtCore.pyqtSignal()                # the "Install YouTube downloader" button was pressed

    def __init__(self, default_seconds, max_seconds, jobs, fetch, preview_cb, local_factory=None, guard=None):
        super().__init__("Trailer / video")
        self.jobs, self.fetch, self.preview_cb = jobs, fetch, preview_cb
        self.local_factory = local_factory or hand_made_candidate
        self.guard = guard or (lambda: "")
        self.local_urls = set()                           # video files the user chose: the only file:// addresses we play
        self.max_seconds = max_seconds
        self.default_seconds = min(default_seconds, max_seconds)
        self._thumbs = OrderedDict()
        self._thumb_job = None
        self._current = None                              # CurrentAsset of the game's video, or None
        self._current_known = False                       # True once the game's files were read
        self.combo = QtWidgets.QComboBox()
        self.combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.combo.setMinimumContentsLength(24)
        self.thumb = CheckerLabel((128, 72), checker=False)
        self.start = QtWidgets.QDoubleSpinBox()
        self.end = QtWidgets.QDoubleSpinBox()
        for s in (self.start, self.end):
            s.setRange(0, 3600)
            s.setDecimals(1)
            s.setSuffix(" s")
        self.end.setValue(self.default_seconds)
        self.whole = QtWidgets.QCheckBox(f"Whole trailer (cut at {int(max_seconds)} s)")
        self.use_box = QtWidgets.QCheckBox("Include video")
        self.use_box.setChecked(True)
        self.summary = plain_label("", lines=1)
        self.note = plain_label("No video found yet. Search finds Steam trailers and YouTube videos.", lines=1)
        self.install_btn = QtWidgets.QPushButton("Install YouTube downloader")
        self.install_btn.setToolTip("YouTube videos are found with a small helper program (about 3 MB) that is not installed yet. "
                                    "Press to download it from its official GitHub page.")
        self.install_btn.hide()                           # shown only while the downloader is missing (set_installer_missing)
        self.preview_btn = QtWidgets.QPushButton("Preview…")
        self.preview_btn.setToolTip("Play the chosen part in an external player (mpv)")
        self.file_btn = QtWidgets.QPushButton("Choose file…")
        self.file_btn.setToolTip("Use a video file from this computer")
        times = QtWidgets.QFormLayout()
        times.addRow("Start", self.start)
        times.addRow("End", self.end)
        side = QtWidgets.QVBoxLayout()
        side.addLayout(times)
        side.addWidget(self.whole)
        middle = QtWidgets.QHBoxLayout()
        middle.addWidget(self.thumb, 1)
        middle.addLayout(side, 1)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.preview_btn)
        row.addWidget(self.file_btn)
        row.addStretch(1)
        row.addWidget(self.use_box)
        top = QtWidgets.QFormLayout()
        top.addRow("Video", self.combo)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(middle, 1)
        lay.addWidget(self.summary)
        lay.addLayout(row)
        hint_row = QtWidgets.QHBoxLayout()                # the hint line, with the install button beside it when it is needed
        hint_row.addWidget(self.note, 1)
        hint_row.addWidget(self.install_btn, 0, QtCore.Qt.AlignTop)
        lay.addLayout(hint_row)
        self.install_btn.clicked.connect(safe(self.install_requested.emit))
        self.preview_btn.clicked.connect(safe(self._preview))
        self.file_btn.clicked.connect(safe(self.add_file))
        self.combo.currentIndexChanged.connect(safe(self._choice_changed))
        self.start.valueChanged.connect(safe(self._range_changed))
        self.end.valueChanged.connect(safe(self._range_changed))
        self.whole.toggled.connect(safe(self._range_changed))
        self.use_box.toggled.connect(safe(self.changed.emit))
        self._range_changed()

    def set_installer_missing(self, missing: bool) -> None:
        """Offer the Install button next to the hint line while the YouTube downloader is not installed."""
        self.install_btn.setVisible(bool(missing))

    # ----- candidates
    def set_current(self, asset) -> None:
        """What the game has now (a ``CurrentAsset``), or None when it has no video. Goes first; the videos found so far stay."""
        self._current, self._current_known = asset, True
        self._fill([c for c in self._items() if not c.current])

    def set_candidates(self, candidates, empty_note: str = "") -> None:
        """Videos found by a search, behind Current (which stays chosen when there is one)."""
        self._fill(list(candidates), empty_note)

    def _items(self) -> list:
        return [self.combo.itemData(i) for i in range(self.combo.count())]

    def _fill(self, found, empty_note: str = "") -> None:
        if self._thumb_job is not None:
            self._thumb_job.cancel()
            self._thumb_job = None
        self.combo.blockSignals(True)
        self.combo.clear()
        if self._current is not None:
            where = "" if self._current.tag_linked else " - not in the game list"
            self.combo.addItem(f"Current video{where} - {logic.current_text(self._current)}", self._current.candidate)
        seen = set()
        for c in found:
            if c.url in seen:
                continue
            seen.add(c.url)
            self.combo.addItem(logic.video_label(c), c)
        self.combo.blockSignals(False)
        n = self.combo.count() - (1 if self._current is not None else 0)
        if n:
            text = f"{n} video(s) found. Choose one, set start and end, press Preview, then Save."
        elif self._current is not None:
            text = "Only the Current video so far. Search finds Steam trailers and YouTube videos."
        else:
            text = empty_note or "No video found. Try another search; pasting a trailer link is planned for a later version."
        self.note.setText("Current: none. " + text if self._current_known and self._current is None else text)
        self.combo.setCurrentIndex(0 if self.combo.count() else -1)
        self._choice_changed()

    def add_file(self) -> None:
        if self.guard():
            self.message.emit(self.guard())
            return
        path = QtWidgets.QFileDialog.getOpenFileName(self, "Choose a video", QtCore.QDir.homePath(),
                                                     "Videos (*.mp4 *.mkv *.webm *.mov *.avi *.m4v *.ts)")[0]
        if path:
            self.add_file_path(path)

    def add_file_path(self, path: str) -> None:
        reason = self.guard()
        if reason:
            self.message.emit(reason)
            return
        if not path.startswith("/"):
            self.message.emit("Choose a file by its full path.")
            return
        factory = self.local_factory
        self.jobs.start("localfile", lambda job: factory("video", path), self._add_local,
                        lambda msg: self.message.emit("Could not use that file: " + msg))

    def _add_local(self, cand) -> None:
        self.local_urls.add(cand.url)
        self.combo.addItem(logic.video_label(cand), cand)
        self.combo.setCurrentIndex(self.combo.count() - 1)
        self.use_box.setChecked(True)

    def reset(self) -> None:
        """Back to the defaults for the next game: no candidates, Include ticked, a clip from 0 s."""
        for box, value in ((self.use_box, True), (self.whole, False)):
            box.blockSignals(True)
            box.setChecked(value)
            box.blockSignals(False)
        self.start.setValue(0)
        self.end.setValue(self.default_seconds)
        self._current, self._current_known = None, False
        self.set_candidates([])

    def selected_candidate(self):
        return self.combo.currentData() if self.combo.count() else None

    def clip(self):
        """(start, end) of the chosen part for the selected video; `end` None = the whole trailer."""
        cand = self.selected_candidate()
        return logic.clip_range(self.start.value(), self.end.value(), self.whole.isChecked(), cand.duration if cand else 0.0)

    def selection(self):
        """(candidate, start, end) or None. `end` None = whole trailer."""
        cand = self.selected_candidate()
        if not self.use_box.isChecked() or cand is None or cand.current:
            return None                                       # Current is what the game has: nothing new to write
        return (cand,) + self.clip()

    def pick(self) -> VideoPick:
        """What this panel shows, for the Save plan."""
        cand = self.selected_candidate()
        start, end = self.clip()
        thumb = self._thumbs.get(cand.thumb_url) if cand is not None and cand.thumb_url else None
        return VideoPick(included=self.use_box.isChecked(), candidate=cand, start=start, end=end, thumb=thumb)

    def _choice_changed(self) -> None:
        cand = self.selected_candidate()
        if cand is not None and cand.current:
            pass                                              # the game's own video: its length is not a range to choose from
        elif cand is not None and cand.duration and cand.duration > 0:
            top = max(float(cand.duration), 1.0)
            self.start.setMaximum(top)
            self.end.setMaximum(top)
            self.end.setValue(min(self.default_seconds, top))
        else:
            self.start.setMaximum(3600)
            self.end.setMaximum(3600)
            self.start.setValue(0)
            self.end.setValue(self.default_seconds)
        self._range_changed()
        self._load_thumb(cand)
        self.changed.emit()

    def _range_changed(self) -> None:
        cand = self.selected_candidate()
        if cand is not None and cand.current and self._current is not None:
            for w in (self.start, self.end, self.whole):
                w.setEnabled(False)                           # nothing to set: the game's own video is not cut again
            if self._current.tag_linked:
                self.summary.setText(f"Current video: {logic.current_text(self._current)}. Nothing will be written for it. "
                                     "Choose another video to replace it.")
            else:
                self.summary.setText(f"Current video: {logic.current_text(self._current)}. It is not in the game list yet; "
                                     "Save links it (the video itself is not changed). Choose another video to replace it.")
            return
        self.whole.setEnabled(True)
        for w in (self.start, self.end):
            w.setEnabled(not self.whole.isChecked())
        start, end = self.clip()
        self.summary.setText(logic.clip_summary(start, end, self.max_seconds))

    def _load_thumb(self, cand) -> None:
        if self._thumb_job is not None:
            self._thumb_job.cancel()
            self._thumb_job = None
        if cand is None or not cand.thumb_url:
            self.thumb.set_image(None)
            return
        if cand.thumb_url in self._thumbs:
            self.thumb.set_image(self._thumbs[cand.thumb_url])
            return
        self.thumb.set_image(None)
        url, fetch = cand.thumb_url, self.fetch

        def work(job):
            return to_qimage(make_preview(fetch(url, local_ok=False, cancelled=job.cancelled)))

        def done(image, u=url):
            self._thumbs[u] = image
            while len(self._thumbs) > 12:
                self._thumbs.popitem(last=False)
            cur = self.selected_candidate()
            if cur is not None and cur.thumb_url == u:
                self.thumb.set_image(image)

        self._thumb_job = self.jobs.start("thumbnail", work, done, lambda _msg: None)    # a missing thumbnail is not worth a message

    def _preview(self) -> None:
        cand = self.selected_candidate()
        if cand is not None:
            self.preview_cb(cand, *self.clip())
