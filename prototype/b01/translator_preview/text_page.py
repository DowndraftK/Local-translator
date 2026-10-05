"""Shared text page. Business state and scheduling live exclusively in the worker core."""
import uuid
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QLabel,
    QPushButton, QPlainTextEdit, QListWidget, QListWidgetItem, QFileDialog, QSplitter)

from translator_core.contracts import MAX_INPUT_BYTES, bilingual_text, sha
from translator_core.store import atomic_write, safe_file


class TextPage(QWidget):
    task_kind = "文字"
    def __init__(self, client, task_root, parent=None):
        super().__init__(parent)
        self.client, self.root = client, Path(task_root)
        self.saved = None; self.pending = False; self.pending_kind = None; self.export_target = None
        self.pending_request = self.export_request = self.operation_request = None
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(10)
        actions = QHBoxLayout()
        self.new = QPushButton("新建文字任务")
        self.direction = QComboBox(); self.direction.addItem("英文 → 简体中文", "en-zh"); self.direction.addItem("中文 → 英文", "zh-en")
        self.check = QPushButton("检查本机翻译服务")
        for item in (self.new, self.direction, self.check): actions.addWidget(item)
        actions.addStretch(); outer.addLayout(actions)
        splitter = QSplitter(Qt.Horizontal)
        left = QWidget(); column = QVBoxLayout(left); column.setContentsMargins(0, 0, 6, 0)
        column.addWidget(QLabel("已保存的预览任务"))
        self.listing = QListWidget(); column.addWidget(self.listing, 1)
        self.refresh = QPushButton("刷新任务列表"); self.open = QPushButton("打开所选任务")
        self.import_button = QPushButton("导入旧任务副本…")
        for button in (self.refresh, self.open, self.import_button): column.addWidget(button)
        right = QWidget(); box = QVBoxLayout(right); box.setContentsMargins(4, 0, 0, 0)
        self.info = QLabel("固定7B模型 · 新任务使用既有双向翻译配置\n原文中的指令作为待译内容。保存后修改原文或方向需另建任务。")
        self.info.setWordWrap(True); self.info.setObjectName("muted"); box.addWidget(self.info)
        self.input = QPlainTextEdit(); self.input.setPlaceholderText("输入待翻译文字，先保存任务，再开始翻译。")
        self.input.setAccessibleName("待翻译原文"); self.input.setMinimumHeight(95); self.input.setMaximumHeight(180); box.addWidget(self.input)
        row = QHBoxLayout()
        self.create = QPushButton("保存为新任务"); self.create.setObjectName("primary")
        self.save = QPushButton("保存当前状态")
        self.start = QPushButton("开始 / 主动继续"); self.start.setObjectName("primary")
        self.cancel_button = QPushButton("安全取消")
        for button in (self.create, self.save, self.start, self.cancel_button): row.addWidget(button)
        box.addLayout(row)
        self.status = QLabel("尚未保存 · 打开任务不会自动翻译")
        self.status.setWordWrap(True); box.addWidget(self.status)
        self.output = QPlainTextEdit(); self.output.setReadOnly(True); self.output.setAccessibleName("双语结果与段状态")
        self.output.setPlaceholderText("已确认的完整结果和未完成段将在这里显示。")
        box.addWidget(self.output, 1)
        self.export_button = QPushButton("导出完整双语 TXT…"); box.addWidget(self.export_button)
        splitter.addWidget(left); splitter.addWidget(right); splitter.setSizes([210, 650]); outer.addWidget(splitter, 1)
        self.notice = QLabel("任务保存在独立预览目录；旧任务只按你的选择导入副本。关闭窗口会等待保存与工作进程安全结束。")
        self.notice.setObjectName("muted"); self.notice.setWordWrap(True); outer.addWidget(self.notice)
        self.new.clicked.connect(self.new_task); self.create.clicked.connect(self.create_task)
        self.save.clicked.connect(lambda: self.send("task.save"))
        self.refresh.clicked.connect(lambda: self.send("task.list"))
        self.open.clicked.connect(self.open_task); self.listing.itemDoubleClicked.connect(lambda _: self.open_task())
        self.import_button.clicked.connect(self.import_task)
        self.start.clicked.connect(lambda: self.operate("task.start"))
        self.cancel_button.clicked.connect(self.client.cancel)
        self.check.clicked.connect(lambda: self.operate("service.check", direction=self.direction.currentData()))
        self.export_button.clicked.connect(self.export)
        self.client.changed.connect(self.update_controls)
        self.client.snapshot.connect(self.receive_snapshot); self.client.event.connect(self.receive_event)
        self.update_controls(self.client.state)

    def update_controls(self, state):
        ready = state == "ready" and not self.pending
        selected = self.saved is not None and self.saved.get("task") is not None
        self.input.setReadOnly(selected)
        self.direction.setEnabled(ready and not selected)
        for button in (self.new, self.refresh, self.open, self.import_button, self.check): button.setEnabled(ready)
        self.create.setEnabled(ready and not selected)
        self.save.setEnabled(ready and selected and not self.saved.get("save_blocked"))
        self.start.setEnabled(ready and selected and not self.saved.get("save_blocked"))
        self.cancel_button.setEnabled(state == "running")
        self.export_button.setEnabled(selected and state in ("ready", "running", "cancelling") and not self.pending)

    def send(self, kind, **fields):
        if self.client.state != "ready" or self.pending: return
        self.pending = True; self.pending_kind = kind; self.update_controls(self.client.state)
        self.pending_request = self.client.send(kind, **fields)
        if self.pending_request is None:
            self.pending = False; self.update_controls(self.client.state)

    def operate(self, kind, **fields):
        self.operation_request = self.client.start_operation(kind, **fields)
        return self.operation_request

    def new_task(self):
        self.saved = None; self.input.clear(); self.output.clear()
        self.status.setText("新任务 · 保存后才能开始；原任务保留在列表中。")
        self.update_controls(self.client.state)

    def create_task(self):
        raw = self.input.toPlainText().encode()
        if not raw.strip() or len(raw) > MAX_INPUT_BYTES:
            self.status.setText("请输入非空文字；当前预览上限为1MiB UTF-8。"); return
        name = uuid.uuid4().hex + ".txt"
        try: atomic_write(raw, self.root / "incoming" / name)
        except (OSError, ValueError) as exc: self.status.setText("输入尚未保存：" + str(exc)); return
        self.send("task.create", input_file=name, byte_count=len(raw), sha256=sha(raw), direction=self.direction.currentData())

    def open_task(self):
        item = self.listing.currentItem()
        if item: self.send("task.open", task_id=item.data(Qt.UserRole))

    def import_task(self):
        path = QFileDialog.getExistingDirectory(self, "选择旧任务UUID目录（原目录只读）")
        if path: self.send("task.import", source_directory=path)

    def export(self):
        if self.export_target is not None: return
        path, _ = QFileDialog.getSaveFileName(self, "保存完整双语TXT", "文字翻译-双语.txt", "UTF-8文字 (*.txt)")
        if not path: return
        self.export_target = Path(path)
        self.export_request = self.client.send("task.export")

    def receive_snapshot(self, snapshot):
        if "listing" in snapshot:
            selected = self.saved.get("task", {}).get("id") if self.saved else None
            self.listing.clear()
            for task in snapshot["listing"]:
                if task.get("kind", "文字") != self.task_kind: continue
                item = QListWidgetItem(task["name"] + "\n" + task["phase"])
                item.setData(Qt.UserRole, task["id"]); self.listing.addItem(item)
                if task["id"] == selected: self.listing.setCurrentItem(item)
            if snapshot.get("errors"): self.status.setText("\n".join(snapshot["errors"]))
            return
        task = snapshot.get("task")
        if task and task["kind"] != self.task_kind:
            self.saved = None; self.update_controls(self.client.state); return
        self.saved = snapshot
        if not task: return
        self.input.setPlainText(task["input"])
        self.direction.setCurrentIndex(0 if task["configuration"]["direction"] == "en-zh" else 1)
        recipe = task["configuration"]
        self.info.setText(f"{recipe['model']} · 配置 v{recipe['version']} · 原文与配置已绑定\n{task['name']}")
        job = task.get("text") or {}
        parts = job.get("segments", [])
        count = sum(p["state"] == "completed" for p in parts)
        phase = {"preparing": "正在分段", "running": "正在翻译", "completed": "全部完成", "partial": "部分失败",
                 "stopped": "已停止，等待主动继续", "failed": "任务未完成"}.get(job.get("phase"), "已保存原文")
        text = f"{phase} · 已完成 {count} / {len(parts)} 段 · 最近已保存序号 {snapshot['durable_revision']}"
        if snapshot.get("warning"): text += "\n" + snapshot["warning"]
        elif job.get("error"): text += "\n" + job["error"]
        self.status.setText(text); self.output.setPlainText(bilingual_text(task))
        self.update_controls(self.client.state)

    def receive_event(self, event):
        kind = event["type"]
        if kind == "ready": self.send("task.list")
        elif kind in ("task.done", "error"):
            if event["request_id"] not in (self.pending_request, self.export_request, self.operation_request): return
            completed_kind = self.pending_kind
            if event["request_id"] == self.pending_request:
                self.pending = False; self.pending_request = self.pending_kind = None
            if kind == "error":
                self.status.setText(event["error"].get("message", event["error"]["code"])); self.export_target = None
            self.update_controls(self.client.state)
            if kind == "task.done" and completed_kind in ("task.create", "task.import", "task.save", "document.create"):
                self.send("task.list")
        elif kind == "operation.finished":
            if event["request_id"] != self.operation_request: return
            self.operation_request = None
            detail = event.get("result", {}).get("detail")
            if event.get("result", {}).get("kind") == "service-diagnostic":
                if detail and "version" in detail:
                    self.status.setText(f"本机翻译服务 {detail['version']} 与固定模型匹配；检查没有推理。")
                else: self.status.setText("服务检查未完成：" + str(detail))
            elif detail and detail.get("error"): self.status.setText(detail["error"])
            if event["status"] == "lease_expired": self.status.setText("连接租约到期，已停止并保存确认结果；等待主动继续。")
            self.update_controls(self.client.state)
        elif kind == "task.exported":
            if event["request_id"] != self.export_request: return
            try:
                name = event["filename"]
                if len(name) != 36 or not name.endswith(".txt") or any(c not in "0123456789abcdef" for c in name[:-4]):
                    raise ValueError("导出身份无效")
                source = self.root / "exports" / name
                raw = safe_file(source, 32 * 1024 * 1024)
                if len(raw) != event["byte_count"] or sha(raw) != event["sha256"] or self.export_target is None:
                    raise ValueError("导出内容身份不匹配")
                atomic_write(raw, self.export_target); source.unlink()
                self.status.setText("完整双语TXT已导出：" + str(self.export_target))
            except (OSError, ValueError) as exc: self.status.setText("导出未完成：" + str(exc))
            finally: self.export_target = self.export_request = None
