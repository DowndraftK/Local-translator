"""One shared Qt Widgets shell; all future business capabilities stay disabled."""
import argparse
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QProcess, qVersion, QCoreApplication
import PySide6
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QListWidget,
    QMainWindow, QPlainTextEdit, QProgressBar, QPushButton, QStackedWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView)

from . import PRODUCT_VERSION, PREVIEW_ID
from .client import WorkerClient
from .text_page import TextPage
from .document_page import DocumentPage
from .protocol import PROTOCOL

LABELS = {"connecting": "正在等待本地工作进程确认启动…", "ready": "本地工作进程已就绪",
          "running": "正在执行本次操作", "cancelling": "正在安全取消，等待尾部确认…",
          "closing": "正在安全结束本地工作进程…", "failed": "本地验证失败，请查看诊断",
          "disconnected": "本地工作进程已停止"}
FEATURES = [("diagnostics", "能力与诊断", "B01"), ("lifecycle_probe", "启动 / 进度 / 安全取消", "B01"),
            ("text_translation", "双向文字翻译", "B02"), ("task_read_resume", "保存 / 打开 / 主动继续 / 导出", "B02"),
            ("pdf_txt", "PDF / TXT 与来源阅读", "B03"), ("ocr", "显式 OCR 与校正", "B04"),
            ("speech_file", "文件语音识别", "B05"), ("refinement_playback", "校对 / 回放 / 字幕修订", "B06"),
            ("microphone", "麦克风与实体恢复", "B07"), ("service_unload", "服务与内存生命周期", "B08"),
            ("runtime_installer", "固定运行包与安装修复", "B09"), ("public_download", "公开下载与最终发行", "B11 / B12")]


class PreviewWindow(QMainWindow):
    def __init__(self, evidence_path=None, screenshot_path=None, task_root=None, boundary_helper=None, pdf_helper=None, pdf_library=None):
        super().__init__()
        self.evidence_path, self.screenshot_path = evidence_path, screenshot_path
        self.started_monotonic = time.monotonic()
        identity_path = Path(__file__).resolve().parents[1] / "build-identity.json"
        self.identity = json.loads(identity_path.read_text(encoding="utf-8")) if identity_path.exists() else {
            "product_version": PRODUCT_VERSION, "preview_id": PREVIEW_ID, "build_id": "development-unbuilt"}
        self.evidence = {"schema": 1, "candidate": self.identity,
            "started_at": datetime.now(timezone.utc).isoformat(), "platform": platform.system(),
            "qt_version": qVersion(), "qpa_platform": QApplication.platformName(),
            "desktop_window_shown": False, "events": [], "normal_exit": None,
            "inference": False, "production_task_access": False,
            "runtime": {"python_executable": sys.executable, "python_prefix": sys.prefix,
                "pyside6_module": PySide6.__file__, "qt_plugin_paths": QCoreApplication.libraryPaths()}}
        self.close_pending = False
        package = Path(__file__).resolve().parents[1]
        project = package.parents[1]
        task_root = Path(task_root or self.identity.get("preview_task_root") or project / "artifacts/b02-20261004/tasks")
        boundary_helper = Path(boundary_helper or package / "shared/platform/macos/text-boundaries")
        if not boundary_helper.is_file() and sys.platform == "darwin":
            boundary_helper = project / "artifacts/b02-20261004/bin/text-boundaries"
        pdf_helper = Path(pdf_helper or package / "shared/platform/macos/pdf-facts")
        if not pdf_helper.is_file() and sys.platform == "darwin": pdf_helper = project / "artifacts/b03-20261005/pdf-facts"
        pdf_library = Path(pdf_library or package / "vendor/pdfium")
        self.client = WorkerClient(self, task_root=task_root.absolute(), boundary_helper=boundary_helper,
                                   pdf_helper=pdf_helper, pdf_library=pdf_library)
        self.evidence["preview_task_root"] = str(task_root)
        self.setWindowTitle("本地翻译器 · B04 文档核对预览")
        self.resize(1100, 760); self.setMinimumSize(860, 600)
        root = QWidget(); self.setCentralWidget(root)
        layout = QHBoxLayout(root); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        sidebar = QFrame(); sidebar.setObjectName("sidebar"); sidebar.setFixedWidth(245)
        side = QVBoxLayout(sidebar); side.setContentsMargins(24, 30, 20, 24)
        brand = QLabel("本地翻译器"); brand.setObjectName("brand"); side.addWidget(brand)
        small = QLabel("共享桌面界面 · B04"); small.setObjectName("muted"); side.addWidget(small)
        self.nav = QListWidget(); self.nav.setObjectName("navigation")
        self.nav.addItems(["连接与能力", "文字翻译", "文档与图片", "录音与字幕", "资源与安装"])
        side.addWidget(self.nav); side.addStretch()
        foot = QLabel("0.2.11 · 开发预览\n本次实施至 B04\n旧 Mac 应用独立保留")
        foot.setWordWrap(True); foot.setObjectName("muted"); side.addWidget(foot)
        layout.addWidget(sidebar)
        main = QVBoxLayout(); main.setContentsMargins(30, 28, 30, 24); main.setSpacing(14)
        layout.addLayout(main)
        title = QLabel("连接与能力"); title.setObjectName("title"); main.addWidget(title)
        note = QLabel("文字、PDF/TXT与显式OCR可独立保存、核对和回看来源。打开不自动推理。")
        note.setWordWrap(True); note.setObjectName("muted"); main.addWidget(note)
        identity = QLabel(f"候选 {self.identity['build_id']}  ·  协议 {PROTOCOL['major']}.{PROTOCOL['minor']}  ·  {platform.system()} / {platform.machine()}")
        identity.setObjectName("muted"); main.addWidget(identity)
        self.stack = QStackedWidget(); main.addWidget(self.stack, 1)
        page = QWidget(); box = QVBoxLayout(page); box.setContentsMargins(0, 0, 0, 0); box.setSpacing(12)
        self.status = QLabel(LABELS["disconnected"]); self.status.setObjectName("status"); box.addWidget(self.status)
        row = QHBoxLayout()
        self.start_button = QPushButton("开始无模型验证"); self.start_button.setObjectName("primary")
        self.cancel_button = QPushButton("安全取消")
        self.reconnect_button = QPushButton("重新连接")
        self.stop_button = QPushButton("停止工作进程")
        for button in (self.start_button, self.cancel_button, self.reconnect_button, self.stop_button):
            row.addWidget(button)
        box.addLayout(row)
        self.progress = QProgressBar(); self.progress.setRange(0, 40); self.progress.setValue(0)
        self.progress.setFormat("无模型验证 · 已确认 %v / %m 步"); box.addWidget(self.progress)
        self.table = QTableWidget(len(FEATURES), 3); self.table.setHorizontalHeaderLabels(["能力", "当前预览", "批次"])
        self.table.verticalHeader().setVisible(False); self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        for index, (_, label, batch) in enumerate(FEATURES):
            self.table.setItem(index, 0, QTableWidgetItem(label))
            self.table.setItem(index, 1, QTableWidgetItem("等待能力确认"))
            self.table.setItem(index, 2, QTableWidgetItem(batch))
        box.addWidget(self.table, 1)
        self.timeline = QPlainTextEdit(); self.timeline.setReadOnly(True); self.timeline.setMaximumHeight(100)
        self.timeline.setPlaceholderText("启动与取消记录将在这里显示。")
        self.timeline.document().setMaximumBlockCount(80); box.addWidget(self.timeline)
        self.stack.addWidget(page)
        self.text_page = TextPage(self.client, task_root, self); self.stack.addWidget(self.text_page)
        self.document_page = DocumentPage(self.client, task_root, self); self.stack.addWidget(self.document_page)
        for label, batch in [("录音与字幕", "B05—B07"), ("资源与安装", "B08—B11")]:
            placeholder = QWidget(); column = QVBoxLayout(placeholder)
            column.addStretch(); heading = QLabel(label); heading.setObjectName("title"); heading.setAlignment(Qt.AlignCenter)
            column.addWidget(heading)
            text = QLabel(f"尚未接入 · {batch}\n已有文档和语音任务请继续使用旧 Mac 应用。")
            text.setAlignment(Qt.AlignCenter); text.setObjectName("muted"); column.addWidget(text); column.addStretch()
            self.stack.addWidget(placeholder)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.currentTextChanged.connect(title.setText); self.nav.setCurrentRow(0)
        self.client.changed.connect(self.state_changed)
        self.client.event.connect(self.on_event); self.client.stopped.connect(self.on_stopped)
        self.start_button.clicked.connect(self.start_probe); self.cancel_button.clicked.connect(self.client.cancel)
        self.reconnect_button.clicked.connect(self.client.connect_worker); self.stop_button.clicked.connect(self.client.shutdown)
        self.state_changed("disconnected")
        QTimer.singleShot(0, self.client.connect_worker)

    def showEvent(self, event):
        super().showEvent(event)
        self.evidence["desktop_window_shown"] = True

    def state_changed(self, state):
        self.status.setText(LABELS[state])
        self.start_button.setEnabled(state == "ready")
        self.cancel_button.setEnabled(state == "running")
        self.reconnect_button.setEnabled(state in ("disconnected", "failed") and
            (not self.client.process or self.client.process.state() == QProcess.NotRunning))
        self.stop_button.setEnabled(state in ("ready", "running", "cancelling"))
        if state == "failed":
            self.timeline.appendPlainText("验证失败：" + str(self.client.failure))
        if state in ("connecting", "disconnected", "failed"):
            for index in range(len(FEATURES)):
                self.table.item(index, 1).setText("等待能力确认" if state == "connecting" else "未连接")

    def start_probe(self):
        self.progress.setValue(0)
        self.client.start_probe(steps=40, interval_ms=250, lease_ms=20000)

    def on_event(self, event):
        compact = dict(event)
        if "data" in compact: compact["data"] = "[bounded snapshot chunk; content not logged]"
        self.evidence["events"].append(dict(compact, received_elapsed_seconds=round(time.monotonic() - self.started_monotonic, 6)))
        self.evidence["events"] = self.evidence["events"][-256:]
        kind = event["type"]
        if kind == "ready":
            self.timeline.appendPlainText("本地工作进程已真实确认启动；未加载模型或打开旧任务。")
            for index, (key, _, _) in enumerate(FEATURES):
                state = event["diagnostics"]["capabilities"].get(key)
                self.table.item(index, 1).setText("已就绪" if state == "ready" else "部分语言可用，缺英语资源" if state == 'partial_offline_languages' else "TXT就绪，PDF适配器缺失" if state == "txt_ready_pdf_adapter_missing" else "尚未实现")
            if self.screenshot_path:
                QTimer.singleShot(300, self.capture_window)
        elif kind == "progress":
            self.progress.setValue(event["confirmed"])
        elif kind == "cancel.accepted":
            self.timeline.appendPlainText("已接受取消，等待确认尾部。")
        elif kind == "operation.finished":
            if event["result"]["kind"] == "synthetic-lifecycle-probe":
                count = len(event["result"]["confirmed_steps"])
                status = "已安全取消" if event["status"] == "cancelled" else "验证完成" if event["status"] == "completed" else "租约已到期"
                self.timeline.appendPlainText(f"{status} · 保留 {count} 个已确认步骤 · 尾部已排空。")
            else:
                self.timeline.appendPlainText("本次文字/服务操作已排空：" + event["status"])
                if event["result"]["kind"] == "text-task": self.evidence["inference"] = True
            if self.screenshot_path:
                QTimer.singleShot(200, self.capture_window)
        elif kind == "shutdown.confirmed":
            self.timeline.appendPlainText("工作进程已确认安全结束。")

    def capture_window(self):
        path = Path(self.screenshot_path); path.parent.mkdir(parents=True, exist_ok=True)
        self.grab().save(str(path))

    def closeEvent(self, event):
        if not self.document_page.prepare_close(self.close):
            event.ignore(); return
        if self.client.process and self.client.process.state() != QProcess.NotRunning:
            event.ignore()
            if not self.close_pending:
                self.close_pending = True; self.client.shutdown()
            return
        # Actual OS process exit must be confirmed externally; this callback only
        # proves window close + worker drain, not later Qt/Python teardown.
        self.evidence["window_close_and_worker_shutdown_confirmed"] = self.client.failure is None
        self.evidence["worker_failure"] = self.client.failure
        self.evidence["closed_at"] = datetime.now(timezone.utc).isoformat()
        if self.evidence_path:
            path = Path(self.evidence_path); path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        event.accept()

    def on_stopped(self, code):
        if self.close_pending:
            QTimer.singleShot(0, self.close)


STYLE = """
QWidget { font-size: 13px; color: #233444; background: #fafcfe; }
#sidebar { background: #edf3f8; border-right: 1px solid #d8e2eb; }
#brand { font-size: 23px; font-weight: 600; background: transparent; }
#title { font-size: 25px; font-weight: 600; }
#muted { color: #607487; line-height: 1.5; background: transparent; }
#navigation { border: none; background: transparent; margin-top: 24px; }
#navigation::item { padding: 14px 10px; margin: 3px 0; border-radius: 7px; }
#navigation::item:selected { background: #d7e7f5; color: #164f79; }
#status { padding: 13px; background: #e9f4ed; border-radius: 7px; font-weight: 600; }
QPushButton { padding: 10px 12px; border: 1px solid #c8d6e1; border-radius: 7px; background: white; }
QPushButton:hover { background: #e8f2fa; }
QPushButton:disabled { color: #9aa9b5; background: #f3f6f8; }
#primary { color: white; background: #246999; border-color: #246999; }
#primary:disabled { color: #8da0af; background: #e4ebf0; border-color: #d7e0e8; }
QTableWidget { background: white; border: 1px solid #dbe4ec; gridline-color: #edf1f5; }
QHeaderView::section { padding: 8px; background: #edf3f8; border: none; font-weight: 600; }
QPlainTextEdit { border: 1px solid #dbe4ec; border-radius: 6px; padding: 6px; background: white; }
QProgressBar { border: 1px solid #d9e3ec; border-radius: 5px; text-align: center; background: white; height: 23px; }
QProgressBar::chunk { background: #a5d6be; }
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description="B02 shared text development preview")
    parser.add_argument("--evidence")
    parser.add_argument("--screenshot")
    parser.add_argument("--task-root")
    parser.add_argument("--boundary-helper")
    parser.add_argument("--pdf-helper")
    parser.add_argument("--pdf-library")
    args = parser.parse_args(argv)
    app = QApplication([]); app.setApplicationName("本地翻译器 B02预览")
    app.setStyle("Fusion"); app.setStyleSheet(STYLE)
    window = PreviewWindow(args.evidence, args.screenshot, args.task_root, args.boundary_helper, args.pdf_helper, args.pdf_library); window.show()
    code = app.exec()
    # Destroy the widget/AX tree while QApplication and its native accessibility
    # bridge still exist, rather than during Python interpreter finalization.
    from shiboken6 import delete
    delete(window)
    app.processEvents()
    delete(app)
    raise SystemExit(code)
