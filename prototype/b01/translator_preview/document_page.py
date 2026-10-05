"""Shared document reader. Native engines never own task workflow or UI rules."""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QComboBox, QCheckBox, QPushButton, QFileDialog, QScrollArea
from translator_core.contracts import sha
from translator_core.documents import document_text, coverage
from translator_core.pdf_adapter import MAX_IMAGE_BYTES
from translator_core.store import safe_file
from .text_page import TextPage


class DocumentPage(TextPage):
    task_kind = "文档"

    def __init__(self, client, task_root, parent=None):
        self.selection = None; self.reading_page = None; self.image_revision = None
        super().__init__(client, task_root, parent)
        self.new.setText("选择PDF / TXT，另建任务…")
        self.new.clicked.disconnect(); self.new.clicked.connect(self.choose_file)
        self.create.setText("保存所选范围任务")
        self.start.setText("开始翻译 / 主动继续")
        self.input.setAccessibleName("当前物理页有效原文")
        self.output.setAccessibleName("按页双语阅读与来源状态")
        self.file_info = QLabel("请先选择文件；默认只提取文字层，扫描页不自动OCR。")
        self.file_info.setWordWrap(True); self.layout().insertWidget(1, self.file_info)
        row = QHBoxLayout()
        self.all_pages = QCheckBox("全部物理页 / TXT全文"); self.all_pages.setChecked(True)
        self.first = QLineEdit("1"); self.first.setMaximumWidth(75); self.first.setAccessibleName("起始物理页")
        self.last = QLineEdit("1"); self.last.setMaximumWidth(75); self.last.setAccessibleName("结束物理页")
        self.extract_button = QPushButton("提取文字层 / 继续缺失页")
        for widget in (self.all_pages, QLabel("连续范围"), self.first, QLabel("至"), self.last, self.extract_button): row.addWidget(widget)
        self.layout().insertLayout(2, row)
        reader = QHBoxLayout()
        self.pages = QComboBox(); self.pages.setAccessibleName("来源阅读物理页")
        self.source_button = QPushButton("回看封存原PDF物理页")
        for widget in (QLabel("按页阅读"), self.pages, self.source_button): reader.addWidget(widget)
        self.layout().insertLayout(3, reader)
        self.image = QLabel("尚未请求原页；回看不会提取、识别、翻译或自动保存。")
        self.image.setAlignment(Qt.AlignCenter); self.image.setAccessibleName("封存PDF原页")
        self.image_scroll = QScrollArea(); self.image_scroll.setWidget(self.image); self.image_scroll.setWidgetResizable(True)
        self.image_scroll.setMinimumHeight(160); self.image_scroll.setMaximumHeight(250)
        self.image_scroll.setVisible(False)
        self.layout().insertWidget(4, self.image_scroll)
        self.extract_button.clicked.connect(lambda: self.operate("document.extract"))
        self.pages.currentIndexChanged.connect(self.read_page)
        self.source_button.clicked.connect(self.render_page)
        self.notice.setText("所选全页清单与原文件副本独立封存；来源路径仅作记录。打开和回看不推理，已有成功段保留。")
        self.update_controls(self.client.state)

    def update_controls(self, state):
        super().update_controls(state)
        if not hasattr(self, "pages"): return
        ready = state == "ready" and not self.pending
        selected = bool(self.saved and self.saved.get("task"))
        self.input.setReadOnly(True)
        self.create.setEnabled(ready and not selected and self.selection is not None)
        for widget in (self.all_pages, self.first, self.last): widget.setEnabled(ready and not selected)
        self.pages.setEnabled(selected)
        self.extract_button.setEnabled(ready and selected and not self.saved.get("save_blocked"))
        self.source_button.setEnabled(ready and selected and self.saved["task"]["document"]["isPDF"])

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择PDF或UTF-8 TXT（原文件只读）", "", "PDF / TXT (*.pdf *.txt)")
        if not path: return
        self.saved = None; self.selection = None; self.reading_page = None; self.pages.clear()
        self.input.clear(); self.output.clear(); self.image.clear(); self.image_revision = None
        self.image_scroll.setVisible(False)
        self.operate("document.select", source_path=path)

    def create_task(self):
        if not self.selection: return
        try:
            first, last = (1, self.selection["totalPages"]) if self.all_pages.isChecked() else (int(self.first.text()), int(self.last.text()))
        except ValueError:
            self.status.setText("物理页码须为整数；反向、越界或超过200页会明确拒绝。"); return
        self.send("document.create", selection_token=self.selection["token"], first=first, last=last, direction=self.direction.currentData())

    def receive_snapshot(self, snapshot):
        if "listing" in snapshot:
            super().receive_snapshot(snapshot); return
        task = snapshot.get("task")
        if not task or task["kind"] != "文档":
            self.saved = None; self.update_controls(self.client.state); return
        previous_id = self.saved["task"]["id"] if self.saved and self.saved.get("task") else None
        self.saved = snapshot; d = task["document"]
        self.direction.setCurrentIndex(0 if d["direction"] == "en-zh" else 1)
        self.info.setText(f"{d['model']} · 有效来源与配置绑定 · {task['name']}")
        self.file_info.setText(f"{d['file']} · {d['totalPages'] if d['isPDF'] else '不适用（TXT全文）'} 物理页 · SHA-256 {d['fingerprint']}")
        ids = [p["id"] for p in d["pages"]]
        if previous_id != task["id"] or [self.pages.itemData(i) for i in range(self.pages.count())] != ids:
            self.pages.blockSignals(True); self.pages.clear()
            for p in d["pages"]:
                self.pages.addItem(f"PDF物理页 {p['id']}" if d["isPDF"] else "TXT全文", p["id"])
            self.reading_page = self.reading_page if previous_id == task["id"] and self.reading_page in ids else ids[0]
            self.pages.setCurrentIndex(ids.index(self.reading_page)); self.pages.blockSignals(False)
            self.image.clear(); self.image_revision = None
        parts = (task.get("text") or {}).get("segments", [])
        self.status.setText(coverage(d) + f"\n翻译：{sum(p['state']=='completed' for p in parts)} / {len(parts)}段 · 最近已保存序号 {snapshot['durable_revision']}" +
                            ("\n" + snapshot["warning"] if snapshot.get("warning") else "") + ("\n" + d["issue"] if d.get("issue") else ""))
        self.read_page(); self.update_controls(self.client.state)

    def read_page(self):
        if not self.saved or not self.saved.get("task") or self.pages.currentData() is None: return
        self.reading_page = self.pages.currentData(); task = self.saved["task"]
        page = next(p for p in task["document"]["pages"] if p["id"] == self.reading_page)
        self.input.setPlainText(page["text"])
        scrollbar = self.output.verticalScrollBar(); position = scrollbar.value()
        value = document_text(task, only_page=self.reading_page)
        if self.output.toPlainText() != value:
            self.output.setPlainText(value); scrollbar.setValue(position)
        if self.image_revision and self.image_revision[:2] != (task["id"], self.reading_page):
            self.image.clear(); self.image_revision = None
            self.image_scroll.setVisible(False)

    def render_page(self):
        if not self.saved: return
        if self.image_scroll.isVisible() and self.image_revision and self.image_revision[:2] == (self.saved["task"]["id"],self.reading_page):
            self.image_scroll.setVisible(False); self.image.clear(); self.image_revision=None;return
        p = next(p for p in self.saved["task"]["document"]["pages"] if p["id"] == self.reading_page)
        self.operate("document.render", page=p["id"], page_revision=p["revision"])

    def receive_event(self, event):
        if event["type"] == "operation.finished":
            if event["request_id"] != self.operation_request: return
            kind = event["result"]["kind"]; detail = event["result"].get("detail")
            if kind == "document-selection":
                self.operation_request = None
                if event["status"] == "completed" and detail:
                    self.selection = detail; self.first.setText("1"); self.last.setText(str(detail["totalPages"]))
                    self.file_info.setText(f"{detail['file']} · 真实物理页数：{detail['totalPages'] if detail['isPDF'] else '不适用（TXT全文）'} · {detail['byteCount']}字节 · 已封存暂存副本")
                    self.status.setText("请选择全部页或连续物理范围，保存任务后主动提取。最多200页，不静默裁剪。")
                else: self.status.setText("文件未选入：" + str(detail))
                self.update_controls(self.client.state); return
            if kind == "document-render":
                self.operation_request = None
                path = None
                try:
                    if event["status"] != "completed" or not detail: raise ValueError(str(detail))
                    relative = Path(detail["image_file"])
                    if len(relative.parts) != 2 or len(relative.parts[0]) != 32 or any(c not in '0123456789abcdef' for c in relative.parts[0]) or relative.parts[1] != 'page.png':
                        raise ValueError("位图路径不在受控暂存目录")
                    candidate_path = self.root / "page-handoff" / relative
                    raw = safe_file(candidate_path, MAX_IMAGE_BYTES); meta = detail["image"]
                    if (len(raw), sha(raw)) != (meta["byteCount"], meta["sha256"]): raise ValueError("位图hash/大小不匹配")
                    path = candidate_path  # Only delete a checked, owned regular file.
                    task = self.saved["task"]; p = next(p for p in task["document"]["pages"] if p["id"] == self.reading_page)
                    if (detail["task_id"], detail["revision"], detail["page"], detail["page_revision"]) != (task["id"], task["revision"], self.reading_page, p["revision"]):
                        raise ValueError("原页回看修订身份已改变")
                    image = QImage.fromData(raw)
                    if image.isNull() or (image.width(), image.height()) != (meta["pixelWidth"], meta["pixelHeight"]): raise ValueError("位图像素尺寸不匹配")
                    # A readable, scrollable single page; no whole-book image cache.
                    display = QPixmap.fromImage(image).scaledToWidth(900, Qt.SmoothTransformation)
                    self.image_scroll.setWidgetResizable(False); self.image.setPixmap(display); self.image.resize(display.size())
                    self.image_scroll.setVisible(True)
                    self.image_revision = (task["id"], self.reading_page, p["revision"])
                    self.status.setText(f"原PDF物理页 {self.reading_page} · {meta['pixelWidth']}×{meta['pixelHeight']} px · rotation {meta['rotation']}°；来源已核对，无推理/写回。")
                except (ValueError, KeyError, TypeError, OSError, StopIteration) as exc: self.status.setText("原页未能回看：" + str(exc))
                finally:
                    if path:
                        try: path.unlink(missing_ok=True); path.parent.rmdir()
                        except OSError: pass
                return
        super().receive_event(event)
