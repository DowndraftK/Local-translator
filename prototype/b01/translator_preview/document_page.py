"""Shared document reader. Native engines never own task workflow or UI rules."""
from pathlib import Path
import uuid
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QComboBox, QCheckBox, QPushButton, QFileDialog, QScrollArea, QInputDialog
from translator_core.contracts import sha, MAX_INPUT_BYTES
from translator_core.documents import document_text, coverage
from translator_core.pdf_adapter import MAX_IMAGE_BYTES
from translator_core.store import safe_file, atomic_write
from .text_page import TextPage
from .components import Combo
from translator_core.images import content, units, unit_field


class DocumentPage(TextPage):
    task_kind = "文档"
    format_version = 2
    operation_prefix = "document"

    def action(self, name):
        return self.operation_prefix+"."+name

    def __init__(self, client, task_root, parent=None, auto_refresh=True):
        self.selection = None; self.reading_page = None; self.image_revision = None
        self.local_drafts = {}; self.draft_inflight = None; self.after_draft = None
        self.close_callback = None; self.displaying = False
        super().__init__(client, task_root, parent, auto_refresh=auto_refresh)
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
        self.mode = Combo(); self.mode.addItem("文字层（默认）", "text"); self.mode.addItem("显式整页 OCR（另建任务）", "ocr")
        row.insertWidget(0, self.mode)
        self.layout().insertLayout(2, row)
        reader = QHBoxLayout()
        self.pages = Combo(); self.pages.setAccessibleName("来源阅读物理页")
        self.source_button = QPushButton("回看封存原PDF物理页")
        for widget in (QLabel("按页阅读"), self.pages, self.source_button): reader.addWidget(widget)
        self.layout().insertLayout(3, reader)
        self.image = QLabel("尚未请求原页；回看不会提取、识别、翻译或自动保存。")
        self.image.setAlignment(Qt.AlignCenter); self.image.setAccessibleName("封存PDF原页")
        self.image_scroll = QScrollArea(); self.image_scroll.setWidget(self.image); self.image_scroll.setWidgetResizable(True)
        self.image_scroll.setMinimumHeight(160); self.image_scroll.setMaximumHeight(250)
        self.image_scroll.setVisible(False)
        self.layout().insertWidget(4, self.image_scroll)
        self.extract_button.clicked.connect(lambda: self.operate(self.action('ocr') if self.saved and content(self.saved['task'])['mode']=='ocr' else self.action('extract')))
        self.draft_notice = QLabel("原文只读；显式OCR任务可单独保存编辑草稿，再应用校正。")
        self.draft_notice.setWordWrap(True); self.layout().insertWidget(5,self.draft_notice)
        editing = QHBoxLayout()
        self.apply_button = QPushButton("保存校正为有效原文")
        self.discard_button = QPushButton("明确放弃本页草稿")
        self.review_box = QCheckBox("已人工核对当前有效修订")
        self.fork_review = QPushButton("另建核对副本…")
        self.fork_fresh = QPushButton("另建全新OCR任务…")
        for w in (self.apply_button,self.discard_button,self.review_box,self.fork_review,self.fork_fresh):editing.addWidget(w)
        self.layout().insertLayout(6,editing)
        self.apply_button.clicked.connect(lambda:self.edit_action(self.action('apply')))
        self.discard_button.clicked.connect(lambda:self.edit_action(self.action('discard')))
        self.review_box.toggled.connect(self.set_review)
        self.fork_review.clicked.connect(lambda:self.fork(False))
        self.fork_fresh.clicked.connect(lambda:self.fork(True))
        self.draft_timer = QTimer(self);self.draft_timer.setSingleShot(True);self.draft_timer.setInterval(600)
        self.draft_timer.timeout.connect(self.flush_drafts)
        self.input.textChanged.connect(self.edited)
        self.save.clicked.disconnect();self.save.clicked.connect(self.save_document)
        self.client.changed.connect(lambda state:self.flush_drafts() if state=='ready' else None)
        self.client.can_change_task = self.guard_switch
        self.pages.currentIndexChanged.connect(self.read_page)
        self.source_button.clicked.connect(self.render_page)
        self.notice.setText("所选全页清单与原文件副本独立封存；来源路径仅作记录。打开和回看不推理，已有成功段保留。")
        self.update_controls(self.client.state)

    def source_read_only(self, state):
        task = self.saved.get('task') if self.saved else None
        ready = state == 'ready' and not self.pending and not self.client.task_request_pending()
        bound = bool(task and self.client.current_task and task['id'] == self.client.current_task['id'])
        return not (ready and bound and task.get('formatVersion') == self.format_version and
                    not self.saved.get('save_blocked') and not content(task)['translationStarted'])

    def update_controls(self, state):
        super().update_controls(state)
        if not hasattr(self, "pages"): return
        ready = state == "ready" and not self.pending and not self.client.task_request_pending()
        selected = bool(self.saved and self.saved.get("task"))
        bound = selected and self.client.current_task is not None and self.saved["task"]["id"] == self.client.current_task["id"]
        d = content(self.saved['task']) if selected else {}
        schema2 = selected and self.saved['task'].get('formatVersion')==self.format_version
        editable = ready and bound and schema2 and not self.saved.get('save_blocked') and not d['translationStarted']
        self.mode.setEnabled(ready and not selected)
        self.extract_button.setText("主动整页OCR / 继续缺失页" if d.get('mode')=='ocr' else "提取文字层 / 继续缺失页")
        self.start.setEnabled(self.start.isEnabled() and not self.local_drafts and not self.saved['task']['drafts'] if selected else False)
        if hasattr(self,'apply_button'):
            page=self.current_page();has_draft=bool(page and (self.draft_key(page) in self.local_drafts or str(page['id']) in self.saved['task']['drafts']))
            self.apply_button.setEnabled(editable and has_draft);self.discard_button.setEnabled(editable and has_draft)
            self.review_box.setEnabled(ready and bound and schema2 and not has_draft and not self.saved.get('save_blocked'))
            self.fork_review.setEnabled(ready and bound and not self.local_drafts and not self.saved['task']['drafts'] and not self.saved.get('save_blocked'))
            self.fork_fresh.setEnabled(self.fork_review.isEnabled() and d.get('isPDF', False))
        self.create.setEnabled(ready and not selected and self.selection is not None)
        for widget in (self.all_pages, self.first, self.last): widget.setEnabled(ready and not selected)
        self.pages.setEnabled(selected)
        self.extract_button.setEnabled(ready and bound and not self.saved.get("save_blocked"))
        self.source_button.setEnabled(ready and bound and content(self.saved['task']).get('isPDF', self.task_kind=='图片'))

    def choose_file(self):
        if not self.guard_switch():return
        path, _ = QFileDialog.getOpenFileName(self, "选择PDF或UTF-8 TXT（原文件只读）", "", "PDF / TXT (*.pdf *.txt)")
        if not path: return
        self.select_path(path)

    def select_path(self, path):
        if self.client.state != 'ready' or self.client.task_request_pending() or not self.guard_switch(): return False
        self.saved = None; self.selection = None; self.reading_page = None; self.pages.clear()
        self.input.clear(); self.output.clear(); self.image.clear(); self.image_revision = None
        self.image_scroll.setVisible(False)
        return self.operate(self.action('select'), source_path=str(path))

    def create_task(self):
        if not self.selection: return
        try:
            first, last = (1, self.selection["totalPages"]) if self.all_pages.isChecked() else (int(self.first.text()), int(self.last.text()))
        except ValueError:
            self.status.setText("物理页码须为整数；反向、越界或超过200页会明确拒绝。"); return
        self.send(self.action('create'), selection_token=self.selection["token"], first=first, last=last, direction=self.direction.currentData(),mode=self.mode.currentData())

    def receive_snapshot(self, snapshot):
        if "listing" in snapshot:
            super().receive_snapshot(snapshot); return
        task = snapshot.get("task")
        if not task or task["kind"] != self.task_kind:
            self.saved = None; self.update_controls(self.client.state); return
        previous_id = self.saved["task"]["id"] if self.saved and self.saved.get("task") else None
        self.saved = snapshot; d = content(task)
        self.direction.setCurrentIndex(0 if d["direction"] == "en-zh" else 1)
        self.info.setText(f"{d['model']} · 有效来源与配置绑定 · {task['name']}")
        self.file_info.setText(f"{d['file']} · {d['totalPages'] if d.get('isPDF', False) else '不适用（TXT全文）'} 物理页 · SHA-256 {d['fingerprint']}")
        ids = [p["id"] for p in units(d)]
        if previous_id != task["id"] or [self.pages.itemData(i) for i in range(self.pages.count())] != ids:
            self.pages.blockSignals(True); self.pages.clear()
            for p in units(d):
                self.pages.addItem(f"PDF物理页 {p['id']}" if d.get('isPDF', False) else "TXT全文", p["id"])
            self.reading_page = self.reading_page if previous_id == task["id"] and self.reading_page in ids else ids[0]
            self.pages.setCurrentIndex(ids.index(self.reading_page)); self.pages.blockSignals(False)
            self.image.clear(); self.image_revision = None
        parts = (task.get("text") or {}).get("segments", [])
        self.status.setText(coverage(d) + f"\n翻译：{sum(p['state']=='completed' for p in parts)} / {len(parts)}段 · 最近已保存序号 {snapshot['durable_revision']}" +
                            ("\n" + snapshot["warning"] if snapshot.get("warning") else "") + ("\n" + d["issue"] if d.get("issue") else ""))
        self.mode.setCurrentIndex(1 if d['mode']=='ocr' else 0)
        self.read_page(); self.update_controls(self.client.state)

    def read_page(self):
        if not self.saved or not self.saved.get("task") or self.pages.currentData() is None: return
        self.reading_page = self.pages.currentData(); task = self.saved["task"]
        page = next(p for p in units(content(task)) if p["id"] == self.reading_page)
        key=self.draft_key(page);draft=task['drafts'].get(str(page['id']))
        body=self.local_drafts.get(key,draft['text'] if isinstance(draft,dict) else page['text'])
        self.displaying=True
        if self.input.toPlainText()!=body:self.input.setPlainText(body)
        self.displaying=False
        self.review_box.blockSignals(True);self.review_box.setChecked(page['reviewed']);self.review_box.blockSignals(False)
        self.draft_notice.setText("当前显示未应用草稿；正式原文、翻译和导出仍使用有效修订。" if key in self.local_drafts or draft is not None else
            ("有效修订："+page['revision']+" · 开始翻译后冻结；编辑须另建核对副本。" if content(task)['translationStarted'] else "有效原文已保存；编辑先保存为独立草稿，应用后生成新修订。"))
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
        p = next(p for p in units(content(self.saved["task"])) if p["id"] == self.reading_page)
        self.operate(self.action('render'), page=p["id"], page_revision=p["revision"])

    def current_page(self):
        if not self.saved or not self.saved.get('task') or self.reading_page is None:return None
        return next((p for p in units(content(self.saved['task'])) if p['id']==self.reading_page),None)

    def draft_key(self,page):return (self.saved['task']['id'],page['id'],page['revision'])

    def edited(self):
        if self.displaying:return
        page=self.current_page()
        if not page or self.saved['task'].get('formatVersion')!=self.format_version:return
        key=self.draft_key(page);body=self.input.toPlainText()
        count=sum(len(v.encode()) for k,v in self.local_drafts.items() if k!=key)+len(body.encode())
        if count>MAX_INPUT_BYTES:
            self.status.setText('编辑草稿累计超过1MiB保护，当前文本尚未保存；请减少内容。')
            self.local_drafts[key]=body;self.update_controls(self.client.state);return
        self.local_drafts[key]=body;self.draft_notice.setText('编辑草稿尚未保存，翻译已禁用；正在等待独立草稿保存。')
        self.draft_timer.start();self.update_controls(self.client.state)

    def flush_drafts(self):
        if not self.local_drafts or self.pending or self.client.state!='ready' or self.draft_inflight or not self.saved or self.saved.get('save_blocked'):return
        key,body=next(iter(self.local_drafts.items()))
        if key[0]!=self.saved['task']['id']:
            self.status.setText('存在未保存的其他任务草稿，保持窗口供恢复。');return
        raw=body.encode()
        if len(raw)>MAX_INPUT_BYTES:return
        name=uuid.uuid4().hex+'.txt'
        try:atomic_write(raw,self.root/'incoming'/name)
        except (OSError,ValueError) as exc:self.status.setText('草稿尚未保存：'+str(exc));return
        self.draft_inflight=(key,body)
        self.send(self.action('draft'),page=key[1],page_revision=key[2],input_file=name,byte_count=len(raw),sha256=sha(raw))
        if self.pending_request is None:self.draft_inflight=None

    def guard_switch(self):
        if self.local_drafts or self.draft_inflight:
            self.flush_drafts();self.status.setText('请先等待编辑草稿保存确认，再切换任务。');return False
        return True

    def open_task(self):
        if self.guard_switch():super().open_task()

    def import_task(self):
        if self.guard_switch():super().import_task()

    def operate(self,kind,**fields):
        if self.local_drafts or self.draft_inflight:
            self.flush_drafts();self.status.setText('尚有未保存草稿，请等待保存并应用校正。');return False
        return super().operate(kind,**fields)

    def edit_action(self,kind):
        page=self.current_page()
        if not page:return
        self.after_draft=(kind,page['id'],page['revision'])
        if self.local_drafts or self.draft_inflight:self.flush_drafts();return
        self.after_draft=None;self.send(kind,page=page['id'],page_revision=page['revision'])

    def set_review(self,value):
        page=self.current_page()
        if page:self.send(self.action('review'),page=page['id'],page_revision=page['revision'],reviewed=value)

    def fork(self,fresh):
        if not self.guard_switch():return
        choices=['英文 → 简体中文','简体中文 → 英文']
        selected,ok=QInputDialog.getItem(self,'另建任务，保留原任务和译文','新任务的翻译与OCR语言方向',choices,0,False)
        if ok:self.send(self.action('fork'),fresh=fresh,direction='en-zh' if selected==choices[0] else 'zh-en')

    def save_document(self):
        if self.local_drafts or self.draft_inflight:self.flush_drafts()
        else:self.send('task.save')

    def prepare_close(self,callback):
        if not self.local_drafts and not self.draft_inflight and not self.pending:return True
        self.close_callback=callback;self.flush_drafts()
        self.status.setText('退出前正在等待草稿保存确认；保存失败时保留窗口。')
        return False

    def receive_event(self, event):
        if event['type'] in ('task.done','error') and event['request_id']==self.pending_request:
            completed_kind=self.pending_kind
            inflight=self.draft_inflight if completed_kind==self.action('draft') else None
            super().receive_event(event)
            if completed_kind==self.action('draft'):
                self.draft_inflight=None
                if event['type']=='task.done' and inflight and self.local_drafts.get(inflight[0])==inflight[1]:self.local_drafts.pop(inflight[0],None)
                if event['type']=='error':self.after_draft=None;self.close_callback=None
            if event['type']=='task.done' and not self.local_drafts and self.after_draft:
                kind,page,revision=self.after_draft;self.after_draft=None;self.send(kind,page=page,page_revision=revision)
            elif event['type']=='task.done':self.flush_drafts()
            self.read_page();self.update_controls(self.client.state)
            if self.close_callback and not self.local_drafts and not self.draft_inflight and not self.pending:
                callback=self.close_callback;self.close_callback=None;QTimer.singleShot(0,callback)
            return
        if event["type"] == "operation.finished":
            if event["request_id"] != self.operation_request: return
            kind = event["result"]["kind"]; detail = event["result"].get("detail")
            if kind == self.operation_prefix+'-selection':
                self.operation_request = None
                if event["status"] == "completed" and detail:
                    self.selection = detail; self.first.setText("1"); self.last.setText(str(detail["totalPages"]))
                    self.file_info.setText(f"{detail['file']} · 真实物理页数：{detail['totalPages'] if detail['isPDF'] else '不适用（TXT全文）'} · {detail['byteCount']}字节 · 已封存暂存副本")
                    self.status.setText("请选择全部页或连续物理范围，保存任务后主动提取。最多200页，不静默裁剪。")
                else: self.status.setText("文件未选入：" + str(detail))
                self.update_controls(self.client.state); return
            if kind == self.operation_prefix+'-render':
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
                    task = self.saved["task"]; p = next(p for p in units(content(task)) if p["id"] == self.reading_page)
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
