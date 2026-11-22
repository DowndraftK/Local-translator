"""Preview-only appearance preference; native system events never replace an override."""
import json
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox

from translator_core import CoreError
from translator_core.store import atomic_write
from translator_core.resources import no_links, safe_read, token
from .components import PALETTES, Button, IconLabel, stylesheet


class ThemeController(QObject):
    changed = Signal(str)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = Path(path)
        self.preference = "system"
        self.warning = ""
        self.baseline = self.baseline_token = None
        self.dirty = self.blocked = False
        self.unconfirmed = None
        self.scheme = QGuiApplication.styleHints().colorScheme()
        self.reload_preference(apply=False)
        QGuiApplication.styleHints().colorSchemeChanged.connect(self.system_changed)

    def disk(self):
        no_links(self.path.absolute())
        if not self.path.exists() and not self.path.is_symlink():
            return None, None
        return safe_read(self.path.absolute(), 4096), token(self.path)

    def reload_preference(self, *, apply=True):
        try:
            raw, disk_token = self.disk()
            preference = 'system'
            if raw is not None:
                data = json.loads(raw)
                if set(data) != {'schema', 'theme'} or data.get("schema") != 1 or data.get("theme") not in ("system", "light", "dark"):
                    raise ValueError("外观配置无效")
                preference = data['theme']
            self.preference = preference
            self.baseline, self.baseline_token = raw, disk_token
            self.dirty = self.blocked = False
            self.unconfirmed = None
            self.warning = ''
        except (OSError, CoreError, ValueError, TypeError, AttributeError) as exc:
            self.blocked = True
            self.preference = 'system'
            self.dirty = False
            self.unconfirmed = None
            self.warning = f"外观配置未读取，已跟随系统：{exc}"
        if apply:
            self.apply()

    def set_preference(self, preference):
        if preference not in ("system", "light", "dark"):
            return
        self.preference = preference
        self.dirty = True
        self.save_preference()
        self.apply()

    def save_preference(self):
        expected = json.dumps({'schema': 1, 'theme': self.preference}).encode()
        try:
            if self.blocked:
                raise ValueError('请先重新读取外观配置并核对。')
            actual, disk_token = self.disk()
            if self.unconfirmed is not None:
                if actual != self.unconfirmed or expected != actual:
                    raise ValueError('磁盘与待确认写入不符，请重新读取。')
            else:
                if (actual, disk_token) != (self.baseline, self.baseline_token):
                    raise ValueError('外观配置已被外部修改，请重新读取。')
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.unconfirmed = expected
                atomic_write(expected, self.path)
                actual, disk_token = self.disk()
                if actual != expected:
                    raise ValueError('外观配置写后回读未确认。')
            self.baseline, self.baseline_token = actual, disk_token
            self.dirty = False
            self.unconfirmed = None
            self.warning = ""
        except (OSError, CoreError, ValueError) as exc:
            if self.unconfirmed is not None:
                try:
                    actual, _ = self.disk()
                    if actual != self.unconfirmed:
                        self.unconfirmed = None
                except (OSError, CoreError):
                    pass
            self.warning = f"本次外观已应用，偏好尚未保存：{exc}"
        self.changed.emit(getattr(self, 'effective', 'light'))
        return not self.dirty

    def prepare_close(self, parent):
        if not self.dirty:
            return True
        dialog = QMessageBox(parent)
        dialog.setWindowTitle('外观偏好尚未确认')
        dialog.setAttribute(Qt.WA_QuitOnClose, False)
        dialog.setText('保存或确认外观偏好后继续关闭？')
        save = dialog.addButton('保存或确认外观', QMessageBox.AcceptRole)
        discard = dialog.addButton('重新读取已保存外观', QMessageBox.DestructiveRole)
        back = dialog.addButton('返回设置', QMessageBox.RejectRole)
        dialog.setEscapeButton(back)
        dialog.exec()
        selected = dialog.clickedButton()
        dialog.deleteLater()
        if selected is save:
            return self.save_preference()
        if selected is discard:
            self.reload_preference()
            return not self.dirty
        return False

    def system_changed(self, scheme):
        self.scheme = scheme
        if self.preference == "system":
            self.apply()

    def apply(self):
        self.effective = ("dark" if self.scheme == Qt.ColorScheme.Dark else "light") if self.preference == "system" else self.preference
        colors = PALETTES[self.effective]
        app = QApplication.instance()
        app.setProperty("translator_colors", colors)
        palette = QPalette()
        for role, key in ((QPalette.Window, "canvas"), (QPalette.WindowText, "text"),
                          (QPalette.Base, "panel"), (QPalette.AlternateBase, "soft"),
                          (QPalette.Text, "text"), (QPalette.Button, "panel"),
                          (QPalette.ButtonText, "text"), (QPalette.Highlight, "accent"),
                          (QPalette.ToolTipBase, "panel"), (QPalette.ToolTipText, "text"),
                          (QPalette.PlaceholderText, "muted")):
            palette.setColor(role, QColor(colors[key]))
        palette.setColor(QPalette.HighlightedText, QColor("white"))
        for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
            palette.setColor(QPalette.Disabled, role, QColor(colors["muted"]))
        app.setPalette(palette)
        # Application scope also covers dialogs and Qt.Popup top-level windows.
        app.setStyleSheet(stylesheet(colors) + f"""
            QDialog, QMessageBox {{ background: {colors['canvas']}; }}
            QListWidget {{ background: {colors['panel']}; border: 1px solid {colors['border']}; border-radius: 9px; }}
            QListWidget::item {{ padding: 10px; border-radius: 6px; }}
            QListWidget::item:selected {{ background: {colors['tint']}; color: {colors['accent']}; }}
            QProgressBar {{ background: {colors['soft']}; border: 1px solid {colors['border']}; border-radius: 6px; text-align: center; min-height: 20px; }}
            QProgressBar::chunk {{ background: {colors['accent']}; border-radius: 5px; }}
            QWidget#compatibility QPushButton {{ min-height: 32px; }}
            QPushButton#primary {{ background: {colors['accent']}; color: white; }}
            QPushButton#primary:disabled {{ background: {colors['hover']}; color: {colors['muted']}; }}
            QLabel#muted {{ color: {colors['muted']}; }}
            QWidget#compatibility QPlainTextEdit {{ background: {colors['panel']}; border: 1px solid {colors['border']}; border-radius: 8px; padding: 6px; font-size: 14px; }}
        """)
        for top in app.topLevelWidgets():
            top.colors = colors
            for child in top.findChildren(IconLabel) + top.findChildren(Button):
                child.recolor(colors)
            top.update()
        self.changed.emit(self.effective)
