"""T04 reader over the existing DocumentPage owner and shared worker workflow.

There is exactly one document draft queue, snapshot consumer and close guard.
The inherited complete export/compatibility operations remain reachable.
"""
import json
from pathlib import Path

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QPlainTextEdit,
                               QMessageBox, QSizePolicy, QSplitter, QWidget)

from translator_core.contracts import LABELS, trim, validate_task, bilingual_text
from translator_core.documents import PAGE_LABELS, coverage, document_text, page_source_label
from .components import Button, ElidedLabel, frame, hbox, label, vbox
from .document_page import DocumentPage
from .workspace import PageLifecycle
from translator_core.images import content, units, unit_field


class DocumentWorkspace(DocumentPage, PageLifecycle):
    controls_changed = Signal()
    back_requested = Signal()

    def __init__(self, client, task_root, parent=None, auto_refresh=False):
        self.reader_ready = False
        self.composing = self.commit_guard = False
        self.ime_events = []
        self.rejected_snapshots = 0
        self.reading_owner = None
        self.reading_contexts = {}
        self.success_text = ''
        self.exported_revision = None
        self.exported_owner = None
        self.exit_approved_owner = None
        self.export_owner = None
        self.selection_backup = None
        super().__init__(client, task_root, parent, auto_refresh=auto_refresh)
        # Reuse the actual controls and connections; discard only their old layout.
        names = ('new', 'direction', 'check', 'return_draft', 'listing', 'refresh', 'open',
                 'import_button', 'info', 'input', 'create', 'save', 'start', 'cancel_button',
                 'status', 'output', 'export_button', 'notice', 'file_info', 'all_pages',
                 'first', 'last', 'extract_button', 'mode', 'pages', 'source_button',
                 'image', 'image_scroll', 'draft_notice', 'apply_button', 'discard_button',
                 'review_box', 'fork_review', 'fork_fresh')
        reused = {getattr(self, name) for name in names}
        for name in names:
            widget = getattr(self, name)
            if name != 'image': widget.setParent(self)
            widget.hide()

        def empty(layout):
            while layout.count():
                item = layout.takeAt(0)
                if item.layout(): empty(item.layout())
                elif item.widget():
                    widget = item.widget()
                    # setParent(self) does not detach controls already parented
                    # to self from their old nested layouts. They still belong
                    # to this reader and must survive DeferredDelete delivery.
                    if widget not in reused:
                        widget.hide(); widget.deleteLater()
        empty(self.layout())
        box = self.layout(); box.setContentsMargins(36, 20, 36, 16); box.setSpacing(10)
        self.setMinimumWidth(0)
        heading = hbox(spacing=8)
        self.back = Button('', 'back', 'icon', self.back_requested.emit)
        self.back.setAccessibleName('返回文档上传页'); heading.addWidget(self.back)
        self.title = ElidedLabel('文档翻译', 'section_title')
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        heading.addWidget(self.title, 1)
        self.new.setText('更换文档…'); heading.addWidget(self.new)
        self.details_button = Button('来源与完整操作', 'file', 'quiet', self.show_details)
        heading.addWidget(self.details_button); box.addLayout(heading)
        self.file_summary = label('选择文件后，保存来源、提取与翻译均需主动操作。', 'tiny', True)
        box.addWidget(self.file_summary)
        self.selection_card = frame('selection'); form = vbox(self.selection_card, (14, 10, 14, 10), 8)
        row = hbox(spacing=8)
        self.mode.setMinimumWidth(0); self.mode.setMaximumWidth(240)
        self.mode.setAccessibleName('文档处理方式，默认文字层')
        self.direction.setAccessibleName('文档翻译与识别语言方向')
        row.addWidget(self.mode, 1); row.addWidget(self.direction, 1); row.addWidget(self.create)
        form.addLayout(row)
        row = hbox(spacing=6)
        row.addWidget(self.all_pages); row.addStretch(); row.addWidget(label('物理页', 'tiny'))
        row.addWidget(self.first); row.addWidget(label('至', 'tiny')); row.addWidget(self.last)
        form.addLayout(row); box.addWidget(self.selection_card)
        tools = hbox(spacing=8)
        self.prev = Button('', 'back', 'icon', lambda: self.step_page(-1)); self.prev.setAccessibleName('上一物理页')
        self.next = Button('', 'chevron', 'icon', lambda: self.step_page(1)); self.next.setAccessibleName('下一物理页')
        self.pages.setMinimumWidth(0); self.pages.setMaximumWidth(240)
        tools.addWidget(self.prev); tools.addWidget(self.pages, 1); tools.addWidget(self.next)
        tools.addStretch(); self.source_button.setText('查看封存原页'); tools.addWidget(self.source_button)
        self.extract_button.setText('提取文字层'); tools.addWidget(self.extract_button)
        box.addLayout(tools)
        self.coverage_label = label('', 'tiny', True); box.addWidget(self.coverage_label)
        self.reader = QSplitter(Qt.Horizontal)
        source = frame('editor_card'); source_box = vbox(source, (14, 10, 14, 10), 6)
        source_box.addWidget(label('原文', 'panel_title'))
        self.page_notice = label('尚未保存文档来源', 'tiny', True); source_box.addWidget(self.page_notice)
        self.input.setMinimumHeight(80); self.input.setMaximumHeight(16777215)
        self.input.setFrameShape(QPlainTextEdit.NoFrame); self.input.setProperty('kind', 'reader_text')
        self.input.setAccessibleName('文档当前页原文或未应用校正草稿')
        self.input.setPlaceholderText('保存来源后，主动提取文字层或运行所选整页 OCR。')
        self.input.installEventFilter(self); source_box.addWidget(self.input, 1)
        self.image_scroll.setParent(source); self.image_scroll.setMaximumHeight(16777215)
        self.image_scroll.setMinimumHeight(80); source_box.addWidget(self.image_scroll, 1)
        self.image.show()
        self.image_scroll.hide()
        target = frame('editor_card'); target_box = vbox(target, (14, 10, 14, 10), 6)
        row = hbox(); row.addWidget(label('主译文', 'panel_title'), 1)
        self.copy_button = Button('复制本页', 'copy', 'quiet', self.copy_translation); row.addWidget(self.copy_button)
        target_box.addLayout(row)
        self.translation_scope = label('尚未建立翻译计划', 'tiny', True); target_box.addWidget(self.translation_scope)
        self.output.setMinimumHeight(80); self.output.setFrameShape(QPlainTextEdit.NoFrame)
        self.output.setProperty('kind', 'reader_text'); self.output.setAccessibleName('当前页已完整确认的主译文')
        self.output.setPlaceholderText('这里只显示当前有效原文的成功译文；完整状态可导出。')
        target_box.addWidget(self.output, 1)
        self.reader.addWidget(source); self.reader.addWidget(target); self.reader.setSizes([420, 420])
        self.reader.setChildrenCollapsible(False); box.addWidget(self.reader, 1)
        self.draft_notice.setObjectName(''); self.draft_notice.setProperty('kind', 'tiny')
        box.addWidget(self.draft_notice)
        self.edit_row = QWidget(); edits = hbox(self.edit_row, spacing=8)
        self.apply_button.setText('应用校正'); self.discard_button.setText('放弃本页草稿')
        edits.addWidget(self.apply_button); edits.addWidget(self.discard_button)
        edits.addStretch(); self.review_box.setText('已核对当前有效原文'); edits.addWidget(self.review_box)
        box.addWidget(self.edit_row)
        footer = hbox(spacing=8)
        self.save.setText('保存'); footer.addWidget(self.save)
        self.export_button.setText('完整双语 TXT…'); footer.addWidget(self.export_button)
        footer.addStretch(); self.cancel_button.setText('停止'); footer.addWidget(self.cancel_button)
        self.start.setText('开始翻译'); footer.addWidget(self.start); box.addLayout(footer)
        self.status.setProperty('kind', 'tiny'); self.status.setMaximumHeight(62)
        self.status.setToolTip('状态包含保存确认与当前操作结果；详情可在来源与完整操作中查看。')
        box.addWidget(self.status)
        for name in ('new', 'direction', 'input', 'create', 'save', 'start', 'cancel_button',
                     'output', 'export_button', 'all_pages', 'first', 'last', 'extract_button',
                     'mode', 'pages', 'source_button', 'draft_notice', 'apply_button', 'discard_button', 'review_box', 'status'):
            getattr(self, name).show()
        self.start.clicked.disconnect(); self.start.clicked.connect(self.start_translation)
        self.apply_button.clicked.disconnect(); self.apply_button.clicked.connect(lambda: self.edit_action(self.action('apply')))
        self.all_pages.toggled.connect(lambda _: self.update_controls(self.client.state))
        self.reader_ready = True
        self.focus_order = [self.back, self.new, self.details_button, self.mode, self.direction,
                            self.all_pages, self.first, self.last, self.create, self.prev, self.pages,
                            self.next, self.source_button, self.extract_button, self.input, self.output,
                            self.copy_button, self.apply_button, self.discard_button, self.review_box,
                            self.save, self.export_button, self.cancel_button, self.start]
        for a, b in zip(self.focus_order, self.focus_order[1:]): QWidget.setTabOrder(a, b)
        self.read_page(); self.update_controls(self.client.state)

    def focusNextPrevChild(self, forward):
        if not self.reader_ready: return super().focusNextPrevChild(forward)
        visible = [w for w in self.focus_order if w.isVisible() and w.isEnabled()]
        current = QApplication.focusWidget()
        if current in visible and visible:
            visible[(visible.index(current) + (1 if forward else -1)) % len(visible)].setFocus(Qt.TabFocusReason)
            return True
        return super().focusNextPrevChild(forward)

    def eventFilter(self, watched, event):
        if watched is self.input:
            if event.type() == QEvent.InputMethod:
                self.ime_events.append(dict(preedit=event.preeditString(), commit=event.commitString()))
                self.ime_events[:] = self.ime_events[-256:]
                self.composing = bool(event.preeditString()); self.commit_guard = True
                QTimer.singleShot(0, self.clear_commit_guard)
            if event.type() in (QEvent.ShortcutOverride, QEvent.KeyPress):
                if self.ime_blocked() and event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() & Qt.ControlModifier:
                    event.accept(); return True
        return super().eventFilter(watched, event)

    def clear_commit_guard(self):
        self.commit_guard = False
        if self.reader_ready:
            self.update_controls(self.client.state)
            if not self.ime_blocked() and self.local_drafts and not self.draft_timer.isActive():
                self.draft_timer.start()

    def ime_blocked(self):
        if self.commit_guard: return True
        if self.composing:
            # Native input contexts can clear Qt's marked area on focus/editor
            # reset without delivering a final InputMethod event to this filter.
            # Keep blocking actual marked text; never let a stale flag lock the
            # reader after QTextControl has already cleared the composition.
            layout = self.input.textCursor().block().layout()
            self.composing = bool(layout and layout.preeditAreaText())
        return self.composing

    def source_read_only(self, state):
        task = self.saved.get('task') if self.saved else None
        bound = bool(task and self.client.current_task and task['id'] == self.client.current_task['id'])
        own_draft_write = bool(self.pending_kind == self.action('draft') and self.draft_inflight and
            task and self.draft_inflight[0][0] == task['id'] and not self.after_draft and not self.close_callback)
        ready = state == 'ready' and (own_draft_write or not self.pending and not self.client.task_request_pending())
        return not (ready and bound and task.get('formatVersion') == self.format_version and
                    not self.saved.get('save_blocked') and not content(task)['translationStarted'])

    def edited(self):
        if self.displaying: return
        page = self.current_page()
        if not page: return
        key = self.draft_key(page)
        draft = self.saved['task']['drafts'].get(str(page['id']))
        previous = self.local_drafts.get(key, draft['text'] if isinstance(draft, dict) else
            draft if isinstance(draft, str) else page['text'])
        # QTextControl can emit textChanged for a marked-area update without
        # changing the committed plain text. That is not a new saved draft.
        if self.input.toPlainText() == previous: return
        return super().edited()

    def flush_drafts(self):
        if self.ime_blocked(): return
        return super().flush_drafts()

    def step_page(self, delta):
        if self.ime_blocked(): return
        index = self.pages.currentIndex() + delta
        if 0 <= index < self.pages.count(): self.pages.setCurrentIndex(index)

    def select_path(self, path):
        if self.ime_blocked() or self.client.state != 'ready' or self.client.task_request_pending() or not self.client.can_change_task(): return False
        # A cancelled/failed acquisition keeps the current task, local view and draft.
        self.selection_backup = self.selection
        return self.operate(self.action('select'), source_path=str(path))

    def receive_snapshot(self, snapshot):
        if 'listing' in snapshot:
            return super().receive_snapshot(snapshot)
        context = self.client.snapshot_context or {}
        if context.get('session') != getattr(self.client, 'session_id', None):
            self.rejected_snapshots += 1; return
        task = snapshot.get('task')
        if task is None: return
        if task.get('kind') != self.task_kind:
            # Keep the inactive document view read-only, never bind it to a text task.
            self.update_controls(self.client.state); return
        explicit = context.get('kind') in ('task.open', 'task.import', self.action('create'), self.action('fork'))
        if not explicit and (not self.saved or task['id'] != self.saved['task']['id']):
            self.rejected_snapshots += 1; return
        if context.get('kind') in ('task.start', self.action('extract'), self.action('ocr')):
            if (context.get('request') != self.operation_request or
                    context.get('generation') != (self.client.operation or (None,))[0]):
                self.rejected_snapshots += 1; return
        if self.saved and task['id'] == self.saved['task']['id'] and task['revision'] < self.saved['task']['revision']:
            self.rejected_snapshots += 1; return
        if self.task_kind != '图片':
            try: validate_task(task)
            except ValueError:
                self.rejected_snapshots += 1; return
        super().receive_snapshot(snapshot)

    def remember_reading(self):
        if not self.reader_ready or self.reading_owner is None: return
        c = self.input.textCursor(); out = self.output.textCursor()
        self.reading_contexts[self.reading_owner] = (c.position(), c.anchor(), self.input.verticalScrollBar().value(),
            out.position(), out.anchor(), self.output.verticalScrollBar().value())
        while len(self.reading_contexts) > 200: self.reading_contexts.pop(next(iter(self.reading_contexts)))

    def read_page(self):
        if not self.reader_ready: return
        page = self.current_page()
        if not page: return
        self.remember_reading()
        task = self.saved['task']; d = content(task); key = self.draft_key(page)
        number = self.pages.currentData()
        page = next((p for p in units(d) if p['id'] == number), None)
        if not page: return
        self.reading_page = number; key = self.draft_key(page)
        owner_changed = self.reading_owner != key
        draft = task['drafts'].get(str(number))
        body = self.local_drafts.get(key, draft['text'] if isinstance(draft, dict) else draft if isinstance(draft, str) else page['text'])
        self.displaying = True
        source_changed = self.input.toPlainText() != body
        if source_changed: self.input.setPlainText(body)
        self.displaying = False
        self.review_box.blockSignals(True); self.review_box.setChecked(page['reviewed']); self.review_box.blockSignals(False)
        self.reading_owner = key
        local = key in self.local_drafts or self.draft_inflight is not None
        self.draft_notice.setText('草稿尚未保存，等待实际保存确认；翻译已禁用。' if local else
            '草稿已保存，尚未应用；译文和导出仍对应有效原文。' if draft is not None else
            '有效原文已冻结；更改文字、方向或重新识别，请在完整操作中明确另建副本。' if d['translationStarted'] else
            '编辑先保存为独立草稿；点击应用校正后才改变有效原文，并重置人工核对。' if task.get('formatVersion') == self.format_version else
            '文字层与旧格式只读；需要校正时，在完整操作中另建核对副本。')
        title = f"PDF 物理页 {number}" if d.get('isPDF', False) else 'TXT 全文'
        if page.get('label') is not None: title += f" · 印刷标签 {page['label']}"
        self.page_notice.setText(title + ' · ' + PAGE_LABELS[page['state']] + ' · ' + page_source_label(page))
        self.page_notice.setToolTip(page.get('issue', '') + '\n有效修订：' + page['revision'])
        parts = (task.get('text') or {}).get('segments', [])
        mapping = [m for m in d['sources'] if m[unit_field(d)] == number and m['revision'] == page['revision']]
        current = [(m, parts[m['segmentID']]) for m in mapping if m['segmentID'] < len(parts)]
        successful = [(m, p) for m, p in current if p['state'] == 'completed']
        self.success_text = '\n\n'.join(p['result']['translation'] for _, p in successful)
        target_changed = self.output.toPlainText() != self.success_text
        if target_changed: self.output.setPlainText(self.success_text)
        statuses = ' · '.join(f"第{m['paragraph']}段：{LABELS[p['state']]}" for m, p in current if p['state'] != 'completed')
        self.translation_scope.setText(f'本页成功 {len(successful)} / {len(mapping)} 段' + (' · ' + statuses if statuses else '') if mapping else
            '本页尚未建立翻译计划' if trim(page['text']) else '本页没有可翻译文字；完整导出保留此页与状态')
        self.translation_scope.setToolTip('\n'.join(f"第{m['paragraph']}段：{p.get('error', '')}" for m, p in current))
        if self.image_revision and self.image_revision != (task['id'], number, page['revision']):
            self.image.clear(); self.image_revision = None; self.image_scroll.hide()
        saved = self.reading_contexts.get(key)
        if saved:
            for editor, changed, pos, anchor, scroll in ((self.input, source_changed, *saved[:3]), (self.output, target_changed, *saved[3:])):
                # Reassigning the same cursor can terminate native marked text.
                # An acknowledgement for this unchanged page must leave it alone.
                if not owner_changed and not changed: continue
                c = editor.textCursor(); limit = editor.document().characterCount() - 1
                c.setPosition(min(anchor, limit)); c.setPosition(min(pos, limit), QTextCursor.KeepAnchor)
                editor.setTextCursor(c); editor.verticalScrollBar().setValue(scroll)
        self.update_controls(self.client.state)

    def update_controls(self, state):
        super().update_controls(state)
        if not self.reader_ready: return
        task = self.saved['task'] if self.saved else None
        selected = bool(task)
        bound = bool(task and self.client.current_task and task['id'] == self.client.current_task['id'] and self.client.ready_confirmed)
        ready = state == 'ready' and not self.pending and not self.client.task_request_pending()
        self.selection_card.setVisible(not selected)
        self.edit_row.setVisible(bool(task and task.get('formatVersion') == self.format_version))
        self.title.full_text = content(task)['file'] if task else self.selection.get('file', self.selection.get('source',{}).get('file','图片')) if self.selection else '文档翻译'
        self.title.setText(self.title.full_text)
        self.title.setToolTip(self.title.text())
        self.file_summary.setText((f"{content(task)['totalPages']} 个真实物理页 · 范围 {content(task)['range']['first']}–{content(task)['range']['last']}" if content(task).get('isPDF', False) else 'TXT 全文 · 严格 UTF-8，原始换行与 Unicode 保留') + ' · 来源已封存' if task else
            (f"{self.selection['totalPages']} 个真实物理页" if self.selection.get('isPDF', False) else 'TXT 全文') + ' · 来源暂存已核对，请保存任务' if self.selection else '选择文件后，保存来源、提取与翻译均需主动操作。')
        if self.selection and not task:
            pdf = self.selection.get('isPDF', False)
            self.mode.model().item(1).setEnabled(pdf)
            if not pdf: self.mode.setCurrentIndex(0)
            self.all_pages.setEnabled(ready and pdf)
            for w in (self.first, self.last): w.setEnabled(ready and pdf and not self.all_pages.isChecked())
        self.pages.setEnabled(selected and not self.ime_blocked())
        self.prev.setEnabled(selected and self.pages.currentIndex() > 0 and not self.ime_blocked())
        self.next.setEnabled(selected and self.pages.currentIndex() + 1 < self.pages.count() and not self.ime_blocked())
        self.copy_button.setEnabled(bool(self.success_text and selected))
        if task:
            d = content(task); job = task.get('text') or {}; parts = job.get('segments', [])
            missing = [p for p in units(d) if p['state'] in ('pending', 'recognizing', 'stopped')]
            gaps = [str(p['id']) if d.get('isPDF', False) else '全文' for p in units(d) if p['state'] in ('noText', 'failed', 'pending', 'recognizing', 'stopped')]
            self.coverage_label.setText(coverage(d) + (' · 未覆盖：' + '、'.join(gaps) if gaps else ''))
            self.coverage_label.setToolTip('\n'.join(f"{p['id']}：{p.get('issue', PAGE_LABELS[p['state']])}" for p in units(d) if p.get('issue')))
            can_start = ready and bound and not self.saved.get('save_blocked') and not self.local_drafts and not self.draft_inflight and not task['drafts'] and not missing and any(trim(p['text']) for p in units(d))
            self.start.setEnabled(bool(can_start and not (parts and all(p['state'] == 'completed' for p in parts))))
            self.start.setText('继续 / 重试' if parts and not all(p['state'] == 'completed' for p in parts) else '已完成所获文字' if parts else '开始翻译')
            self.start.setToolTip('仅翻译已有合法文字；未覆盖页仍在清单和完整导出中。' if gaps else '主动开始；打开记录、翻页与换主题均不生成。')
            self.extract_button.setEnabled(ready and bound and not self.saved.get('save_blocked') and not d['translationStarted'] and not task['drafts'] and not self.local_drafts)
            self.extract_button.setText('整页 OCR' if d['mode'] == 'ocr' else '提取文字层')
        else:
            self.page_notice.setText(('PDF 来源' if self.selection.get('isPDF', False) else 'TXT 全文') +
                ' · 尚未保存来源任务' if self.selection else '尚未保存文档来源')
            self.page_notice.setToolTip('')
            self.draft_notice.setText('当前来源尚未保存为任务；保存来源并主动提取后可阅读原文。')
            self.translation_scope.setText('尚未建立翻译计划')
            self.translation_scope.setToolTip('')
            self.coverage_label.setText('默认只提取文字层；扫描内容须显式选择整页 OCR，单个连续范围最多 200 页。' if not self.selection or self.selection.get('isPDF', False) else
                'TXT 仅处理全文，严格保留 UTF-8 原文；不使用 OCR。')
            self.coverage_label.setToolTip('')
        self.source_button.setToolTip('当前操作结束后可读取封存原页；切页与已有文字阅读不受影响。' if state in ('running', 'cancelling') else '核验封存副本后有界渲染；不会重新提取或识别。')
        self.details_button.setEnabled(bool(task or self.selection))
        if self.ime_blocked():
            for control in (self.new, self.extract_button, self.source_button, self.save,
                            self.apply_button, self.discard_button, self.review_box, self.start):
                control.setEnabled(False)
        if task and task.get('formatVersion') != self.format_version:
            self.fork_review.setEnabled(ready and bound and content(task).get('isPDF', False) and not self.local_drafts and not self.saved.get('save_blocked'))
            self.fork_fresh.setEnabled(self.fork_review.isEnabled() and content(task).get('isPDF', False) and not task['drafts'])
        self.controls_changed.emit()

    def start_translation(self):
        if self.ime_blocked() or not self.start.isEnabled(): return False
        return self.operate('task.start')

    def edit_action(self, kind):
        if self.ime_blocked(): return
        return super().edit_action(kind)

    def copy_translation(self):
        if self.success_text: QApplication.clipboard().setText(self.success_text)

    def guard_switch(self):
        if self.ime_blocked():
            self.status.setText('请先完成输入法上屏或取消组合，再切换任务。'); return False
        if not super().guard_switch(): return False
        if self.saved and (self.saved.get('save_blocked') or self.saved.get('durable_revision') != self.saved['task']['revision']):
            self.status.setText('当前文档保存受阻；保留窗口与任务，请先完整导出。')
            return self.exported_owner == (self.saved['task']['id'], self.saved['task']['revision'])
        return True

    def prepare_close(self, callback):
        if self.ime_blocked():
            self.status.setText('请先完成输入法上屏或取消组合，再关闭窗口。'); return False
        if not super().prepare_close(callback): return False
        if self.saved and self.saved.get('save_blocked'):
            owner = (self.saved['task']['id'], self.saved['task']['revision'])
            if self.exit_approved_owner == owner: return True
            if self.exported_owner == owner:
                answer = QMessageBox.question(self, '已导出当前受阻文档，仍要退出？',
                    '当前有效内容与完整状态已经导出。任务保存仍受阻；确认退出后，可从导出保留内容或重新打开原任务。',
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if answer == QMessageBox.Yes:
                    self.exit_approved_owner = owner
                    return True
                return False
            self.status.setText('文档保存受阻，窗口与有效内容保留；请先完整导出当前修订。'); return False
        return True

    def export(self):
        if self.saved: self.export_owner = (self.saved['task']['id'], self.saved['task']['revision'])
        return super().export()

    def receive_event(self, event):
        # Never treat a terminal alone as durable draft confirmation.
        if event['type'] == 'task.done' and event['request_id'] == self.pending_request and self.pending_kind == self.action('draft'):
            key, body = self.draft_inflight if self.draft_inflight else (None, None)
            task = self.saved['task'] if self.saved else None
            draft = task['drafts'].get(str(key[1])) if task and key else None
            if not (task and task['id'] == key[0] and isinstance(draft, dict) and
                    draft.get('baseRevision') == key[2] and draft.get('text') == body and
                    not self.saved.get('save_blocked') and self.saved.get('durable_revision') == task['revision']):
                self.draft_inflight = None; self.pending = False; self.pending_request = self.pending_kind = None
                self.close_callback = self.after_draft = None
                self.status.setText('草稿保存未获匹配的持久确认，当前编辑保留。'); self.update_controls(self.client.state); return
        if event['type'] == 'operation.finished' and event['request_id'] == self.operation_request and event['result']['kind'] == self.operation_prefix+'-selection':
            if event['status'] == 'completed' and event['result'].get('detail'):
                self.remember_reading(); self.saved = None; self.reading_page = self.reading_owner = None
                self.pages.blockSignals(True); self.pages.clear(); self.pages.blockSignals(False)
                self.displaying = True; self.input.clear(); self.displaying = False
                self.output.clear(); self.success_text = ''; self.image.clear(); self.image_revision = None; self.image_scroll.hide()
            else: self.selection = self.selection_backup
        export_event = event['type'] == 'task.exported' and event['request_id'] == self.export_request
        target = self.export_target if export_event else None
        super().receive_event(event)
        if event['type'] == 'operation.finished' and event['result']['kind'] == self.operation_prefix+'-selection' and event['status'] != 'completed':
            detail = event['result'].get('detail') or {}
            self.status.setText('文件未选入：' + (detail.get('error', '选择已取消，当前文档保留。') if isinstance(detail, dict) else str(detail)))
        if export_event and target and self.status.text().startswith('完整双语TXT已导出：'):
            self.exported_revision = self.export_owner[1] if self.export_owner else None
            self.exported_owner = self.export_owner
        if event['type'] in ('task.done', 'operation.finished', 'error'): self.update_controls(self.client.state)

    def show_details(self):
        dialog = QDialog(self); dialog.setWindowTitle('来源、原始识别与完整文档操作'); dialog.resize(760, 600)
        box = vbox(dialog, (20, 20, 20, 20), 10)
        box.addWidget(label('原始识别、有效原文与未应用草稿分别保留。可在“最近记录”或“我的翻译”中搜索、收藏、修改显示标题及回收恢复。', 'tiny', True))
        output = QPlainTextEdit(); output.setReadOnly(True); output.setAccessibleName('完整文档来源与原始 OCR 证据')
        task = self.saved['task'] if self.saved else None
        output.setPlainText(bilingual_text(task) if task else json.dumps(self.selection, ensure_ascii=False, indent=2))
        box.addWidget(output, 1)
        row = hbox(spacing=8)
        for text, action, enabled in (('另建核对副本…', lambda: self.fork(False), self.fork_review.isEnabled()),
                ('另建全新 OCR…', lambda: self.fork(True), self.fork_fresh.isEnabled()),
                ('导入旧任务副本…', self.import_task, self.import_button.isEnabled())):
            button = Button(text); button.setEnabled(enabled)
            button.clicked.connect(lambda checked=False, fn=action: (dialog.accept(), fn()))
            row.addWidget(button)
        box.addLayout(row)
        close = Button('关闭', callback=dialog.accept); box.addWidget(close)
        dialog.exec(); self.details_button.setFocus()
