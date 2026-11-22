"""R6 page containers. Unconnected capabilities are explicitly labelled."""
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication, QImageReader, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFileDialog, QFrame, QStackedWidget,
                               QWidget)

from .components import (Button, Combo, DropZone, ElidedLabel, IconLabel, ImagePreview,
                         frame, hbox, label, scrollable, vbox)
from .workspace import PageLifecycle


class UploadPage(QWidget, PageLifecycle):
    document_selected = Signal(str)
    image_selected = Signal(str)
    image_paste_requested = Signal()
    compatibility_requested = Signal()

    def __init__(self, image=False, parent=None, *, workbench=False):
        super().__init__(parent)
        self.is_image = image
        self.guard = lambda:True
        self.image_source = None
        outer = vbox(self)
        page = QWidget(); box = vbox(page, (36, 28, 36, 24), 20)
        heading = hbox()
        heading.addWidget(label("图片翻译" if image else "文档翻译", "heading")); heading.addStretch()
        if not image and not workbench:
            heading.addWidget(Button("返回文档阅读", "chevron", "quiet", self.compatibility_requested.emit))
        box.addLayout(heading)
        self.drop = DropZone(); self.drop.setMinimumHeight(350); self.drop.setFocusPolicy(Qt.StrongFocus)
        body = vbox(self.drop, (24, 24, 24, 20), 16)
        self.upload_stack = QStackedWidget()
        empty = QWidget(); e = vbox(empty, spacing=16); e.addStretch()
        art = frame("upload_art"); art.setFixedSize(80, 80)
        vbox(art).addWidget(IconLabel("image" if image else "file", 40, "accent"), 0, Qt.AlignCenter)
        e.addWidget(art, 0, Qt.AlignHCenter)
        for text, kind in (("点击或拖拽图片到这里" if image else "点击或拖拽文档到这里", "drop_title"),
                           ("支持 PNG、JPG、JPEG 格式" if image else "支持 PDF、UTF-8 TXT 格式", "secondary")):
            hint = label(text, kind); hint.setAlignment(Qt.AlignCenter); e.addWidget(hint)
        row = hbox(spacing=12); row.addStretch()
        row.addWidget(Button("选择图片" if image else "选择文档", "upload", "primary", self.choose))
        if image: row.addWidget(Button("粘贴图片", "paste", "secondary", self.paste))
        row.addStretch(); e.addLayout(row)
        if image:
            hint = label("也可直接按 ⌘ / Ctrl + V 粘贴图片", "tiny"); hint.setAlignment(Qt.AlignCenter); e.addWidget(hint)
        e.addStretch(); self.upload_stack.addWidget(empty)
        if image:
            loaded = QWidget(); p = vbox(loaded, spacing=16)
            row = hbox(); row.addWidget(label("图片预览", "panel_title")); row.addStretch(); row.addWidget(label("按比例适应窗口", "tiny")); p.addLayout(row)
            self.preview = ImagePreview(); p.addWidget(self.preview, 1)
            self.toolbar = QWidget(); tools = hbox(self.toolbar, spacing=12)
            tools.addWidget(Button("更换图片", "folder", "secondary", self.choose))
            tools.addWidget(Button("粘贴图片", "paste", "secondary", self.paste)); tools.addStretch()
            tools.addWidget(label("⌘ / Ctrl + V 可替换图片", "tiny")); p.addWidget(self.toolbar)
            self.upload_stack.addWidget(loaded)
            self.paste_shortcut = QShortcut(QKeySequence.Paste, self)
            self.paste_shortcut.setContext(Qt.WidgetWithChildrenShortcut); self.paste_shortcut.activated.connect(self.paste)
        body.addWidget(self.upload_stack, 1)
        self.note = label("图片导入与预览已接入；独立图片 OCR / 翻译在 T05 接入" if image else
                          "选择后核对真实页数与范围；保存来源、提取文字层或显式 OCR，再主动翻译。", "tiny", True)
        body.addWidget(self.note); box.addWidget(self.drop, 1)
        self.selection = frame("selection"); selected = hbox(self.selection, (16, 12, 16, 12), 10)
        self.file_label = ElidedLabel("", "secondary"); selected.addWidget(self.file_label, 1)
        if image:
            self.translate = Button("保存与识别图片", "arrow", "primary"); self.translate.setEnabled(False); selected.addWidget(self.translate)
            remove = Button("", "close", "icon", self.clear_image); remove.setAccessibleName("移除图片"); remove.setFixedWidth(34); selected.addWidget(remove)
        self.selection.hide(); box.addWidget(self.selection)
        outer.addWidget(scrollable(page))
        self.drop.clicked.connect(lambda: self.choose() if self.upload_stack.currentIndex() == 0 else self.drop.setFocus())
        self.drop.fileDropped.connect(self.load_file)

    def choose(self):
        if not self.guard():
            self.note.setText('请先完成当前输入与草稿保存，再选择来源。');return
        path, _ = QFileDialog.getOpenFileName(self, "选择图片" if self.is_image else "选择 PDF / TXT", "",
                                             "图片 (*.png *.jpg *.jpeg)" if self.is_image else "文档 (*.pdf *.txt)")
        if path: self.load_file(path)

    def load_file(self, path):
        path = Path(path)
        if not self.is_image:
            if path.suffix.lower() not in (".pdf", ".txt"):
                self.note.setText("请选择 PDF 或 UTF-8 TXT。"); return
            self.document_selected.emit(str(path)); return
        self.image_selected.emit(str(path))

    def paste(self):
        self.image_paste_requested.emit()

    def display_image(self, pixmap, description, source):
        self.image_source = source
        self.preview.set_image(pixmap); self.upload_stack.setCurrentIndex(1)
        self.file_label.full_text = description; self.file_label.setText(description); self.file_label.setToolTip(description)
        self.selection.show(); self.note.setText("仅完成导入与预览；尚未保存来源或执行识别、翻译（T05）。")

    def clear_image(self):
        self.image_source = None; self.preview.clear(); self.upload_stack.setCurrentIndex(0); self.selection.hide()


from .history_view import SummaryView


class AudioPage(QWidget, PageLifecycle):
    recent_requested = Signal()
    def __init__(self, parent=None, workspace=None, *, whole_cards=False):
        super().__init__(parent);self.workspace=workspace
        box=vbox(self)
        recent=hbox(spacing=8);recent.addStretch()
        recent.addWidget(Button('最近记录','history','quiet',self.recent_requested.emit))
        box.addLayout(recent)
        self.stack=QStackedWidget();box.addWidget(self.stack)
        landing=QWidget();content=vbox(landing,(36,28,36,24),24)
        content.addWidget(label('同传转写','heading'));row=hbox(spacing=20)
        for index,title,note,symbol in ((0,'同声传译','麦克风或电脑内部声音，分别选择','mic'),(1,'录音转写','音频文件、字幕、翻译与播放','headphones')):
            if whole_cards:
                card = Button('', None, 'audio_card', lambda checked=False, i=index: self.open_session(i))
                card.setAccessibleName(title)
                card.setEnabled(bool(workspace))
                card.setMinimumHeight(194)
                body = vbox(card, (24,24,24,24), 10)
                for child in (IconLabel(symbol,31,'accent'), label(title,'hero_title'), label(note,'secondary',True)):
                    child.setAttribute(Qt.WA_TransparentForMouseEvents)
                    body.addWidget(child)
                body.addStretch()
                row.addWidget(card,1)
                continue
            card=frame('audio_secondary');c=vbox(card,(24,24,24,24),16)
            c.addWidget(IconLabel(symbol,31));c.addWidget(label(title,'hero_title'));c.addWidget(label(note,'tiny'))
            button=Button('选择音频文件' if index else '选择声音来源','arrow','quiet',lambda checked=False,i=index:self.open_session(i))
            button.setEnabled(bool(workspace));c.addWidget(button);row.addWidget(card,1)
        content.addLayout(row);content.addStretch();self.stack.addWidget(landing)
        if workspace:
            self.stack.addWidget(workspace)
            workspace.back_requested.connect(lambda:self.stack.setCurrentIndex(0) if workspace.guard_switch() else None)

    def open_session(self,index):
        if self.workspace:
            self.workspace.set_live_mode(index == 0)
            self.stack.setCurrentIndex(1)

    def leave_page(self):
        if self.workspace:self.workspace.leave_page()


class SettingsPage(QWidget, PageLifecycle):
    diagnostics_requested = Signal()

    def __init__(self, theme, parent=None, *, simple_resources=False):
        super().__init__(parent)
        self.theme = theme
        outer = vbox(self); page = QWidget(); box = vbox(page, (36, 28, 36, 24), 24)
        if not simple_resources:
            box.addWidget(label("偏好设置", "heading"))
        appearance = frame("editor_card"); p = vbox(appearance, (24, 24, 24, 24), 18)
        p.addWidget(label("外观", "section_title")); row = hbox(spacing=14)
        self.theme_buttons = {}; group = QButtonGroup(self)
        for key, title, symbol in (("system", "跟随系统", "system"), ("light", "浅色", "sun"), ("dark", "深色", "moon")):
            button = Button(title, symbol, "theme")
            button.setCheckable(True); button.setMinimumHeight(58); group.addButton(button)
            button.toggled.connect(lambda checked, k=key: theme.set_preference(k) if checked and theme.preference != k else None)
            self.theme_buttons[key] = button; row.addWidget(button, 1)
        p.addLayout(row); self.theme_status = label("", "tiny", True); p.addWidget(self.theme_status); box.addWidget(appearance)
        row = hbox(spacing=10)
        self.theme_retry = Button('保存或确认外观', None, 'quiet', theme.save_preference)
        self.theme_reload = Button('重新读取外观', None, 'quiet', theme.reload_preference)
        row.addWidget(self.theme_retry)
        row.addWidget(self.theme_reload)
        row.addStretch()
        p.addLayout(row)
        preferences = frame("editor_card"); p = vbox(preferences, (24, 24, 24, 24), 14)
        p.addWidget(label("翻译偏好", "section_title"))
        self.preferences_layout = p
        box.addWidget(preferences)
        if simple_resources:
            head = frame('tabbar')
            header = vbox(head, (28,18,28,0), 4)
            header.addWidget(label('偏好设置', 'heading'))
            tab_row = hbox(spacing=12)
            self.tabs = {}
            self.sections = QStackedWidget()
            self.layout_body = outer
            tab_group = QButtonGroup(self)
            box.addStretch()
            for index, (key,title) in enumerate((('general','通用'),('speech','语音资源'),('services','本地服务'))):
                tab = Button(title, None, 'tab')
                tab.setCheckable(True)
                tab.setFixedSize(104,44)
                tab_group.addButton(tab,index)
                self.tabs[key] = tab
                tab_row.addWidget(tab)
                if key=='general':
                    content = page
                else:
                    content = QWidget()
                    layout = vbox(content, (28,20,28,20), 12)
                    layout.addStretch()
                    if key=='speech':
                        self.speech_layout = layout
                    else:
                        self.resources_overview = layout
                self.sections.addWidget(scrollable(content))
            tab_row.addStretch()
            tab_group.idClicked.connect(self.sections.setCurrentIndex)
            self.tabs['general'].setChecked(True)
            header.addLayout(tab_row)
            outer.addWidget(head)
            outer.addWidget(self.sections,1)
            self.advanced = QWidget()
            self.resources_layout = vbox(self.advanced, spacing=14)
            self.advanced_button = Button("高级设置", "settings", "quiet")
            self.advanced_button.setCheckable(True)
            self.advanced_button.toggled.connect(self.advanced.setVisible)
            self.advanced.hide()
            self.resources_layout.addWidget(Button("工作进程状态与连接诊断", "settings", "quiet",
                                                   self.diagnostics_requested.emit))
        else:
            resources = frame("feature"); r = vbox(resources, (24, 22, 24, 22), 14)
            r.addWidget(label("本地资源与运行环境", "section_title"))
            r.addWidget(label("资源文件、运行能力与实际识别分别记录。翻译服务仍由文字页的主动检查提供状态。", "tiny", True))
            r.addWidget(Button("工作进程状态与连接诊断", "settings", "quiet", self.diagnostics_requested.emit))
            self.resources_layout = r
            box.addWidget(resources)
            box.addStretch()
            outer.addWidget(scrollable(page))
        theme.changed.connect(self.update_theme)

    def install_resource_overview(self, overview):
        self.speech_layout.insertWidget(0,overview.speech)
        self.resources_overview.insertWidget(0,overview)
        self.resources_overview.insertWidget(1,self.advanced_button,alignment=Qt.AlignLeft)
        self.resources_overview.insertWidget(2,self.advanced)
        self.layout_body.addWidget(overview.feedback)

    def open_section(self, section):
        self.tabs[section].click()

    def update_theme(self, *_):
        self.theme_buttons[self.theme.preference].setChecked(True)
        title = "深色" if self.theme.effective == "dark" else "浅色"
        saved = '默认跟随系统' if self.theme.baseline is None else '外观偏好已保存'
        self.theme_status.setText(f"当前显示：{title} · {saved}" if not self.theme.warning else self.theme.warning)
        self.theme_retry.setVisible(self.theme.dirty)
        self.theme_reload.setVisible(self.theme.dirty or self.theme.blocked)
