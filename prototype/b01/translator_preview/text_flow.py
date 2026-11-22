"""R6 text UI orchestration over the shared task/operation/snapshot contracts.

No splitting, generation, retry policy or task serialization lives in this layer.
"""
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QFileDialog, QMessageBox
from translator_core.contracts import MAX_INPUT_BYTES, default_recipe, sha, validate_task
from translator_core.store import atomic_write, safe_file
from translator_core.text_policy import analyze_concise_text
from .text_draft import TextDraftStore
from .workspace import PHASES, TranslationResult


class TextFlow(QObject):
    controls_changed = Signal()

    def __init__(self, page, client, bridge, root, parent=None):
        super().__init__(parent)
        self.page, self.client, self.bridge, self.root = page, client, bridge, Path(root)
        self.saved = None
        self.view_source = self.view_revision = None
        self.pending = self.pending_kind = self.operation_request = None
        self.auto_start = False
        self.export_request = self.export_target = self.export_owner = None
        self.exported_revision = None
        self.suppressed = False
        self.dirty = False
        self.message = ''
        self.closing = False
        self.close_callback = None
        self.context_restore = None
        self.submission = None
        self.store = TextDraftStore(root)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(350)
        self.timer.timeout.connect(self.flush_draft)
        self.draft = self.empty_draft()
        data = self.store.load()
        if data:
            self.draft = data['draft']; self.context_restore = data.get('context')
            # Retain the schema for older readers; the removed selector must not
            # restore a hidden detailed state that prevents translation.
            self.draft['mode'] = 0
        self.restore_editor(self.draft)
        page.flow = self
        page.draft_changed.connect(self.edited)
        page.translate_requested.connect(self.translate)
        page.export_requested.connect(self.export)
        page.stop_button.clicked.connect(self.cancel)
        page.new_button.clicked.connect(self.new_draft)
        page.edit_button.clicked.connect(self.edit_copy)
        page.return_button.clicked.connect(self.return_draft)
        page.tools.currentIndexChanged.connect(self.tool_action)
        client.snapshot.connect(self.receive_snapshot)
        client.event.connect(self.receive_event)
        client.changed.connect(self.connection_changed)
        bridge.state_changed.connect(self.update_controls)
        self.update_controls()

    @staticmethod
    def empty_draft():
        return dict(text='', direction='en-zh', mode=0, position=0, anchor=0)

    @property
    def busy(self):
        return bool(self.auto_start or self.pending or self.operation_request or self.client.state in ('running','cancelling','closing') or
                    self.client.task_request_pending() or self.closing)

    @property
    def bound(self):
        current = self.client.current_task
        task = self.saved['task'] if self.saved else None
        return bool(task and current and current['id'] == task['id'] and
                    self.page.source.toPlainText()==self.view_source and
                    self.page.effective_revision==self.view_revision and
                    self.page.direction.currentData()==task['configuration']['direction'] and
                    self.page.configuration==task['configuration'] and
                    self.client.ready_confirmed and self.client.state in ('ready','running','cancelling'))

    def capture_editor(self):
        p = self.page; c = p.source.textCursor()
        return dict(text=p.source.toPlainText(), direction=p.direction.currentData(), mode=0,
                    position=c.position(), anchor=c.anchor())

    def restore_editor(self, draft):
        p = self.page; p.restoring = True
        p.direction.setCurrentIndex(0 if draft['direction']=='en-zh' else 1)
        p.configuration = default_recipe(draft['direction']); p.effective_revision = 'draft'
        p.source.setPlainText(draft['text'])
        self.restore_cursor(draft)
        p.presentation.bind(p.owner()); p.presentation.clear()
        p.restoring = False; p.render()

    def restore_cursor(self, context):
        p = self.page; cursor = p.source.textCursor(); limit = p.source.document().characterCount()-1
        cursor.setPosition(min(context['anchor'],limit)); cursor.setPosition(min(context['position'],limit),QTextCursor.KeepAnchor)
        p.source.setTextCursor(cursor)

    def reading_context(self):
        if not self.saved: return None
        cursor = self.page.source.textCursor()
        return dict(task_id=self.saved['task']['id'], position=cursor.position(), anchor=cursor.anchor(),
                    scroll=self.page.output.verticalScrollBar().value(), mode=0,
                    suppressed=self.suppressed)

    def edited(self):
        if self.page.restoring: return
        if not self.saved:
            self.draft = self.capture_editor()
        self.dirty = True; self.timer.start()
        self.update_controls()

    def flush_draft(self):
        self.timer.stop()
        if not self.dirty: return True
        try:
            self.store.save(self.draft, self.reading_context())
            self.dirty = False
            if self.message.startswith('草稿尚未保存：'): self.message = ''
            self.update_controls()
            return True
        except (OSError, ValueError) as exc:
            self.message = '草稿尚未保存：'+str(exc)
            self.update_controls(); return False

    def protect_draft(self):
        if self.busy or not self.client.can_change_task(): return False
        if not self.saved: self.draft = self.capture_editor()
        self.dirty = True
        return self.flush_draft()

    def guard_switch(self):
        if self.client.current_task is None: return True
        if self.saved and (self.saved.get('save_blocked') or self.saved.get('durable_revision')!=self.saved['task']['revision']):
            if self.exported_revision!=self.saved['task']['revision']:
                self.message = '当前确认内容尚未保存，请先导出完整原文与状态，再切换任务。'
                self.update_controls(); return False
        return True

    def new_draft(self):
        if self.busy or not self.client.can_change_task(): return False
        # Keep the only unsubmitted input reachable until the user clears it explicitly.
        return self.return_draft() if not self.saved else self.show_draft(self.draft)

    def show_draft(self, draft):
        if self.busy or not self.client.can_change_task(): return False
        if self.saved and self.saved.get('save_blocked') and self.exported_revision != self.saved['task']['revision']:
            self.message = '当前结果保存受阻，请先导出完整原文与状态，再返回草稿。'; self.update_controls(); return False
        self.saved = None; self.view_source = self.view_revision = None; self.suppressed = False; self.message = ''
        self.draft = dict(draft); self.restore_editor(self.draft)
        self.dirty = True; self.flush_draft(); self.update_controls(); return True

    def return_draft(self):
        return self.show_draft(self.draft)

    def dictionary_transfer_block(self):
        if self.busy: return '当前翻译操作运行或正在停止保存，请等待安全结束后带入。'
        snapshot = self.bridge.snapshot or {}
        task = snapshot.get('task')
        if task and (snapshot.get('save_blocked') or snapshot.get('durable_revision')!=task['revision']):
            return '当前任务保存受阻，请先恢复保存或完整导出并处理当前任务。'
        if self.store.conflicted or self.message.startswith('草稿尚未保存：'):
            return '翻译草稿尚未安全保存，请先恢复保存。'
        if not self.client.can_change_task(): return '请先完成文档或文字草稿的保存保护。'
        return ''

    def accept_dictionary(self, raw, direction):
        """Save the replacement before changing the view; never create/start a task."""
        blocked = self.dictionary_transfer_block()
        if blocked:
            self.message = blocked; self.update_controls(); return False
        analysis = analyze_concise_text(raw, direction)
        if analysis.is_empty or len(raw.encode('utf-8'))>MAX_INPUT_BYTES:
            self.message = '带入需要非空原文，翻译容量最多1MiB UTF-8。'
            self.update_controls(); return False
        current = self.draft if self.saved else self.capture_editor()
        if current['text'] and (current['text']!=raw or current['direction']!=direction):
            names={'zh-en':'中→英','en-zh':'英→中'}
            summary = lambda text:text[:160]+('…' if len(text)>160 else '')
            answer = QMessageBox.question(self.page, '替换当前翻译草稿？',
                '当前（'+names[current['direction']]+'）：'+summary(current['text'])+
                '\n\n新草稿（'+names[direction]+'）：'+summary(raw)+
                '\n\n替换前将保护当前内容；带入后仍需主动翻译。',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes: return False
        if not self.protect_draft(): return False
        draft = dict(text=raw,direction=direction,mode=0,position=0,anchor=0)
        try: self.store.save(draft,None)
        except (OSError,ValueError) as exc:
            self.message='草稿尚未保存：'+str(exc); self.update_controls(); return False
        self.saved=None; self.view_source=self.view_revision=None; self.suppressed=False
        self.draft=draft; self.dirty=False; self.restore_editor(draft)
        self.message='已带入完整原文与方向，请主动点击翻译。'; self.update_controls(); return True

    def edit_copy(self):
        if not self.saved or self.busy: return False
        # Never replace an unrelated unsubmitted draft without an explicit decision.
        if self.draft['text'] and self.draft['text'] != self.page.source.toPlainText():
            answer = QMessageBox.question(self.page, '保留未提交草稿', '当前还有未提交草稿。用这条记录的原文替换该草稿？原历史任务会保留。')
            if answer != QMessageBox.Yes: return False
        return self.show_draft(self.capture_editor())

    def tool_action(self, index):
        if not index: return
        self.page.tools.blockSignals(True); self.page.tools.setCurrentIndex(0); self.page.tools.blockSignals(False)
        actions = {1:lambda:self.create(False), 2:self.save, 3:self.check_service, 4:self.import_copy,
                   5:self.reopen, 6:lambda:self.export(full=True)}
        actions[index]()

    def send(self, kind, **fields):
        if self.busy or self.client.state != 'ready': return False
        req = self.client.send(kind, **fields)
        if not req:
            self.message = '操作未发送，请等待连接就绪及文档草稿保存确认。'; self.update_controls(); return False
        self.pending, self.pending_kind = req, kind
        self.message = ''; self.update_controls(); return True

    def create(self, start=False):
        if self.saved or self.busy or self.client.state != 'ready': return False
        raw = self.page.source.toPlainText().encode()
        if self.page.analysis.is_empty or len(raw)>MAX_INPUT_BYTES:
            self.message = '请输入非空原文，最多1MiB UTF-8。'; self.update_controls(); return False
        if not self.protect_draft(): return False
        self.submission = (sha(raw),self.page.direction.currentData())
        name = uuid.uuid4().hex+'.txt'
        try: atomic_write(raw, self.root/'incoming'/name)
        except (OSError,ValueError) as exc:
            self.message = '输入尚未提交：'+str(exc); self.update_controls(); return False
        if not self.send('task.create',input_file=name,byte_count=len(raw),sha256=sha(raw),direction=self.page.direction.currentData()):
            self.auto_start = False; (self.root/'incoming'/name).unlink(missing_ok=True); return False
        self.auto_start = start
        return True

    def translate(self):
        if self.busy: return False
        if not self.saved: return self.create(True)
        record=self.client.history.records.get(self.saved['task']['id'])
        if record and record['trashed_at'] is not None:
            self.message='此记录在回收站，请先恢复后继续。';self.update_controls();return False
        if not self.bound:
            self.message = '请先从任务操作中重新打开当前任务，确认连接后的任务身份。'; self.update_controls(); return False
        if self.suppressed: return self.reopen()
        if not self.bridge.state.can_start: return False
        parts = (self.saved['task'].get('text') or {}).get('segments',[])
        if parts and all(p['state']=='completed' for p in parts): return False
        self.message = ''; self.suppressed = False
        self.operation_request = self.client.start_operation('task.start') or None
        self.update_controls(); return bool(self.operation_request)

    def save(self):
        return self.send('task.save') if self.bound and not self.saved.get('save_blocked') else False

    def check_service(self):
        if self.busy: return False
        self.operation_request = self.client.start_operation('service.check', direction=self.page.direction.currentData()) or None
        self.update_controls(); return bool(self.operation_request)

    def import_copy(self, path=None):
        if not self.protect_draft(): return False
        if path is None: path = QFileDialog.getExistingDirectory(self.page,'选择旧任务UUID目录（只读导入副本）')
        return self.send('task.import',source_directory=str(path)) if path else False

    def reopen(self):
        if not self.saved or not self.protect_draft(): return False
        return self.bridge.open_task(self.saved['task']['id'])

    def cancel(self):
        return self.client.cancel() if self.operation_request else False

    def receive_snapshot(self, snapshot):
        if 'task' not in snapshot: return
        context = self.client.snapshot_context or {}
        if context.get('session') != getattr(self.client,'session_id',None): return
        task = snapshot.get('task')
        kind = context.get('kind')
        explicit = (kind=='task.open' and context.get('request')==self.bridge.open_request) or (
            kind in ('task.create','task.import') and context.get('request')==self.pending)
        if not task or task.get('kind')!='文字':
            # The text draft remains separate while a document owns the worker.
            self.update_controls(); return
        if not explicit and (not self.saved or task['id']!=self.saved['task']['id']): return
        if not explicit and not self.bound: return
        if kind=='task.create' and (sha(task['input'].encode()),task['configuration']['direction'])!=self.submission:
            self.message = '保存响应不属于本次原文与方向，未接收，也未开始翻译。'; self.update_controls(); return
        if kind=='task.start' and (context.get('request')!=self.operation_request or
                                  context.get('generation')!=(self.client.operation or (None,))[0]): return
        if self.saved and task['id']==self.saved['task']['id'] and task['revision']<self.saved['task']['revision']: return
        try: validate_task(task)
        except ValueError as exc:
            self.message = '未接收不完整任务：'+str(exc); self.update_controls(); return
        p = self.page
        changed_task = not self.saved or self.saved['task']['id']!=task['id']
        if explicit:
            if not self.saved: self.draft = self.capture_editor()
            self.suppressed = False; self.message = ''
        previous_input = self.saved['task']['input'] if self.saved else None
        self.saved = snapshot
        p.restoring = True
        if changed_task or previous_input!=task['input']:
            p.source.setPlainText(task['input'])
            # QTextDocument normalizes CRLF/paragraph separators for display. Keep
            # that view identity separate from the original sealed task bytes.
            self.view_source = p.source.toPlainText()
        p.direction.setCurrentIndex(0 if task['configuration']['direction']=='en-zh' else 1)
        p.configuration = task['configuration']; p.effective_revision = task['asset']['fingerprint'] if task.get('asset') else sha(task['input'].encode())
        self.view_revision = p.effective_revision
        owner = p.owner(task_id=task['id'], task_revision=task['revision'], request_id=context.get('request'),
                        generation=context.get('generation') or context['session'])
        p.expect_result(owner)
        parts = (task.get('text') or {}).get('segments',[])
        translations = [part['result']['translation'] for part in parts if part['state']=='completed']
        if translations and not self.suppressed:
            p.present_result(TranslationResult(owner,'\n\n'.join(translations)))
        if explicit and self.context_restore and self.context_restore['task_id']==task['id']:
            self.restore_cursor(self.context_restore)
            p.output.verticalScrollBar().setValue(self.context_restore['scroll']); self.context_restore = None
        elif changed_task:
            p.output.verticalScrollBar().setValue(0)
        p.restoring = False; p.render()
        # Opening a record may update UI context, never the draft body or task payload.
        if explicit:
            self.dirty = True; self.timer.start()

    def receive_event(self, event):
        kind, req = event['type'], event['request_id']
        if kind=='ready' and self.context_restore:
            QTimer.singleShot(0, lambda:self.bridge.open_task(self.context_restore['task_id']) if self.context_restore else None)
        if kind in ('task.done','error') and req==self.pending:
            pending_kind = self.pending_kind
            self.pending = self.pending_kind = None
            start = self.auto_start; self.auto_start = False
            if kind=='error':
                self.message = event['error'].get('message',event['error']['code'])
            elif pending_kind=='task.create':
                if (self.saved and self.bound and not self.saved.get('save_blocked') and
                        self.saved.get('durable_revision')==self.saved['task']['revision']):
                    self.draft = self.empty_draft(); self.dirty = True
                    # The confirmed saved task owns the input now. A failed UI context save must stop the chained start.
                    if self.flush_draft() and start and not self.closing:
                        self.auto_start = True
                        identity = (self.saved['task']['id'],self.saved['task']['revision'])
                        QTimer.singleShot(0,lambda:self.start_after_save(identity))
                else: self.message = '原文保存尚未确认，未开始翻译。'
        if kind in ('operation.finished','error') and req==self.operation_request:
            self.operation_request = None
            if kind=='error': self.message = event['error'].get('message',event['error']['code'])
            else:
                detail = event.get('result',{}).get('detail')
                if event['status']=='lease_expired': self.message = '连接租约到期，确认结果已保留，请重新打开任务后主动继续。'
                elif detail and detail.get('error'): self.message = detail['error']
                elif event.get('result',{}).get('kind')=='service-diagnostic':
                    self.message = '本机服务与固定模型检查通过。检查没有生成译文。'
        if kind=='task.exported' and req==self.export_request:
            self.finish_export(event)
        elif kind=='error' and req==self.export_request:
            self.message = '导出未完成：'+event['error'].get('message',event['error']['code'])
            self.export_request = self.export_target = self.export_owner = None
        self.update_controls()
        if self.close_callback and not self.pending and not self.operation_request:
            callback = self.close_callback; self.close_callback = None; QTimer.singleShot(0,callback)

    def start_after_save(self, identity):
        requested = self.auto_start
        self.auto_start = False
        if (requested and not self.closing and self.bound and
                identity==(self.saved['task']['id'],self.saved['task']['revision'])):
            self.translate()
        self.update_controls()

    def connection_changed(self, state):
        if state in ('failed','disconnected','connecting'):
            self.pending = self.pending_kind = self.operation_request = None; self.auto_start = False
            self.export_request = self.export_target = self.export_owner = None
            if self.saved: self.message = '连接已变化；当前记录可阅读、复制，请重新打开确认任务身份后继续或导出。'
        self.update_controls()

    def export(self, full=False, target=None):
        if not self.bound or self.export_request or (not full and not self.page.presentation.result): return False
        if self.suppressed: return False
        if target is None:
            target,_ = QFileDialog.getSaveFileName(self.page,'导出完整双语TXT','文字翻译-双语.txt','UTF-8文字 (*.txt)')
        if not target: return False
        self.export_target = Path(target)
        self.export_owner = (self.saved['task']['id'],self.saved['task']['revision'])
        self.export_request = self.client.send('task.export')
        if not self.export_request: self.export_target = self.export_owner = None
        self.update_controls(); return bool(self.export_request)

    def finish_export(self, event):
        try:
            name = event['filename']
            if len(name)!=36 or not name.endswith('.txt') or any(c not in '0123456789abcdef' for c in name[:-4]):
                raise ValueError('导出文件身份无效')
            source = self.root/'exports'/name; raw = safe_file(source,32*1024*1024)
            if len(raw)!=event['byte_count'] or sha(raw)!=event['sha256'] or self.export_target is None:
                raise ValueError('导出快照hash或大小不匹配')
            atomic_write(raw,self.export_target); source.unlink()
            if self.saved and self.export_owner==(self.saved['task']['id'],self.saved['task']['revision']):
                self.exported_revision = self.export_owner[1]
            self.message = '完整双语TXT已导出：'+str(self.export_target)
        except (OSError,ValueError) as exc: self.message = '导出未完成：'+str(exc)
        finally: self.export_request = self.export_target = self.export_owner = None

    def update_controls(self, *_):
        p = self.page; busy = self.busy; ready = self.client.state=='ready' and not busy
        editable = not self.saved and not busy
        p.source.setReadOnly(not editable)
        for widget in (p.direction,p.target_language,p.swap,p.paste_button): widget.setEnabled(editable)
        p.clear_button.setEnabled(editable and bool(p.source.toPlainText()))
        p.new_button.setEnabled(not busy); p.new_button.setVisible(bool(self.saved))
        p.edit_button.setVisible(bool(self.saved)); p.edit_button.setEnabled(not busy)
        p.return_button.setVisible(bool(self.saved)); p.return_button.setEnabled(not busy)
        p.tools.setEnabled(ready and not self.export_request)
        for index, enabled in {1:ready and not self.saved and not p.analysis.is_empty,
                2:ready and self.bound and self.bridge.state.can_save, 3:ready, 4:ready,
                5:ready and bool(self.saved),
                6:self.bound and not self.export_request and not self.suppressed}.items():
            p.tools.model().item(index).setEnabled(bool(enabled))
        p.stop_button.setVisible(bool(self.operation_request)); p.stop_button.setEnabled(self.client.state=='running')
        p.copy_button.setEnabled(p.presentation.result is not None)
        p.export_button.setEnabled(bool(p.presentation.result and self.bound and not self.export_request))
        text = '翻译'; can_translate = ready and not p.analysis.is_empty
        status = '输入原文后点击翻译 · ⌘ / Ctrl + Enter'
        p.result_scope.hide()
        if self.saved:
            task = self.saved['task']; parts = (task.get('text') or {}).get('segments',[])
            completed = [s for s in parts if s['state']=='completed']
            phase = (task.get('text') or {}).get('phase','draft')
            save_status = '保存受阻' if self.saved.get('save_blocked') else '已保存' if self.saved.get('durable_revision')==task['revision'] else '尚未落盘'
            status = f"{PHASES.get(phase,phase)} · 已确认 {len(completed)}/{len(parts)} 段 · {save_status}"
            if completed and len(completed)!=len(parts): status += ' · 仅显示/复制成功段，导出含全部原文与状态'
            if completed and p.presentation.result:
                ranges = []
                for part in completed:
                    number = part['id']+1
                    if ranges and ranges[-1][1]+1==number: ranges[-1][1]=number
                    else: ranges.append([number,number])
                scope = '、'.join(str(a) if a==b else f'{a}–{b}' for a,b in ranges)
                p.result_scope.setText('已确认：第'+(scope if len(scope)<100 else scope[:100]+'…')+f'段 / 共{len(parts)}段')
                p.result_scope.setToolTip('已确认译文：第'+scope+'段；复制范围与当前主译文一致，完整段状态见双语TXT。')
                p.result_scope.show()
            text = '继续未完成 / 重试' if parts else '开始翻译'
            eligible = any(s['state']!='completed' and (task.get('text') or {}).get('attempts',{}).get(str(s['id']),0)<3 for s in parts)
            if parts and not eligible:
                text = '已完成' if len(completed)==len(parts) else '尝试次数已达上限'
                can_translate = False
            if self.suppressed: text = '读取已保存译文'; can_translate = ready
            can_translate = can_translate and self.bound and self.bridge.state.can_start
            record=self.client.history.records.get(task['id'])
            if record and record['trashed_at'] is not None:
                can_translate=False;status+=' · 已移到回收站，请先恢复'
            if not self.bound: status += ' · 请重新打开任务确认身份'
            error = self.saved.get('warning') or (task.get('text') or {}).get('error')
            if error: status += '\n'+error
        if self.pending: status = '正在保存原文，确认后才会开始翻译…' if self.pending_kind=='task.create' else '正在等待任务保存或读取确认…'
        if self.client.state=='cancelling': status += '\n正在停止并保存确认结果，请等待尾部确认。'
        if self.dirty: status += ' · 草稿待保存'
        elif not self.saved and self.store.revision and p.source.toPlainText(): status += ' · 草稿已保存'
        if self.store.warning: status += '\n'+self.store.warning
        if self.message: status += '\n'+self.message
        if self.client.failure:
            status += '\n' + self.client.connection_error_text()
        p.notice.setText(status); p.translate_button.setText(text); p.translate_button.setEnabled(can_translate)
        self.controls_changed.emit()

    def prepare_close(self, callback):
        if not self.flush_draft(): return False
        self.closing = True; self.auto_start = False
        if self.pending or self.operation_request or self.export_request:
            self.close_callback = callback
            if self.operation_request: self.cancel()
            self.update_controls(); return False
        if self.saved and (self.saved.get('save_blocked') or self.saved.get('durable_revision')!=self.saved['task']['revision']):
            self.closing = False
            if self.exported_revision!=self.saved['task']['revision']:
                self.message = '退出已暂停：任务尚未保存，请先导出完整原文与状态，保留当前窗口供恢复。'
                self.update_controls(); return False
            if QMessageBox.question(self.page,'任务尚未保存','已导出当前确认内容，但任务保存仍受阻。确认退出？')!=QMessageBox.Yes:
                return False
        return True
