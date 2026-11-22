"""Shared R6 shell, direct text workflow and retained document/OCR reader."""
import argparse
import json
import os
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import PySide6
from translator_core.resources import no_links
from PySide6.QtCore import QCoreApplication, QEvent, QProcess, QTimer, Qt, qVersion
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QButtonGroup, QDialog, QMainWindow, QPlainTextEdit,
                               QProgressBar, QScrollArea, QStackedWidget, QWidget)
from . import PREVIEW_ID, PRODUCT_VERSION
from .client import WorkerClient
from .components import Button, Combo, IconLabel, frame, hbox, label, vbox
from .document_page import DocumentPage
from .document_workspace import DocumentWorkspace
from .environment_page import EnvironmentPage
from .image_workspace import ImageWorkspace
from .dictionary_page import DictionaryPage, DictionaryResources
from .audio_workspace import AudioWorkspace
from .pages import AudioPage, SettingsPage, SummaryView, UploadPage
from .paths import LOCAL_DELIVERY, data_directory, initialize_package_resources
from .resource_page import ResourcePanel
from .preferences_page import LocalPreferencesPanel, PreferencesPanel, ManagedPanel
from .text_flow import TextFlow
from .text_workspace import TextWorkspace
from .theme import ThemeController
from .workspace import PHASES, WorkspaceBridge
from .workbench_page import WorkbenchPage, open_record

LABELS = {"connecting": "工作进程：正在连接", "ready": "工作进程：已就绪",
        "running": "工作进程：正在执行操作", "cancelling": "工作进程：正在取消",
          "closing": "工作进程：正在保存并关闭", "failed": "工作进程：连接失败",
          "disconnected": "工作进程：已停止"}


class PreviewWindow(QMainWindow):
    def __init__(self, evidence_path=None, screenshot_path=None, task_root=None, boundary_helper=None,
                 pdf_helper=None, pdf_library=None, preferences_path=None, connect_worker=True, endpoint=None,
                 dictionary_config=None, image_helper=None, preview_data_root=None, install_catalog=None, catalog_sha256=None, translation_models_root=None):
        super().__init__()
        package = Path(__file__).resolve().parents[1]; project = package.parents[1]
        identity_path = package / "build-identity.json"
        self.identity = json.loads(identity_path.read_text(encoding="utf-8")) if identity_path.exists() else {
            "product_version": PRODUCT_VERSION, "preview_id": PREVIEW_ID, "build_id": PREVIEW_ID + "-source-entry"}
        project = Path(self.identity.get('project_root', project)).resolve()
        self.evidence_path, self.screenshot_path = evidence_path, screenshot_path
        self.started_monotonic = time.monotonic()
        self.close_pending = False; self.current_section = "translate"; self.current_tab = 0
        self.evidence = {"schema": 1, "candidate": self.identity, "platform": platform.system(),
            "started_at": datetime.now(timezone.utc).isoformat(), "qt_version": qVersion(),
            "qpa_platform": QApplication.platformName(), "desktop_window_shown": False, "events": [],
            "normal_exit": None, "inference": False, "production_task_access": False,
            "runtime": {"python_executable": sys.executable, "python_prefix": sys.prefix,
                        "pyside6_module": PySide6.__file__, "qt_plugin_paths": QCoreApplication.libraryPaths()}}
        local = self.identity.get('delivery_layout') == LOCAL_DELIVERY
        self.local = local
        self.record_request = None
        self.record_identity = None
        self.record_dialog = None
        preview_data = data_directory(project, self.identity, preview_data_root, task_root)
        task_root = preview_data/'tasks'
        selected_dictionary = dictionary_config or self.identity.get('dictionary_config')
        for output_path in (preferences_path,evidence_path,screenshot_path):
            if output_path and not no_links(Path(output_path).absolute()).is_relative_to(preview_data):
                raise ValueError('配置与证据必须位于显式本包数据根。')
        if not local:
            if not selected_dictionary:
                raise ValueError('T09-B入口需要只读词典配置。')
            initialize_package_resources(package, self.identity, preview_data)
        boundary_helper = Path(boundary_helper or package / "shared/platform/macos/text-boundaries")
        if not local and not boundary_helper.is_file() and sys.platform == "darwin": boundary_helper = project / "artifacts/b02-20261004/bin/text-boundaries"
        pdf_helper = Path(pdf_helper or package / "shared/platform/macos/pdf-facts")
        if not local and not pdf_helper.is_file() and sys.platform == "darwin": pdf_helper = project / "artifacts/b03-20261005/pdf-facts"
        pdf_library = Path(pdf_library or package / "vendor/pdfium")
        image_helper = Path(image_helper or package/'shared/platform/macos/image-facts')
        if not local and not image_helper.is_file() and sys.platform=='darwin': image_helper=project/'artifacts/t09a-20261106-run1/bin/image-facts'
        self.client = WorkerClient(self, task_root=task_root, boundary_helper=boundary_helper,
                                   pdf_helper=pdf_helper, pdf_library=pdf_library, endpoint=endpoint,
                                   dictionary_config=selected_dictionary, image_helper=image_helper, preview_data_root=preview_data,
                                   install_catalog=install_catalog, catalog_sha256=catalog_sha256,
                                   translation_models_root=translation_models_root)
        self.bridge = WorkspaceBridge(self.client, self)
        self.theme = ThemeController(preferences_path or preview_data/'preferences/appearance.json', self)
        self.evidence["preview_task_root"] = str(task_root)
        self.setWindowTitle("本地翻译器" if local else
                            f"本地翻译器 · {self.identity['preview_id']} · {platform.system()} · {self.identity['build_id']}")
        self.resize(1320, 860); self.setMinimumSize(1060, 720)
        QApplication.instance().styleHints().setTabFocusBehavior(Qt.TabFocusAllControls)
        root = QWidget(); root.setObjectName("root"); self.setCentralWidget(root); base = hbox(root)
        self.sidebar = frame(); self.sidebar.setObjectName("sidebar"); self.sidebar.setFixedWidth(208)
        side = vbox(self.sidebar, (16, 28, 16, 20), 6)
        brand = hbox(spacing=11); mark = label("译", "brandmark"); mark.setFixedSize(40, 40); mark.setAlignment(Qt.AlignCenter)
        brand.addWidget(mark); names = vbox(spacing=5); names.addWidget(label("本地翻译器", "brand")); names.addWidget(label("LOCAL TRANSLATOR", "wordmark")); brand.addLayout(names)
        side.addLayout(brand); side.addSpacing(30); side.addWidget(label("  工作空间", "eyebrow")); side.addSpacing(6)
        self.nav_buttons = {}; self.nav_group = QButtonGroup(self)
        for key, title, symbol in (("translate", "翻译", "translate"), ("dictionary", "词典", "book"), ("audio", "同传转写", "mic"), ("history", "我的翻译", "history")):
            button = Button("  "+title, symbol, "nav")
            button.setCheckable(True); button.setFixedHeight(48); button.setAccessibleName(title)
            button.toggled.connect(lambda checked, k=key: self.navigate(k) if checked else None)
            self.nav_buttons[key] = button; self.nav_group.addButton(button); side.addWidget(button)
        side.addStretch()
        settings = Button("  偏好设置", "settings", "nav"); settings.setCheckable(True); settings.setFixedHeight(48)
        settings.toggled.connect(lambda checked: self.navigate("settings") if checked else None)
        self.nav_buttons["settings"] = settings; self.nav_group.addButton(settings); side.addWidget(settings); base.addWidget(self.sidebar)
        right = QWidget(); right.setObjectName("right"); r = vbox(right); base.addWidget(right, 1)
        self.page_stack = QStackedWidget(); self.page_stack.setObjectName("canvas"); r.addWidget(self.page_stack, 1)
        translation = QWidget(); t = vbox(translation)
        tabbar = frame("tabbar"); tabs = hbox(tabbar, (36, 0, 30, 0), 25)
        group = QButtonGroup(self); self.tab_buttons = []
        for index, (title, symbol) in enumerate((("文本", "translate"), ("文档", "file"), ("图片", "image"))):
            button = Button("  "+title, symbol, "tab")
            button.setCheckable(True); button.setFixedSize(94, 60); group.addButton(button); tabs.addWidget(button); self.tab_buttons.append(button)
            button.toggled.connect(lambda checked, i=index: self.select_tab(i) if checked else None)
        tabs.addStretch(); tabs.addWidget(Button("最近记录", "history", "quiet", self.toggle_history_drawer)); t.addWidget(tabbar)
        self.translation_stack = QStackedWidget(); t.addWidget(self.translation_stack, 1)
        if local:
            self.text_workspace = WorkbenchPage('text', self.client, task_root)
            self.document_page = WorkbenchPage('document', self.client, task_root)
            self.image_page = WorkbenchPage('image', self.client, task_root)
        else:
            self.text_workspace = TextWorkspace()
            self.text_flow = TextFlow(self.text_workspace, self.client, self.bridge, task_root, self)
            self.document_page = DocumentWorkspace(self.client, task_root, auto_refresh=False)
            self.image_page = ImageWorkspace(self.client, task_root, auto_refresh=False)
        self.text_container = QStackedWidget(); self.text_container.addWidget(self.text_workspace)
        self.document_upload = UploadPage(workbench=local)
        self.document_container = QStackedWidget(); self.document_container.addWidget(self.document_upload)
        self.document_container.addWidget(self.document_page)
        self.image_upload = UploadPage(image=True, workbench=local)
        self.image_container = QStackedWidget();self.image_container.addWidget(self.image_upload);self.image_container.addWidget(self.image_page)
        if local:
            self.document_upload.note.setText('PDF 由 Apple Vision 提取原文。TXT 直接读取正文。')
            self.image_upload.note.setText('可用 Apple Vision 提取图片文字，也可手动补录。')
            self.document_page.selection_ready.connect(lambda:self.document_container.setCurrentIndex(1))
            for view, upload in ((self.document_page, self.document_upload), (self.image_page, self.image_upload)):
                view.notice.connect(upload.note.setText)
                view.notice.connect(self.notify)
        else:
            self.image_upload.note.setText('选择或主动粘贴图片，保存可恢复来源，再显式识别、核对和翻译。')
            self.document_page.back_requested.connect(lambda: self.document_container.setCurrentIndex(0) if self.protect_navigation() else None)
            self.image_page.back_requested.connect(lambda:self.image_container.setCurrentIndex(0) if self.protect_navigation() else None)
            self.document_upload.guard=self.text_flow.protect_draft
            self.image_upload.guard=self.text_flow.protect_draft
        self.image_page.selection_ready.connect(lambda:self.image_container.setCurrentIndex(1))
        self.image_upload.image_selected.connect(self.select_image)
        self.image_upload.image_paste_requested.connect(self.paste_image)
        for page in (self.text_container, self.document_container, self.image_container): self.translation_stack.addWidget(page)
        self.audio_workspace = AudioWorkspace(self.client, task_root, local=local)
        self.audio_page = AudioPage(workspace=self.audio_workspace, whole_cards=local); self.history_page = SummaryView(self.bridge)
        self.settings_page = SettingsPage(self.theme, simple_resources=local)
        self.preferences_panel = (LocalPreferencesPanel(self.client.preferences) if local else
                                  PreferencesPanel(self.client.preferences))
        self.settings_page.preferences_layout.addWidget(self.preferences_panel)
        self.managed_panel = ManagedPanel(self.client.managed)
        self.settings_page.resources_layout.addWidget(self.managed_panel)
        self.resource_panel = ResourcePanel(self.client.resources)
        self.settings_page.resources_layout.addWidget(self.resource_panel)
        self.client.managed.guard = lambda: self.preferences_panel.guard_switch() and self.resource_panel.guard_switch() and self.task_edit_guard()
        self.client.preferences.guard = self.task_edit_guard
        self.client.preferences.changed.connect(self.apply_preferences)
        self.client.resources.guard = self.task_edit_guard
        # This predicate is also consumed by passive control-state rendering.
        # Modal protection belongs only to explicit navigation/close actions.
        self.client.can_change_task = lambda: not self.client.preferences.pending and (local or not self.preferences_panel.local_dirty) and not (self.client.preferences.description or {}).get('dirty') and not (self.client.preferences.description or {}).get('unconfirmed') and not self.client.resources.pending and not (self.client.resources.description or {}).get('dirty') and self.task_edit_guard(flush=False)
        self.dictionary_page = DictionaryPage(self.client.dictionary)
        self.environment_page = None
        if local:
            self.environment_page = EnvironmentPage(self.client, lambda: (
                not self.client.preferences.pending
                and not (self.client.preferences.description or {}).get('dirty')
                and not (self.client.preferences.description or {}).get('unconfirmed')
                and not self.client.resources.pending and not (self.client.resources.description or {}).get('dirty')
            ))
            self.settings_page.install_resource_overview(self.environment_page)
        else:
            self.dictionary_resources = DictionaryResources(self.client.dictionary)
            self.settings_page.resources_layout.addWidget(self.dictionary_resources)
        self.pages = {"translate": translation, "dictionary": self.dictionary_page, "audio": self.audio_page, "history": self.history_page, "settings": self.settings_page}
        for page in self.pages.values(): self.page_stack.addWidget(page)
        self.document_upload.compatibility_requested.connect(lambda: self.document_container.setCurrentIndex(1))
        self.document_upload.document_selected.connect(self.select_document)
        self.settings_page.diagnostics_requested.connect(self.show_diagnostics)
        self.dictionary_page.resources_requested.connect(self.open_dictionary_resources)
        self.dictionary_page.transfer_requested.connect(self.dictionary_to_translation)
        self.text_workspace.lookup_requested.connect(self.selection_to_dictionary)
        self.theme.changed.connect(lambda *_:self.client.dictionary.passive('theme'))
        self.history_page.open_requested.connect(self.open_task)
        self.history_page.locate_requested.connect(self.open_task)
        self.audio_page.recent_requested.connect(self.toggle_history_drawer)
        self.history_page.block_reason=self.history_management_block
        self.history_page.guard=self.history_management_guard
        self.bridge.task_opened.connect(self.route_opened_task)
        self.bridge.location_ready.connect(self.apply_history_location)
        self.history_locations = []
        if not local:
            self.document_page.refresh.clicked.disconnect()
            self.document_page.refresh.clicked.connect(self.bridge.refresh)
        footer = frame("selection"); status_box = vbox(footer, (20, 7, 20, 7), 4)
        row = hbox(spacing=10); self.connection_label = label("", "tiny"); row.addWidget(self.connection_label)
        self.task_label = label("", "tiny", True); row.addWidget(self.task_label, 1)
        self.cancel_button = Button("取消" if local else "安全取消", None, "quiet", self.client.cancel); row.addWidget(self.cancel_button); status_box.addLayout(row)
        self.progress = QProgressBar(); self.progress.setMaximumHeight(22); status_box.addWidget(self.progress); self.progress.hide()
        self.error_label = label("", "tiny", True); status_box.addWidget(self.error_label); r.addWidget(footer)
        self.history_drawer = frame(); self.history_drawer.setObjectName("historyDrawer"); self.history_drawer.setParent(root)
        drawer = vbox(self.history_drawer, (21, 20, 21, 18), 12); row = hbox()
        row.addWidget(label("最近记录", "section_title"), 1)
        close = Button("", "close", "icon", self.history_drawer.hide); close.setAccessibleName("关闭最近记录"); close.setFixedWidth(28); row.addWidget(close); drawer.addLayout(row)
        self.drawer_view = SummaryView(self.bridge, compact=True); self.drawer_view.open_requested.connect(self.open_task); drawer.addWidget(self.drawer_view, 1)
        self.drawer_view.locate_requested.connect(self.open_task)
        self.drawer_view.block_reason=self.history_management_block
        self.drawer_view.guard=self.history_management_guard
        if not local:
            self.text_workspace.favorite.clicked.connect(self.favorite_current)
            self.client.history.updated.connect(lambda _:self.text_flow.update_controls())
            self.client.history.result.connect(lambda *_:self.text_flow.update_controls())
        drawer.addWidget(Button("查看全部翻译记录", "arrow", "quiet", lambda: self.navigate("history")))
        drawer.addWidget(label("打开仅回看；继续与重试须主动操作", "tiny")); self.history_drawer.hide()
        self.toast = label("", "toast", True); self.toast.setParent(root); self.toast.setAlignment(Qt.AlignCenter); self.toast.hide()
        self.toast_timer = QTimer(self); self.toast_timer.setSingleShot(True); self.toast_timer.timeout.connect(self.toast.hide)
        self.escape = QShortcut(QKeySequence("Escape"), self); self.escape.activated.connect(self.dismiss_overlays)
        self.translate_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        self.translate_shortcut.activated.connect(self.submit_current_page)
        self.enter_shortcut = QShortcut(QKeySequence('Ctrl+Enter'),self)
        self.enter_shortcut.activated.connect(self.submit_current_page)
        self.quit_action = QAction('退出本地翻译器',self)
        self.quit_action.setShortcut(QKeySequence.Quit); self.quit_action.setMenuRole(QAction.QuitRole)
        self.quit_action.triggered.connect(self.close)
        file_menu=self.menuBar().addMenu('文件')
        self.close_action=QAction('关闭窗口',self);self.close_action.setShortcut(QKeySequence.Close)
        self.close_action.triggered.connect(self.close);file_menu.addAction(self.close_action)
        file_menu.addAction(self.quit_action)
        self.allow_quit = False
        QApplication.instance().installEventFilter(self)
        self.bridge.state_changed.connect(self.update_status)
        (self.text_workspace if local else self.text_flow).controls_changed.connect(self.update_status)
        self.document_page.controls_changed.connect(self.update_status)
        self.image_page.controls_changed.connect(self.update_status)
        self.client.event.connect(self.on_event); self.client.stopped.connect(self.on_stopped)
        if local:
            self.client.snapshot.connect(self.read_history_snapshot)
        self.theme.apply(); self.navigate("translate"); self.select_tab(0); self.update_status()
        if connect_worker: QTimer.singleShot(0, self.client.connect_worker)

    def apply_preferences(self):
        value = self.client.preferences.description
        if not value or value['blocked'] or value['dirty'] or value['unconfirmed']:
            return
        fields = value['saved_values']
        if self.local:
            for page in (self.text_workspace, self.document_page, self.image_page, self.audio_workspace):
                page.apply_preferences(fields)
            return
        flow = self.text_flow
        if not flow.saved and not flow.page.source.toPlainText() and not flow.draft['text']:
            flow.draft['direction'] = fields['direction']
            flow.restore_editor(flow.draft)
        for page in (self.document_page, self.image_page):
            if not page.saved and not page.selection:
                page.direction.setCurrentIndex(page.direction.findData(fields['direction']))

    def eventFilter(self, watched, event):
        if watched is QApplication.instance() and event.type()==QEvent.Quit and not self.allow_quit:
            self.close(); return True
        return super().eventFilter(watched,event)

    def history_management_block(self):
        reason = '' if self.local else self.text_flow.dictionary_transfer_block()
        if reason:
            return reason
        task = self.client.current_task
        if task and task.get('drafts'):
            return '请先应用或放弃已保存的正文校正草稿，再管理记录。'
        return ''

    def history_management_guard(self):
        return (not self.history_management_block() and self.client.can_change_task() and
                (self.local or self.text_flow.protect_draft()))

    def favorite_current(self):
        saved=self.text_flow.saved
        row=self.client.history.records.get(saved['task']['id']) if saved else None
        if row and self.history_management_guard():self.client.history.manage(row,dict(favorite=not row['favorite']))

    def submit_current_page(self):
        if QApplication.activePopupWidget() or QApplication.activeModalWidget(): return
        if self.current_section=='dictionary': self.dictionary_page.query('enter')
        elif self.current_section=='audio': self.audio_workspace.start_translation()
        elif self.current_section=='translate' and self.current_tab==0:
            self.text_workspace.start_translation() if self.local else self.text_flow.translate()
        elif self.current_section=='translate' and self.current_tab==1 and self.document_container.currentIndex()==1:
            self.document_page.start_translation()
        elif self.current_section=='translate' and self.current_tab==2 and self.image_container.currentIndex()==1:
            self.image_page.start_translation()

    def selection_to_dictionary(self, raw):
        self.navigate('dictionary'); self.dictionary_page.prefill(raw)

    def dictionary_to_translation(self, raw, direction):
        if self.local:
            if self.text_workspace.transfer(raw, direction):
                self.navigate('translate'); self.select_tab(0)
            else:
                self.notify('当前翻译草稿已有原文。清空后可从词典带入。')
            return
        if self.text_flow.accept_dictionary(raw,direction):
            self.navigate('translate'); self.select_tab(0)
        else:
            self.notify(self.text_flow.message or '已保留当前翻译草稿，未替换。')

    def compatibility_container(self, page, back_title, callback):
        container = QWidget(); box = vbox(container, (24, 16, 24, 16), 10)
        row = hbox(); row.addWidget(Button(back_title, "back", "quiet", callback)); row.addStretch()
        row.addWidget(label("既有真实流程 · T01 兼容入口", "tiny")); box.addLayout(row)
        # Both axes scroll for dense legacy OCR controls. R6 reader migration is T04.
        page.setObjectName("compatibility"); page.setMinimumWidth(1060 if isinstance(page, DocumentPage) else 820)
        page.setMinimumHeight(660 if isinstance(page, DocumentPage) else 510)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(page); box.addWidget(scroll, 1)
        return container

    def navigate(self, section):
        if section!=self.current_section and not self.protect_navigation():
            self.nav_buttons[self.current_section].setChecked(True);return
        old = self.active_page()
        if hasattr(old, "leave_page"): old.leave_page()
        self.toast.hide()
        self.current_section = section; self.nav_buttons[section].setChecked(True)
        self.page_stack.setCurrentWidget(self.pages[section]); self.history_drawer.hide()
        page = self.active_page()
        if hasattr(page, "enter_page"): page.enter_page()
        if self.environment_page and section in ('settings', 'audio'):
            self.environment_page.refresh()
        self.recolor()

    def active_page(self):
        if self.current_section == "translate":
            return (self.text_workspace, self.document_container.currentWidget(), self.image_container.currentWidget())[self.current_tab]
        return self.pages[self.current_section]

    def open_dictionary_resources(self):
        self.navigate('settings')
        if self.local and self.current_section=='settings':
            self.settings_page.open_section('services')

    def select_tab(self, index):
        if index!=self.current_tab and not self.protect_navigation():
            self.tab_buttons[self.current_tab].setChecked(True);return
        old = self.active_page()
        if hasattr(old, "leave_page"): old.leave_page()
        self.current_tab = index; self.tab_buttons[index].setChecked(True)
        self.translation_stack.setCurrentIndex(index)
        self.active_page().enter_page(); self.recolor()
        if self.history_drawer.isVisible(): self.drawer_view.set_kind(("文字", "文档", "图片")[index])

    def select_document(self, path):
        if self.local:
            self.document_page.select_path(path)
            return
        if not self.text_flow.protect_draft() or not self.document_page.select_path(path):
            self.notify("请等待工作进程就绪、当前操作结束及 OCR 草稿保存后再选择文档。")
        self.document_container.setCurrentIndex(1)

    def protect_navigation(self):
        # Navigation can read another page while a long operation runs; local
        # drafts and IME still require their one owner to finish saving first.
        return self.preferences_panel.guard_switch() and self.resource_panel.guard_switch() and self.task_edit_guard()

    def task_edit_guard(self, *, flush=True):
        if not self.audio_workspace.guard_switch():
            return False
        if self.local:
            return True
        return (self.document_page.guard_switch() and self.image_page.guard_switch()
                and self.text_flow.guard_switch() and (not flush or self.text_flow.flush_draft()))

    def select_image(self,path):
        if self.local:
            self.image_page.select_path(path)
            return
        if self.text_flow.protect_draft():
            if not self.image_page.select_path(path):self.notify('请等待当前操作及草稿保存完成后再更换图片。')

    def paste_image(self):
        if self.current_section!='translate' or self.current_tab!=2:return
        if self.local:
            self.image_page.paste()
            return
        if self.text_flow.protect_draft():
            accepted=self.image_page.paste()
            if not accepted:self.image_upload.note.setText(self.image_page.status.text())

    def open_task(self, identity, locator=None):
        if self.local:
            if not self.record_request and self.client.state in ('ready','running','cancelling'):
                self.record_request = self.client.send('workbench.read', task_id=identity, locator=locator)
                if self.record_request:
                    self.record_identity = identity
                    self.history_drawer.hide()
                    return
            self.notify('暂时无法打开此记录，请稍后重试。')
            return
        if self.client.can_change_task() and self.text_flow.protect_draft() and self.bridge.open_task(identity,locator): self.history_drawer.hide()
        else: self.notify("任务尚未切换：请等待操作结束与 OCR 草稿保存确认。")

    def read_history_snapshot(self, snapshot):
        owner = self.client.snapshot_context or {}
        if self.record_request and owner.get('request')==self.record_request and 'workbench_record' in snapshot:
            if self.record_dialog:
                self.record_dialog.close()
            record = snapshot['workbench_record']
            identity = self.record_identity
            open_audio = (lambda:self.open_audio_record(identity)) if record['kind']=='音频' else None
            self.record_dialog = open_record(self, record, open_audio)
            self.record_dialog.finished.connect(lambda _: self.clear_record_dialog())

    def clear_record_dialog(self):
        self.record_dialog = None

    def open_audio_record(self, identity):
        if self.client.can_change_task() and self.bridge.open_task(identity):
            self.record_dialog.close()
        else:
            self.notify('请等待当前操作结束，再打开音频工作区。')

    def apply_history_location(self, result):
        if result.get('status') != 'located':
            self.notify(result.get('message','请刷新搜索后重新定位。'))
            self.history_locations.append(dict(status='stale'))
            return
        locator = result['locator']
        task = self.client.current_task or {}
        if (task.get('id'), task.get('revision')) != (result['task_id'], result['task_revision']):
            self.notify('当前内容已变化，请刷新搜索后重新定位。')
            return
        unit = locator['unit_type']
        located = True
        if unit in ('page','unit'):
            page = self.image_page if unit=='unit' else self.document_page
            index = page.pages.findData(locator['unit_id'])
            selected = next((p for p in (task.get('image') or task.get('document') or {}).get(
                'units' if unit=='unit' else 'pages',[]) if p['id']==locator['unit_id']),None)
            located = bool(selected and selected['revision']==locator['unit_revision'] and index>=0)
            if located:
                page.pages.setCurrentIndex(index)
                page.read_page()
                (page.output if locator['field']=='译文' else page.input).setFocus(Qt.OtherFocusReason)
        elif unit=='cue':
            located = self.audio_workspace.locate_history(locator)
        self.history_locations.append(dict(status='located' if located else 'stale',locator=locator))
        self.history_locations[:] = self.history_locations[-64:]
        self.notify('已定位保存的命中内容。' if located else '命中单元已更新，请刷新后重新定位。')

    def route_opened_task(self, kind):
        if kind == "音频":
            self.navigate("audio"); self.audio_page.open_session(0 if (self.client.current_task or {}).get('audio', {}).get('recording') else 1); return
        self.navigate("translate"); self.select_tab(1 if kind == "文档" else 2 if kind=='图片' else 0)
        if kind == "文档": self.document_container.setCurrentIndex(1)
        elif kind=='图片':self.image_container.setCurrentIndex(1)
        else: self.text_container.setCurrentIndex(0)

    def toggle_history_drawer(self):
        if self.history_drawer.isVisible(): self.history_drawer.hide(); return
        self.drawer_view.set_kind('音频' if self.current_section=='audio' else ("文字", "文档", "图片")[self.current_tab]); self.bridge.refresh()
        self.position_overlays(); self.history_drawer.show(); self.history_drawer.raise_()

    def update_status(self):
        state = self.bridge.state
        self.connection_label.setText(LABELS[self.client.state])
        phase = PHASES.get(state.phase, state.phase)
        saved = {"saved": "已保存", "blocked": "保存受阻", "unsaved": "尚未落盘", "unsubmitted": "尚未提交任务"}[state.save_status]
        self.task_label.setText(saved if state.task_id is None else f"任务：{phase} · {state.completed}/{state.total} 段 · {saved}")
        self.task_label.setVisible(not self.local or self.current_section=='audio' and bool(state.task_id))
        if not self.local and self.current_section=='translate' and self.current_tab==0 and self.text_flow.saved is None and state.task_id:
            self.task_label.setText(f"当前文字：未提交草稿 · 后台记录：{phase} · {state.completed}/{state.total} 段 · {saved}")
        workbench = bool(self.client.operation and self.client.operation_kind.startswith('workbench.'))
        self.cancel_button.setVisible(not self.local or (state.can_cancel and not workbench))
        self.cancel_button.setEnabled(state.can_cancel and not (self.local and workbench))
        self.progress.setVisible(self.client.state in ("running", "cancelling") or state.total > 0)
        self.progress.setRange(0, max(1, state.total)); self.progress.setValue(state.completed)
        self.progress.setFormat("已确认 %v / %m 段" if state.total else "当前操作进行中（非翻译完成状态）")
        error = self.client.connection_error_text() or ("" if self.local else state.error or "")
        self.error_label.setText(error); self.error_label.setVisible(bool(error))
        if not self.local:
            self.dictionary_page.transfer_block = self.text_flow.dictionary_transfer_block()
        self.dictionary_page.refresh()
        for view in (self.history_page,self.drawer_view):view.update_controls()
        if self.local:
            return
        saved=self.text_flow.saved
        record=self.client.history.records.get(saved['task']['id']) if saved else None
        favorite=self.text_workspace.favorite
        favorite.setEnabled(bool(record and record['kind']=='文字' and not record['trashed_at'] and not self.history_management_block()))
        favorite.setToolTip(self.history_management_block() or ('取消收藏' if record and record['favorite'] else '收藏此记录'))
        favorite.setAccessibleName('取消收藏当前文字' if record and record['favorite'] else '收藏当前文字')
        favorite.setText('已收藏' if record and record['favorite'] else '')

    def show_diagnostics(self):
        dialog = QDialog(self); dialog.setWindowTitle("工作进程状态与连接诊断"); dialog.resize(640, 450)
        box = vbox(dialog, (24, 24, 24, 24), 14)
        box.addWidget(label("工作进程就绪与业务任务完成分别记录。无模型验证不生成译文。", "secondary", True))
        output = QPlainTextEdit(); output.setReadOnly(True); box.addWidget(output, 1)
        def refresh():
            output.setPlainText(json.dumps({"connection": self.client.state, "failure": self.client.failure,
                "startup_error": self.client.startup_error,
                "worker_stderr": self.client.stderr.decode("utf-8", errors="replace"),
                "diagnostics": self.client.diagnostic, "task_state": self.bridge.state.__dict__}, ensure_ascii=False, indent=2))
        self.bridge.state_changed.connect(refresh)
        buttons = hbox(spacing=10)
        for button in (Button("无模型验证", callback=lambda: self.client.start_probe(steps=40, interval_ms=100, lease_ms=20000)),
                       Button("重新连接", callback=self.client.connect_worker), Button("安全取消", callback=self.client.cancel),
                       Button("关闭", callback=dialog.accept)): buttons.addWidget(button)
        box.addLayout(buttons); refresh(); self.recolor(); dialog.exec()
        self.bridge.state_changed.disconnect(refresh); dialog.deleteLater()

    def notify(self, text):
        self.toast.setText(text); self.position_overlays(); self.toast.show(); self.toast.raise_(); self.toast_timer.start(5000)

    def recolor(self):
        colors = QApplication.instance().property("translator_colors")
        if colors:
            for child in self.findChildren(IconLabel) + self.findChildren(Button): child.recolor(colors)

    def position_overlays(self):
        height = self.centralWidget().height()
        self.history_drawer.setGeometry(self.width()-396, 68, 376, max(400, height-88))
        self.toast.setGeometry(228, max(0, height-130), max(200, self.width()-456), 64)

    def dismiss_overlays(self):
        popup = QApplication.activePopupWidget()
        if popup:
            popup.close(); return
        if self.history_drawer.isVisible():
            self.history_drawer.hide();self.tab_buttons[self.current_tab].setFocus();return
        if self.current_section=='history' and self.client.history.views['history']['state']=='querying':
            self.client.history.cancel('history');self.history_page.search.setFocus();return
        if self.current_section=='audio' and self.audio_workspace.operation_request:
            self.audio_workspace.cancel_owned(); return
        if self.current_section=='dictionary' and self.client.dictionary.state=='querying':
            self.client.dictionary.cancel(); return
        if self.current_section=='settings' and (self.client.resources.expected or self.client.managed.expected):
            self.client.cancel(); return
        self.history_drawer.hide(); self.toast.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "history_drawer"): self.position_overlays()

    def showEvent(self, event):
        super().showEvent(event); self.evidence["desktop_window_shown"] = True

    def on_event(self, event):
        if self.record_request and event['request_id']==self.record_request and event['type'] in ('task.done','error'):
            self.record_request = None
            if event['type']=='error':
                self.notify(event['error'].get('message','暂时无法打开此记录。'))
        if event['type']=='ready':
            self.evidence['worker_pid']=event['diagnostics']['worker_pid']
            self.evidence['ready_confirmed_at']=time.time()
        compact = {key: ("[bounded snapshot chunk; content not logged]" if key == "data" else value) for key, value in event.items()}
        self.evidence["events"].append(dict(compact, received_elapsed_seconds=round(time.monotonic()-self.started_monotonic, 6)))
        self.evidence["events"] = self.evidence["events"][-4096:]
        if event["type"] == "operation.started" and event.get("result_kind") == "task.start": self.evidence["inference"] = True
        if event["type"] == "progress" and "confirmed" in event:
            self.progress.setRange(0, 40); self.progress.setValue(event["confirmed"]); self.progress.setFormat("无模型验证 · %v / %m 步")
        if self.screenshot_path and event["type"] == "ready": QTimer.singleShot(300, self.capture_window)

    def capture_window(self):
        path = Path(self.screenshot_path); path.parent.mkdir(parents=True, exist_ok=True); self.grab().save(str(path))

    def closeEvent(self, event):
        if not self.theme.prepare_close(self): event.ignore(); return
        if not self.audio_workspace.prepare_close(self.close): event.ignore(); return
        if not self.preferences_panel.prepare_close(self.close): event.ignore(); return
        if not self.resource_panel.prepare_close(self.close): event.ignore(); return
        if not self.local:
            if not self.image_page.prepare_close(self.close): event.ignore(); return
            if not self.document_page.prepare_close(self.close): event.ignore(); return
            if not self.text_flow.prepare_close(self.close): event.ignore(); return
        if self.client.process and self.client.process.state() != QProcess.NotRunning:
            event.ignore()
            if not self.close_pending:
                self.close_pending = True; self.client.shutdown()
            return
        self.evidence.update(window_close_and_worker_shutdown_confirmed=self.client.failure is None,
                             worker_failure=self.client.failure, closed_at=datetime.now(timezone.utc).isoformat())
        self.evidence.update(window_size=[self.width(),self.height()],
            minimum_size=[self.minimumWidth(),self.minimumHeight()],
            dictionary_config=str(self.client.dictionary_config) if self.client.dictionary_config else None,
            dictionary_input_method_events=self.dictionary_page.ime_events,
            dictionary_submissions=self.dictionary_page.submissions,
            dictionary_ui_timings=self.dictionary_page.timings,
            shutdown_confirmed=self.client.shutdown_confirmed,
            history_input_method_events=self.history_page.ime_events+self.drawer_view.ime_events,
            history_submissions=self.history_page.submissions+self.drawer_view.submissions,
            history_locations=self.history_locations,
            history_rejected_responses=self.client.history.rejected,
            document_input_method_events=getattr(self.document_page,'ime_events',[]),
            document_rejected_snapshots=getattr(self.document_page,'rejected_snapshots',0),
            audio_input_method_events=self.audio_workspace.ime_events,
            image_input_method_events=getattr(self.image_page,'ime_events',[]),
            image_rejected_snapshots=getattr(self.image_page,'rejected_snapshots',0),
            preferences_description=self.client.preferences.description or self.client.preferences.last_description,
            managed_description=self.client.managed.description or self.client.managed.last_description,
            resource_description=self.client.resources.description or self.client.resources.last_description,
            resource_guard_events=self.resource_panel.guard_events,
            resource_rejected_responses=self.client.resources.rejected,
            popup_interactions=[dict(name=combo.accessibleName() or combo.currentText(),
                                     events=combo.popup_events)
                                for combo in self.findChildren(Combo) if combo.popup_events],
            preview_data_root=str(self.client.preview_data_root),
            frame_geometry=[self.frameGeometry().x(),self.frameGeometry().y(),self.frameGeometry().width(),self.frameGeometry().height()])
        if self.evidence_path:
            path = Path(self.evidence_path); path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.screenshot_path:
            path = Path(self.screenshot_path)
            self.grab().save(str(path.with_name(path.stem + '-closed' + path.suffix)))
        if not self.local:
            self.text_flow.store.close()
        self.allow_quit = True
        event.accept()
        QTimer.singleShot(0,QApplication.instance().quit)

    def on_stopped(self, code):
        if self.close_pending: QTimer.singleShot(0, self.close)


def main(argv=None):
    parser = argparse.ArgumentParser(description=f"Shared development preview {PREVIEW_ID} ({platform.system()})")
    for option in ("evidence", "screenshot", "task-root", "boundary-helper", "pdf-helper", "pdf-library", "preferences", "endpoint", "dictionary-config", "image-helper", "preview-data-root", "install-catalog", "catalog-sha256", "translation-models-root"):
        parser.add_argument("--"+option)
    args = parser.parse_args(argv)
    identity_path = Path(__file__).resolve().parents[1] / 'build-identity.json'
    identity = json.loads(identity_path.read_bytes()) if identity_path.is_file() else {}
    project = Path(identity.get('project_root', identity_path.parents[2]))
    data_root = data_directory(project, identity, args.preview_data_root, args.task_root)
    cache = no_links(data_root/'temporary')
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ.update(TMPDIR=str(cache), TMP=str(cache), TEMP=str(cache), XDG_CACHE_HOME=str(cache / 'cache'))
    app = QApplication([]); app.setApplicationName(f"本地翻译器 {PREVIEW_ID}"); app.setStyle("Fusion")
    # Only the root's completed close guard ends the app. Native panels and
    # temporary modal dialogs do not own application shutdown.
    app.setQuitOnLastWindowClosed(False)
    window = PreviewWindow(args.evidence, args.screenshot, args.task_root, args.boundary_helper,
                           args.pdf_helper, args.pdf_library, args.preferences, endpoint=args.endpoint,
                           dictionary_config=args.dictionary_config, image_helper=args.image_helper, preview_data_root=args.preview_data_root, install_catalog=args.install_catalog,
                           catalog_sha256=args.catalog_sha256, translation_models_root=args.translation_models_root); window.show()
    code = app.exec()
    from shiboken6 import delete
    delete(window); app.processEvents(); delete(app)
    raise SystemExit(code)
