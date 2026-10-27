"""The Save review dialog: a read-only view of a ``SavePlan``.

It receives ready-made data only (strings and QImages that the loaders built in worker threads) and decodes nothing, so
building it never blocks the window. Every piece of text, including everything that came from the web or from file names,
is shown through plain-text labels (never rich text). Buttons: Save (the default) and Cancel; a plan with nothing to
write only offers Close.

Safety touches: when files will be replaced, Cancel (not Save) is the default and has the focus, so a stray Enter never
overwrites anything; and key auto-repeat is ignored for a short moment after the dialog opens, so an Enter that is still
held down from the main window cannot press a button.
"""
from __future__ import annotations

from PyQt5 import QtCore, QtGui, QtWidgets

from . import logic
from .widgets import CheckerLabel, plain_label

TEXT_LIMIT = 700                      # characters of one old/new value shown on screen (the plan keeps the whole value)
PREVIEW_SIZE = (200, 128)


def _clip(text: str, limit: int = TEXT_LIMIT) -> str:
    text = text if text else "(empty)"
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


class ReviewDialog(QtWidgets.QDialog):
    REPEAT_GUARD_MS = 300                 # key auto-repeat is ignored for this long after the dialog opens

    def __init__(self, plan, parent=None):
        super().__init__(parent)
        self.plan = plan
        self._opened = QtCore.QElapsedTimer()
        self._opened.start()
        self.setWindowTitle("Review before saving")
        self.setAccessibleName("Review before saving")
        self.setModal(True)
        self.save_btn = None
        outer = QtWidgets.QVBoxLayout(self)
        if plan.is_empty:
            outer.addWidget(self._heading(f"Nothing would be written for “{plan.game}”."))
            outer.addWidget(plain_label("Nothing was chosen that differs from what the game has now, so there is nothing to save. "
                                        "Step to a new candidate with ◀ ▶, or change a detail, then press Save again.", lines=2))
        else:
            outer.addWidget(self._heading(f"Check what will be saved for “{plan.game}”."))
            summary = self._heading(plan.summary())
            summary.setAccessibleName("Summary")
            outer.addWidget(summary)
            outer.addWidget(plain_label("Nothing is written until you press Save. Only the items listed here are changed.", lines=1))
        body = QtWidgets.QWidget()
        self._lay = QtWidgets.QVBoxLayout(body)
        self._lay.setContentsMargins(4, 4, 8, 4)
        if plan.pictures:
            self._section("Pictures")
            for item in plan.pictures:
                self._lay.addWidget(self._picture_row(item))
        if plan.video is not None:
            self._section("Trailer")
            self._lay.addWidget(self._video_row(plan.video))
        if plan.links:
            self._section("Will be linked")
            self._lay.addWidget(plain_label("These files are already there but are not in the game list. "
                                            "They are not changed or re-encoded; only gamelist.xml gets the entry."))
            for item in plan.links:
                self._lay.addWidget(self._link_row(item))
        if plan.texts:
            self._section("Details")
            self._lay.addLayout(self._text_grid(plan.texts))
        self._replaced_section(plan)
        self._left_alone_section(plan)
        self._lay.addStretch(1)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll.setWidget(body)
        self.scroll.setAccessibleName("What will be saved")
        outer.addWidget(self.scroll, 1)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        if plan.is_empty:
            self.cancel_btn = QtWidgets.QPushButton("Close")
            self.cancel_btn.setDefault(True)
        else:
            self.save_btn = QtWidgets.QPushButton("Save")
            self.cancel_btn = QtWidgets.QPushButton("Cancel")
            careful = bool(plan.replaced_files)              # overwriting: the safe button answers a stray Enter
            for button, default in ((self.save_btn, not careful), (self.cancel_btn, careful)):
                button.setAutoDefault(default)
                button.setDefault(default)
            buttons.addWidget(self.save_btn)
            self.save_btn.clicked.connect(self.accept)
        buttons.addWidget(self.cancel_btn)
        self.cancel_btn.clicked.connect(self.reject)
        outer.addLayout(buttons)
        (self.cancel_btn if plan.is_empty or plan.replaced_files else self.save_btn).setFocus()
        for widget in (self, self.scroll, self.cancel_btn, self.save_btn):
            if widget is not None:
                widget.installEventFilter(self)
        self._fit()

    def showEvent(self, event) -> None:
        self._opened.restart()
        super().showEvent(event)

    def eventFilter(self, obj, event) -> bool:
        """Swallow key auto-repeat during the first moments (an Enter still held down from the main window)."""
        if event.type() in (QtCore.QEvent.KeyPress, QtCore.QEvent.ShortcutOverride) and event.isAutoRepeat() \
                and self._opened.elapsed() < self.REPEAT_GUARD_MS:
            return True
        return super().eventFilter(obj, event)

    def _fit(self) -> None:
        screen = QtWidgets.QApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else QtCore.QRect(0, 0, 1366, 768)
        self.resize(min(1000, max(520, area.width() - 80)), min(900, max(360, area.height() - 120)))

    # ----- building blocks
    @staticmethod
    def _heading(text: str):
        label = plain_label(text)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def _section(self, title: str) -> None:
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Sunken)
        self._lay.addWidget(line)
        self._lay.addWidget(self._heading(title))

    @staticmethod
    def _preview(image, caption: str, checker: bool = True):
        box = QtWidgets.QVBoxLayout()
        view = CheckerLabel((60, 40), checker=checker)
        view.setFixedSize(*PREVIEW_SIZE)
        view.set_image(image)
        view.setAccessibleName(caption)
        box.addWidget(view)
        box.addWidget(plain_label(caption, lines=1), 0, QtCore.Qt.AlignHCenter)
        return box

    def _picture_row(self, item):
        row = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        if item.current is not None:
            lay.addLayout(self._preview(item.current_image, "Current"))
            lay.addWidget(plain_label("→", wrap=False))
        lay.addLayout(self._preview(item.image, "New"))
        info = QtWidgets.QVBoxLayout()
        info.addWidget(self._heading(f"{item.name} - " + ("replaces the current one" if item.current is not None else "new")))
        info.addWidget(plain_label(f"Source: {item.source} · {item.label}" if item.label else f"Source: {item.source}"))
        info.addWidget(plain_label("Size: " + item.size_text))
        if item.current is not None:
            info.addWidget(plain_label("Current: " + item.current_size_text))
        if item.warning:
            info.addWidget(plain_label("⚠ " + item.warning))
        info.addStretch(1)
        lay.addLayout(info, 1)
        return row

    def _link_row(self, item):
        row = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        if item.image is not None:
            lay.addLayout(self._preview(item.image, "Current"))
        info = QtWidgets.QVBoxLayout()
        info.addWidget(self._heading(f"{item.name} - link the existing file"))
        info.addWidget(plain_label("File: " + item.path))
        info.addWidget(plain_label("Current: " + item.current_text))
        info.addStretch(1)
        lay.addLayout(info, 1)
        return row

    def _video_row(self, item):
        row = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        if item.thumb is not None:
            lay.addLayout(self._preview(item.thumb, "New", checker=False))
        info = QtWidgets.QVBoxLayout()
        info.addWidget(self._heading("Trailer - " + ("replaces the current one" if item.current is not None else "new")))
        info.addWidget(plain_label(f"Source: {item.source} · {item.label}" if item.label else f"Source: {item.source}"))
        info.addWidget(plain_label(f"Start: {item.start_text}    End: {item.end_text}    Length: {item.length_text}"))
        if item.current is not None:
            info.addWidget(plain_label("Current video: " + item.current_text))
        info.addStretch(1)
        lay.addLayout(info, 1)
        return row

    def _text_grid(self, items):
        grid = QtWidgets.QGridLayout()
        grid.setColumnMinimumWidth(0, 120)                  # (labels have no preferred width of their own)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        for column, title in enumerate(("Field", "Current", "New")):
            grid.addWidget(self._heading(title), 0, column)
        for row, item in enumerate(items, start=1):
            grid.addWidget(plain_label(item.label), row, 0, QtCore.Qt.AlignTop)
            grid.addWidget(plain_label(_clip(item.old)), row, 1)
            grid.addWidget(plain_label(_clip(item.new)), row, 2)
        return grid

    def _replaced_section(self, plan) -> None:
        self._section("Files that will be replaced")
        if not plan.replaced_files:
            self._lay.addWidget(plain_label("No existing file will be replaced."))
        else:
            for path in plan.replaced_files:
                self._lay.addWidget(plain_label("• " + path))
            self._lay.addWidget(plain_label("The old copy of each is kept in the undo journal first, so Undo last save brings it back."))
        for path in plan.repointed_files:
            self._lay.addWidget(plain_label(f"• {path} is no longer used by the game list. The file itself stays on disk, untouched."))

    def _left_alone_section(self, plan) -> None:
        self._section("Left as they are")
        if not plan.left_alone:
            self._lay.addWidget(plain_label("Everything is being written."))
        for item in plan.left_alone:
            self._lay.addWidget(plain_label(f"• {item.name}: {item.reason}"))
