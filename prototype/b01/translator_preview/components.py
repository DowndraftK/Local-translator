"""Shared R6 visual components; no prototype data, tasks or model calls."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QTimer, Signal, QByteArray, QEvent, QPoint, QRectF
from PySide6.QtGui import (
    QColor, QFont, QFontDatabase, QIcon, QPalette, QPixmap, QPainter, QKeySequence,
    QShortcut, QGuiApplication, QPen, QTextCursor, QTextBlockFormat, QImageReader,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QGridLayout, QStackedWidget, QButtonGroup,
    QComboBox, QListView, QPlainTextEdit, QLineEdit, QScrollArea, QFileDialog,
    QLayout, QSizePolicy, QMenu, QInputDialog, QMessageBox, QGraphicsDropShadowEffect,
)


PALETTES = {
    "light": dict(
        shell="#F4F5F7", side="#F5F6F8", canvas="#FFFFFF", panel="#FFFFFF",
        soft="#F7F8FA", select_bg="#F2F3F6", hover="#ECEEF2", border="#E7E9EE", strong="#D6DAE2",
        text="#242833", secondary="#626B7A", muted="#6E7685", accent="#EF4D61",
        accent_hover="#DC3E52", tint="#FFF0F2", green="#38896A", green_bg="#EAF5EF",
        lavender="#EEEAFE", lavender_text="#4F427D", icon_bg="#F0F1F5",
    ),
    "dark": dict(
        shell="#151619", side="#17181C", canvas="#212328", panel="#282B31",
        soft="#25272D", select_bg="#30333B", hover="#30333B", border="#373A43", strong="#4D515D",
        text="#F0F1F5", secondary="#AFB3C1", muted="#949BAB", accent="#FF5B71",
        accent_hover="#FF7285", tint="#39262E", green="#7AC4A1", green_bg="#22392F",
        lavender="#D3CCF5", lavender_text="#353047", icon_bg="#32353D",
    ),
}

ICONS = {
    "translate": '<path d="M3 5h11M8.5 3v2M5 8c1.5 4 4.5 6.5 8 8M12 5c-.8 5-4 9-9 12M13 21l4-10 4 10M14.5 17.5h5"/>',
    "mic": '<rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11v1a7 7 0 0014 0v-1M12 19v3M8 22h8"/>',
    "history": '<path d="M3 11a9 9 0 119 10M3 4v7h7M12 7v5l3 2"/>',
    "file": '<path d="M14 3H6a2 2 0 00-2 2v14a2 2 0 002 2h12a2 2 0 002-2V9zM14 3v6h6M8 13h8M8 17h6"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="M3 16l5-5 4 4 3-3 6 6"/>',
    "shield": '<path d="M12 3L4 6v6c0 5 8 9 8 9s8-4 8-9V6zM8.5 12l2.5 2.5 4.5-5"/>',
    "chevron": '<path d="M9 5l7 7-7 7"/>',
    "back": '<path d="M15 5l-7 7 7 7"/>',
    "down": '<path d="M6 9l6 6 6-6"/>',
    "swap": '<path d="M4 7h16M16 3l4 4-4 4M20 17H4M8 13l-4 4 4 4"/>',
    "copy": '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
    "paste": '<rect x="7" y="3" width="10" height="4" rx="1"/><path d="M7 5H4v16h16V5h-3M8 12h8M8 16h6"/>',
    "close": '<path d="M6 6l12 12M18 6L6 18"/>',
    "arrow": '<path d="M4 12h16M14 6l6 6-6 6"/>',
    "upload": '<path d="M12 16V3M7 8l5-5 5 5M4 15v6h16v-6"/>',
    "download": '<path d="M12 3v13M7 11l5 5 5-5M4 16v5h16v-5"/>',
    "settings": '<path d="M9 3h6l.6 3 2.6 1.5 2.9-.9 3 5.2-2.3 2.1v3l2.3 2.1-3 5.2-2.9-.9-2.6 1.5L15 28H9l-.6-3-2.6-1.5-2.9.9-3-5.2L2.2 17v-3L-.1 11.9l3-5.2 2.9.9L8.4 6z" transform="translate(3 0) scale(.75)"/><circle cx="12" cy="11.7" r="3"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5"/>',
    "moon": '<path d="M20.5 13.5A9 9 0 0110.5 3a9 9 0 1010 10.5z"/>',
    "system": '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4M12 4v13"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M16 16l5 5"/>',
    "headphones": '<path d="M4 14v-3a8 8 0 0116 0v3M4 12H2v8h5v-8H4M20 12h2v8h-5v-8h3"/>',
    "book": '<path d="M12 5C8 2 3 3 3 3v16s5-1 9 2c4-3 9-2 9-2V3s-5-1-9 2zM12 5v16"/>',
    "scan": '<path d="M8 3H3v5M16 3h5v5M3 16v5h5M21 16v5h-5M2 12h20"/>',
    "bookmark": '<path d="M6 3h12v18l-6-4-6 4z"/>',
    "check": '<path d="M5 12l4 4L19 6"/>',
    "play": '<path d="M8 4l13 8-13 8z"/>',
    "pause": '<path d="M8 5v14M16 5v14"/>',
    "wave": '<path d="M3 10v4M7 6v12M12 2v20M17 6v12M21 10v4"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
    "folder": '<path d="M3 6a2 2 0 012-2h5l2 3h7a2 2 0 012 2v11H3z"/>',
    "trash": '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
    "dots": '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
}


def icon(name: str, color: str, size: int = 22) -> QIcon:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
           f'viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.65" '
           f'stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>')
    pix = QPixmap(size * 2, size * 2)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    QSvgRenderer(QByteArray(svg.encode())).render(painter)
    painter.end()
    pix.setDevicePixelRatio(2)
    return QIcon(pix)


def label(text: str, kind: str = "", wrap: bool = False) -> QLabel:
    w = QLabel(text)
    w.setTextFormat(Qt.PlainText)
    w.setProperty("kind", kind)
    w.setWordWrap(wrap)
    return w


def frame(kind: str = "") -> QFrame:
    w = QFrame()
    w.setProperty("kind", kind)
    return w


def vbox(widget=None, margins=(0, 0, 0, 0), spacing=0):
    box = QVBoxLayout(widget)
    box.setContentsMargins(*margins)
    box.setSpacing(spacing)
    return box


def hbox(widget=None, margins=(0, 0, 0, 0), spacing=0):
    box = QHBoxLayout(widget)
    box.setContentsMargins(*margins)
    box.setSpacing(spacing)
    return box


def scrollable(page):
    area = QScrollArea()
    area.setFrameShape(QFrame.NoFrame)
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    page.layout().setSizeConstraint(QLayout.SetMinimumSize)
    area.setWidget(page)
    return area


class IconLabel(QLabel):
    def __init__(self, name, size=22, role="secondary"):
        super().__init__()
        self.name, self.icon_size, self.role = name, size, role
        self.setFixedSize(size, size)

    def recolor(self, colors):
        self.setPixmap(icon(self.name, colors.get(self.role, self.role), self.icon_size).pixmap(QSize(self.icon_size, self.icon_size)))


class ElidedLabel(QLabel):
    def __init__(self, text, kind="tiny"):
        super().__init__(text)
        self.full_text = text
        self.setTextFormat(Qt.PlainText)
        self.setProperty("kind", kind)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setToolTip(text)

    def minimumSizeHint(self):
        return QSize(0, self.fontMetrics().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setText(self.fontMetrics().elidedText(self.full_text, Qt.ElideRight, self.width()))


class Button(QPushButton):
    def __init__(self, text="", symbol=None, kind="secondary", callback=None):
        super().__init__(text)
        self.symbol = symbol
        self.setProperty("kind", kind)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(38)
        self.setIconSize(QSize(18, 18))
        if callback:
            self.clicked.connect(callback)

    def recolor(self, colors):
        if self.symbol:
            kind = self.property("kind")
            role = "#FFFFFF" if kind == "primary" else colors["accent"] if self.isChecked() else colors["secondary"]
            if self.property("icon_role"):
                role = colors[self.property("icon_role")]
            self.setIcon(icon(self.symbol, role, 20))


class ComboPopup(QFrame):
    """One rounded, non-native menu shared by every select in the preview."""
    def __init__(self, combo):
        super().__init__(combo, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.combo = combo
        self.setAccessibleName("下拉选项")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoMouseReplay)
        self.setObjectName("selectPopup")
        self.list = QListView(self)
        self.list.setObjectName("selectOptions")
        self.list.setAccessibleName("可选项")
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setEditTriggers(QListView.NoEditTriggers)
        self.list.setUniformItemSizes(True)
        self.list.setMouseTracking(True)
        self.list.installEventFilter(self)
        vbox(self, (6, 6, 6, 6)).addWidget(self.list)
        self.list.clicked.connect(self.choose)
        self.list.activated.connect(self.choose)

    def paintEvent(self, event):
        c = QApplication.instance().property("translator_colors") or PALETTES["light"]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(c["soft"]))
        painter.setPen(QPen(QColor(c["strong"]), 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 12, 12)

    def choose(self, index):
        if not index.isValid() or not index.flags() & Qt.ItemIsEnabled:
            return
        self.combo.hidePopup()
        self.combo.window().activateWindow()
        self.combo.setFocus(Qt.PopupFocusReason)
        self.combo.setCurrentIndex(index.row())
        self.combo.record_popup('choose', index=index.row())
        self.combo.activated.emit(index.row())
        self.combo.textActivated.emit(self.combo.currentText())

    def eventFilter(self, watched, event):
        if event.type() == QEvent.ShortcutOverride and event.key() in (
                Qt.Key_Escape, Qt.Key_Tab, Qt.Key_Backtab, Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            event.accept()
            return True
        if event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Escape, Qt.Key_Tab, Qt.Key_Backtab):
                self.combo.record_popup('key', key=event.key(), modifiers=int(event.modifiers().value))
                self.combo.hidePopup()
                self.combo.window().activateWindow()
                self.combo.setFocus(Qt.PopupFocusReason)
                if event.key() != Qt.Key_Escape:
                    self.combo.focusNextPrevChild(event.key() != Qt.Key_Backtab)
                return True
            if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
                self.choose(self.list.currentIndex())
                return True
        return super().eventFilter(watched, event)


class Combo(QComboBox):
    """Rounded selects and popups with the same behavior in both themes."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.popup = ComboPopup(self)
        self.popup_events = []
        self.setCursor(Qt.PointingHandCursor)

    def record_popup(self, action, **fields):
        self.popup_events.append(dict(action=action, at=time.monotonic(), **fields))
        self.popup_events[:] = self.popup_events[-64:]

    def showPopup(self):
        if not self.isEnabled() or not self.count():
            return
        view = self.popup.list
        self.popup.setWindowTitle(self.accessibleName() or self.currentText() or "下拉选项")
        view.setModel(self.model())
        view.setRootIndex(self.rootModelIndex())
        view.setModelColumn(self.modelColumn())
        view.setCurrentIndex(self.model().index(self.currentIndex(), self.modelColumn(), self.rootModelIndex()))
        anchor = self.mapToGlobal(QPoint(0, self.height() + 6))
        screen = QGuiApplication.screenAt(anchor) or self.screen()
        available = screen.availableGeometry().adjusted(8, 8, -8, -8)
        width = min(available.width(), max(self.width(), max(self.fontMetrics().horizontalAdvance(self.itemText(i)) for i in range(self.count())) + 48))
        height = min(self.count() * max(36, view.sizeHintForRow(0)) + 12, 280, available.height())
        x = max(available.left(), min(anchor.x(), available.right() - width + 1))
        y = anchor.y()
        if y + height > available.bottom() + 1:
            y = self.mapToGlobal(QPoint(0, -6)).y() - height
        y = max(available.top(), min(y, available.bottom() - height + 1))
        self.popup.setGeometry(x, y, width, height)
        self.popup.show()
        view.setFocus(Qt.PopupFocusReason)
        view.scrollTo(view.currentIndex())
        self.record_popup('open', index=self.currentIndex(), visible=self.popup.isVisible())

    def hidePopup(self):
        self.record_popup('close', index=self.currentIndex(), visible=self.popup.isVisible())
        self.popup.hide()
        super().hidePopup()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = self.palette().color(QPalette.Text if self.isEnabled() else QPalette.PlaceholderText)
        painter.setPen(QPen(color, 1.35, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        x, y = self.width() - 14, self.height() // 2
        painter.drawLine(x - 3, y - 1, x, y + 2)
        painter.drawLine(x, y + 2, x + 3, y - 1)
        painter.end()


class ReadingEdit(QPlainTextEdit):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._placeholder = self.placeholderText()
        self._preediting = False

    def setPlaceholderText(self, text):
        self._placeholder = text
        super().setPlaceholderText("" if self._preediting else text)

    def _set_preediting(self, active):
        if active != self._preediting:
            self._preediting = active
            super().setPlaceholderText("" if active else self._placeholder)

    def inputMethodEvent(self, event):
        # Qt's document remains empty until the input method commits its text.
        self._set_preediting(bool(event.preeditString()))
        super().inputMethodEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._set_preediting(False)

    def setPlainText(self, text):
        super().setPlainText(text)
        cursor = QTextCursor(self.document())
        cursor.select(QTextCursor.Document)
        block = QTextBlockFormat()
        block.setLineHeight(140, QTextBlockFormat.ProportionalHeight.value)
        cursor.mergeBlockFormat(block)
        self.document().setModified(False)


class DropZone(QFrame):
    fileDropped = Signal(str)
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setProperty("kind", "drop")
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and len(event.mimeData().urls()) == 1 and event.mimeData().urls()[0].isLocalFile():
            event.acceptProposedAction()
            self.setProperty("dragging", True)
            self.style().unpolish(self)
            self.style().polish(self)

    def dragLeaveEvent(self, event):
        self.setProperty("dragging", False)
        self.style().unpolish(self)
        self.style().polish(self)

    def dropEvent(self, event):
        self.dragLeaveEvent(event)
        if event.mimeData().hasUrls() and event.mimeData().urls()[0].isLocalFile():
            self.fileDropped.emit(event.mimeData().urls()[0].toLocalFile())
            event.acceptProposedAction()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ImagePreview(QWidget):
    """Paint only within a reserved canvas; image size never changes the layout."""
    def __init__(self):
        super().__init__()
        self.original = QPixmap()
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(180, 160)
        self.setAccessibleName("待翻译图片预览")

    def set_image(self, pixmap):
        self.original = pixmap
        self.fit_image()

    def fit_image(self):
        self.update()

    def sizeHint(self):
        return QSize(560, 320)

    def paintEvent(self, event):
        c = QApplication.instance().property("translator_colors") or PALETTES["light"]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.setBrush(QColor(c["panel"]))
        painter.setPen(QPen(QColor(c["border"]), 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 10, 10)
        available = self.rect().adjusted(16, 16, -16, -16)
        if not self.original.isNull() and available.width() > 0 and available.height() > 0:
            fitted = self.original.size().scaled(available.size(), Qt.KeepAspectRatio)
            target = QRectF(available.x() + (available.width() - fitted.width()) / 2,
                            available.y() + (available.height() - fitted.height()) / 2,
                            fitted.width(), fitted.height())
            painter.drawPixmap(target, self.original, QRectF(self.original.rect()))

    def clear(self):
        self.original = QPixmap()
        self.update()



def stylesheet(c):
    system_family = QFontDatabase.systemFont(QFontDatabase.GeneralFont).family().replace("'", "")
    return f"""
    QWidget {{ color: {c['text']}; font-family: '{system_family}', 'PingFang SC', 'Microsoft YaHei UI', 'Noto Sans CJK SC', sans-serif; font-size: 13px; }}
    #root, #right {{ background: {c['shell']}; }}
    #sidebar {{ background: {c['side']}; border-right: 1px solid {c['border']}; }}
    #topbar {{ background: {c['shell']}; }}
    #canvas {{ background: {c['canvas']}; border-top-left-radius: 18px; }}
    #historyDrawer {{ background: {c['panel']}; border: 1px solid {c['strong']}; border-radius: 14px; }}
    QLabel {{ background: transparent; border: none; }}
    QLabel[kind='brandmark'] {{ background: {c['accent']}; color: white; font-size: 23px; font-weight: 600; border-radius: 12px; }}
    QLabel[kind='brand'] {{ font-size: 17px; font-weight: 600; }}
    QLabel[kind='wordmark'] {{ font-size: 8px; font-weight: 600; color: {c['muted']}; letter-spacing: 1px; }}
    QLabel[kind='eyebrow'] {{ color: {c['muted']}; font-size: 11px; }}
    QLabel[kind='heading'] {{ font-size: 25px; font-weight: 600; letter-spacing: .2px; }}
    QLabel[kind='section_title'] {{ font-size: 16px; font-weight: 600; }}
    QLabel[kind='secondary'], QLabel[kind='hero_description'] {{ color: {c['secondary']}; }}
    QLabel[kind='muted'] {{ color: {c['muted']}; }}
    QLabel[kind='tiny'] {{ font-size: 11px; color: {c['muted']}; }}
    QLabel[kind='breadcrumb'] {{ font-size: 13px; font-weight: 500; }}
    QLabel[kind='preview_badge'] {{ color: {c['secondary']}; border: 1px solid {c['strong']}; border-radius: 12px; padding: 4px 12px; font-size: 10px; }}
    QLabel[kind='sample_badge'] {{ background: {c['soft']}; color: {c['secondary']}; font-size: 10px; padding: 4px 9px; border-radius: 5px; }}
    QLabel[kind='panel_title'] {{ font-size: 12px; font-weight: 500; color: {c['secondary']}; }}
    QLabel[kind='empty_title'] {{ font-size: 16px; font-weight: 500; color: {c['secondary']}; }}
    QLabel[kind='privacy_title'] {{ font-size: 11px; color: {c['green']}; }}
    QLabel[kind='feature_title'], QLabel[kind='record_title'] {{ font-size: 13px; font-weight: 500; }}
    QLabel[kind='detail_translation'] {{ font-size: 26px; font-weight: 500; }}
    QLabel[kind='detail_text'] {{ font-size: 17px; }}
    QLabel[kind='detail_heading'] {{ font-size: 13px; font-weight: 600; }}
    QLabel[kind='detail_term'] {{ font-size: 16px; font-weight: 500; }}
    QLabel[kind='detail_note'] {{ font-size: 13px; color: {c['secondary']}; }}
    QLabel[kind='detail_example'] {{ font-size: 13px; }}
    QLabel[kind='drop_title'] {{ font-size: 20px; font-weight: 500; }}
    QLabel[kind='hero_title'] {{ font-size: 23px; font-weight: 600; }}
    QLabel[kind='timestamp'] {{ color: {c['accent']}; font-size: 11px; font-family: 'SF Mono', 'Consolas', monospace; }}
    QLabel[kind='history_date'] {{ color: {c['muted']}; font-size: 11px; padding-top: 18px; padding-bottom: 5px; }}
    QLabel[kind='state_done'] {{ color: {c['green']}; background: {c['green_bg']}; border-radius: 5px; padding: 4px 9px; font-size: 11px; }}
    QLabel[kind='state_paused'], QLabel[kind='state_review'] {{ color: {c['secondary']}; background: {c['soft']}; border-radius: 5px; padding: 4px 9px; font-size: 11px; }}
    QLabel[kind='state_failed'] {{ color: {c['accent']}; background: {c['tint']}; border-radius: 5px; padding: 4px 9px; font-size: 11px; }}
    QLabel[kind='transcript_source'] {{ color: {c['secondary']}; font-size: 14px; }}
    QLabel[kind='transcript_target'] {{ font-size: 17px; }}
    QLabel[kind='duration'] {{ font-family: 'SF Mono', 'Consolas', monospace; font-size: 23px; }}
    QLabel[kind='toast'] {{ background: {c['text']}; color: {c['canvas']}; border-radius: 12px; padding: 10px 22px; font-size: 12px; }}
    QFrame[kind='privacy'] {{ background: {c['green_bg']}; border-radius: 10px; }}
    QFrame[kind='editor_card'] {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 13px; }}
    QFrame[kind='languagebar'] {{ background: transparent; border: none; border-bottom: 1px solid {c['border']}; }}
    QFrame[kind='tabbar'] {{ border: none; border-bottom: 1px solid {c['border']}; }}
    QFrame[kind='vertical_divider'], QFrame[kind='horizontal_divider'] {{ background: {c['border']}; border: none; }}
    QFrame[kind='detail_section'] {{ border: none; border-top: 1px solid {c['border']}; }}
    QFrame[kind='feature'], QFrame[kind='selection'] {{ background: {c['soft']}; border: 1px solid {c['border']}; border-radius: 10px; }}
    QFrame[kind='drop'] {{ background: {c['soft']}; border: 1px dashed {c['strong']}; border-radius: 14px; }}
    QFrame[kind='drop'][dragging='true'] {{ border: 2px dashed {c['accent']}; background: {c['tint']}; }}
    QFrame[kind='upload_art'] {{ background: {c['tint']}; border: 1px solid {c['border']}; border-radius: 22px; }}
    QFrame[kind='wave_art'] {{ background: {c['icon_bg']}; border-radius: 36px; }}
    QFrame[kind='page_rail'] {{ background: {c['soft']}; border: none; border-right: 1px solid {c['border']}; border-top-left-radius: 13px; border-bottom-left-radius: 13px; }}
    QFrame[kind='record'] {{ border: none; border-bottom: 1px solid {c['border']}; }}
    QFrame[kind='drawer_record'] {{ border: none; border-bottom: 1px solid {c['border']}; }}
    QFrame[kind='recovery'] {{ background: {c['tint']}; border: none; border-bottom: 1px solid {c['border']}; }}
    QFrame[kind='record_icon'] {{ background: {c['tint']}; border-radius: 12px; }}
    QFrame[kind='audio_hero'] {{ background: {c['lavender']}; border-radius: 16px; }}
    QFrame[kind='audio_hero'] QLabel {{ color: {c['lavender_text']}; }}
    QFrame[kind='audio_secondary'] {{ background: {c['soft']}; border: 1px solid {c['border']}; border-radius: 16px; }}
    QFrame[kind='reader_page'] {{ border: none; border-bottom: 1px dashed {c['strong']}; }}
    QPushButton {{ background: {c['panel']}; border: 1px solid {c['strong']}; border-radius: 7px; padding: 0 13px; font-weight: 500; }}
    QPushButton:hover {{ background: {c['hover']}; }}
    QPushButton:pressed {{ background: {c['border']}; }}
    QPushButton:disabled {{ color: {c['muted']}; background: {c['soft']}; border-color: {c['border']}; }}
    QPushButton:focus-visible {{ border: 2px solid {c['accent']}; }}
    QPushButton[kind='primary'] {{ background: {c['accent']}; color: white; border: none; border-radius: 9px; padding: 0 22px; }}
    QPushButton[kind='primary']:hover {{ background: {c['accent_hover']}; }}
    QPushButton[kind='primary']:disabled {{ background: {c['hover']}; color: {c['muted']}; }}
    QPushButton[kind='nav'] {{ text-align: left; border: none; background: transparent; color: {c['secondary']}; border-radius: 9px; font-size: 15px; padding-left: 17px; }}
    QPushButton[kind='nav']:hover {{ background: {c['hover']}; }}
    QPushButton[kind='nav']:checked {{ background: {c['tint']}; color: {c['accent']}; font-weight: 600; }}
    QPushButton[kind='quiet'], QPushButton[kind='icon'] {{ background: transparent; color: {c['secondary']}; border: 1px solid transparent; padding: 0 8px; font-weight: 400; }}
    QPushButton[kind='quiet']:hover, QPushButton[kind='icon']:hover {{ background: {c['hover']}; }}
    QPushButton[kind='quiet']:disabled, QPushButton[kind='icon']:disabled {{ color: {c['muted']}; background: transparent; }}
    QPushButton[kind='quiet']:checked {{ background: {c['tint']}; color: {c['accent']}; }}
    QPushButton[kind='tab'] {{ background: transparent; border: none; border-radius: 0; border-bottom: 3px solid transparent; color: {c['secondary']}; font-size: 15px; }}
    QPushButton[kind='tab']:checked {{ border-bottom-color: {c['accent']}; color: {c['accent']}; font-weight: 600; }}
    QPushButton[kind='tab']:hover {{ background: {c['soft']}; }}
    QPushButton[kind='filter'], QPushButton[kind='page'] {{ background: transparent; color: {c['secondary']}; border: none; padding: 0 17px; }}
    QPushButton[kind='filter']:checked, QPushButton[kind='page']:checked {{ background: {c['tint']}; color: {c['accent']}; }}
    QPushButton[kind='page'] {{ text-align: left; font-size: 12px; padding: 0 8px; }}
    QPushButton[kind='theme'] {{ background: {c['soft']}; border: 1px solid {c['border']}; font-size: 14px; }}
    QPushButton[kind='theme']:checked {{ background: {c['tint']}; color: {c['accent']}; border-color: {c['accent']}; }}
    QPushButton[kind='hero_arrow'] {{ background: transparent; border: 1px solid {c['strong']}; border-radius: 18px; padding: 0; }}
    QPushButton[kind='hero_link'] {{ background: transparent; color: {c['secondary']}; border: none; text-align: left; padding: 0; }}
    QPushButton[kind='audio_card'] {{ background: {c['soft']}; border: 1px solid {c['border']}; border-radius: 16px; padding: 0; }}
    QPushButton[kind='audio_card']:hover, QPushButton[kind='audio_card']:focus {{ border-color: {c['accent']}; background: {c['tint']}; }}
    QFrame[kind='audio_hero'] QPushButton {{ color: {c['lavender_text']}; border-color: #AFA4D6; }}
    QPlainTextEdit {{ background: transparent; color: {c['text']}; border: none; selection-background-color: {c['accent']}; selection-color: white; font-size: 16px; padding: 0; }}
    QPlainTextEdit#sourceEditor {{ font-size: 17px; }}
    QPlainTextEdit#targetEditor {{ font-size: 17px; }}
    QPlainTextEdit[kind='reader_text'] {{ font-size: 15px; }}
    QTextEdit {{ background: transparent; color: {c['text']}; border: none; selection-background-color: {c['accent']}; selection-color: white; font-size: 15px; }}
    QComboBox {{ background: {c['select_bg']}; border: 1px solid {c['border']}; border-radius: 10px; padding: 9px 24px 9px 12px; min-height: 19px; }}
    QComboBox:hover {{ background: {c['hover']}; border-color: {c['strong']}; }}
    QComboBox:focus {{ border-color: {c['accent']}; }}
    QComboBox:disabled {{ color: {c['muted']}; border-color: {c['border']}; }}
    QComboBox[kind='language'] {{ font-size: 15px; font-weight: 500; }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    QFrame#selectPopup {{ background: transparent; border: none; }}
    QListView#selectOptions {{ background: transparent; border: none; outline: none; color: {c['text']}; font-size: 13px; }}
    QListView#selectOptions::item {{ min-height: 36px; padding: 0 10px; border-radius: 7px; }}
    QListView#selectOptions::item:hover {{ background: {c['hover']}; }}
    QListView#selectOptions::item:selected {{ background: {c['tint']}; color: {c['accent']}; }}
    QListView#selectOptions::item:disabled {{ color: {c['muted']}; }}
    QLineEdit {{ background: {c['soft']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 10px 14px; selection-background-color: {c['accent']}; }}
    QLineEdit:focus {{ border-color: {c['accent']}; }}
    QPlainTextEdit#historyQuery, QPlainTextEdit#historyQuery:focus,
    QPlainTextEdit#historyTitle, QPlainTextEdit#historyTitle:focus,
    QPlainTextEdit#dictionaryQuery, QPlainTextEdit#dictionaryQuery:focus {{
        background: transparent; border: 1px solid {c['border']}; border-radius: 8px;
        padding: 8px 10px; font-size: 17px; outline: none;
    }}
    QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
    QScrollBar:vertical {{ background: transparent; width: 6px; margin: 4px 0; }}
    QScrollBar::handle:vertical {{ background: {c['strong']}; border-radius: 3px; min-height: 25px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
    QToolTip {{ background: {c['panel']}; color: {c['text']}; border: 1px solid {c['strong']}; padding: 6px 10px; }}
    QMenu {{ background: {c['panel']}; color: {c['text']}; border: 1px solid {c['strong']}; border-radius: 7px; padding: 5px; }}
    QMenu::item {{ padding: 9px 20px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {c['tint']}; color: {c['accent']}; }}
    QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 7px; }}
    """

