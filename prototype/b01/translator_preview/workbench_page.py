"""Native Mac editors and read-only history for submission-owned worker tasks."""
import json
import uuid
from functools import partial
from pathlib import Path

from PySide6.QtCore import QEvent, QSignalBlocker, QTimer, Qt, Signal
from PySide6.QtGui import QIntValidator, QPixmap, QTextCursor
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QLayout, QLineEdit, QScrollArea,
                               QSizePolicy, QTextEdit, QWidget)

from translator_core import CoreError
from translator_core.contracts import MAX_INPUT_BYTES, sha
from translator_core.languages import direction_label
from translator_core.store import atomic_write

from .components import Button, ElidedLabel, ImagePreview, ReadingEdit, frame, hbox, label, vbox
from .model_controls import LanguagePair, TranslationModel, captioned
from .workspace import PageLifecycle


class PageText(QTextEdit):
    """Fit a single page to its contents so its outer reader owns scrolling."""
    def __init__(self, name, read_only=False):
        super().__init__()
        self.setAccessibleName(name)
        self.setAcceptRichText(False)
        self.setReadOnly(read_only)
        self.setFixedHeight(60)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.document().documentLayout().documentSizeChanged.connect(self.fit_height)

    def fit_height(self, size):
        self.setFixedHeight(max(60, int(size.height())+4))

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            super().wheelEvent(event)
        else:
            event.ignore()


class PageReader(QWidget):
    changed = Signal(int, str)

    def __init__(self, source=False):
        super().__init__()
        self.source = source
        self.pages = {}
        self.comparison = False
        self.card = frame('editor_card')
        vbox(self).addWidget(self.card)
        layout = vbox(self.card, (12, 10, 12, 10), 6)
        self.header = hbox(spacing=6)
        self.title = label('原文' if source else '译文', 'panel_title')
        self.header.addWidget(self.title)
        self.header.addStretch()
        self.jump = QLineEdit()
        self.jump.setAccessibleName('原文跳转页码' if source else '译文跳转页码')
        self.jump.setFixedWidth(66)
        self.jump.setAlignment(Qt.AlignCenter)
        self.validator = QIntValidator(1, 1, self.jump)
        self.jump.setValidator(self.validator)
        self.jump.returnPressed.connect(self.jump_to_page)
        self.total = label('', 'tiny')
        self.header.addWidget(self.jump)
        self.header.addWidget(self.total)
        layout.addLayout(self.header)
        self.scroll = QScrollArea()
        self.scroll.setAccessibleName('连续原文阅读区' if source else '连续译文阅读区')
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.verticalScrollBar().valueChanged.connect(self.scrolled)
        self.scroll.viewport().installEventFilter(self)
        layout.addWidget(self.scroll, 1)
        self.content = None
        self.tail = None
        self.last_section = None

    def set_pages(self, first, last, total, originals):
        old = self.scroll.takeWidget()
        if old:
            old.deleteLater()
        self.pages.clear()
        self.content = QWidget()
        content = vbox(self.content, (0, 0, 3, 0), 0)
        content.setSizeConstraint(QLayout.SetMinimumSize)
        for number in range(first, last+1):
            section = frame('reader_page' if number != last else '')
            section.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
            layout = vbox(section, (7, 14, 5, 14), 7)
            layout.addWidget(label(f'第 {number} 页', 'tiny'))
            name = f'第 {number} 页原文' if self.source else f'第 {number} 页译文'
            main = PageText(name, not self.source)
            main.setPlaceholderText('尚未提取，可直接输入' if self.source else '译文将在这里显示')
            if self.source:
                main.setPlainText(originals.get(number, ''))
                main.textChanged.connect(partial(self.edited, number, main))
            layout.addWidget(main)
            if self.source:
                paired = PageText(f'第 {number} 页对照译文', True)
                paired.setPlaceholderText('译文将在这里显示')
                layout.addWidget(paired)
                paired.setVisible(self.comparison)
            else:
                paired = None
            content.addWidget(section)
            self.pages[number] = (section, main, paired)
        self.tail = QWidget()
        self.tail.setFixedHeight(0)
        content.addWidget(self.tail)
        self.last_section = self.pages[last][0]
        self.last_section.installEventFilter(self)
        self.scroll.setWidget(self.content)
        self.update_tail()
        self.validator.setRange(first, last)
        self.jump.setText(str(first))
        self.total.setText(f'/ {total} 页')

    def eventFilter(self, watched, event):
        if event.type()==QEvent.Resize and getattr(self,'tail',None):
            QTimer.singleShot(0,self.update_tail)
        return super().eventFilter(watched,event)

    def update_tail(self):
        if self.tail and self.last_section:
            remaining = max(0, self.scroll.viewport().height()-self.last_section.minimumSizeHint().height())
            if self.tail.height()!=remaining:
                self.tail.setFixedHeight(remaining)

    def edited(self, number, editor):
        if not self.source or number not in self.pages or editor is not self.pages[number][1]:
            return
        self.changed.emit(number, editor.toPlainText())

    def set_source(self, number, body):
        if number in self.pages:
            editor = self.pages[number][1]
            with QSignalBlocker(editor):
                editor.setPlainText(body)

    def set_target(self, number, body):
        if number in self.pages:
            _, main, paired = self.pages[number]
            editor = paired if self.source else main
            if editor.toPlainText() != body:
                editor.setPlainText(body)

    def compare(self, on):
        current = self.visible_page(self.scroll.verticalScrollBar().value())
        self.comparison = on
        self.title.setText('原文与译文' if on else '原文')
        for _, _, paired in self.pages.values():
            paired.setVisible(on)
        if current is not None:
            QTimer.singleShot(0, partial(self.scroll_to_page, current))

    def jump_to_page(self):
        number = int(self.jump.text())
        QTimer.singleShot(0, partial(self.scroll_to_page, number, True))

    def scroll_to_page(self, number, select=False):
        if number in self.pages:
            self.update_tail()
            self.content.layout().activate()
            self.scroll.ensureWidgetVisible(self.pages[number][0], 0, 0)
            self.scroll.verticalScrollBar().setValue(self.pages[number][0].y())
            self.jump.setText(str(number))
            if select:
                self.jump.selectAll()

    def scrolled(self, offset):
        if self.jump.hasFocus():
            return
        number = self.visible_page(offset)
        if number is not None:
            self.jump.setText(str(number))

    def visible_page(self, offset):
        if not self.pages:
            return None
        current = next(iter(self.pages))
        for number, (section, _, _) in self.pages.items():
            if section.y() > offset + 12:
                break
            current = number
        return current


class WorkbenchPage(QWidget, PageLifecycle):
    selection_ready = Signal()
    notice = Signal(str)
    lookup_requested = Signal(str)
    controls_changed = Signal()

    def __init__(self, mode, client, task_root):
        super().__init__()
        self.mode, self.client, self.task_root = mode, client, task_root
        self.selection = None
        self.pending = None
        self.originals, self.edits, self.results = {}, {}, {}
        self.preview_results = {}
        self.chosen = False
        self.last_record = None
        self.title = {'text':'文字翻译', 'document':'文档翻译', 'image':'图片翻译'}[mode]
        self.direction = LanguagePair()
        self.model = TranslationModel(client)
        self.status = label('', 'secondary', True)
        self.status.setAccessibleName(self.title+'状态')
        self.translate = Button('翻译' if mode=='text' else '开始翻译', None, 'primary', self.start_translation)
        self.cancel = Button('取消', None, 'secondary', self.request_cancel)
        self.extract = Button('提取原文', 'scan', 'secondary', self.start_extraction) if mode != 'text' else None
        self.stop = Button('停止', None, 'secondary', self.request_stop) if mode == 'document' else None

        box = vbox(self, (26, 20, 26, 16), 12)
        heading = hbox(spacing=8)
        heading.addWidget(label(self.title, 'heading'))
        if mode == 'text':
            heading.addStretch()
        else:
            self.filename = ElidedLabel('', 'secondary')
            heading.addWidget(self.filename, 1)
            self.replace = Button('更换文档' if mode=='document' else '更换图片', 'folder', 'quiet', self.choose)
            heading.addWidget(self.replace)
        box.addLayout(heading)
        fields = hbox(spacing=12)
        fields.addWidget(captioned('翻译方向', self.direction))
        fields.addWidget(captioned('翻译模型', self.model))
        fields.addStretch(1)
        if mode == 'document':
            self.first = self.make_range_field('起始页')
            self.last = self.make_range_field('结束页')
            span = QWidget()
            sizes = hbox(span, spacing=6)
            sizes.addWidget(self.first)
            sizes.addWidget(label('–', 'tiny'))
            sizes.addWidget(self.last)
            sizes.addWidget(Button('全文', None, 'quiet', self.full_range))
            self.span = captioned('页面范围', span)
            fields.addWidget(self.span)
        box.addLayout(fields)

        row = hbox(spacing=8)
        row.addWidget(self.status, 1)
        for button in (self.extract, self.translate, self.cancel, self.stop):
            if button:
                row.addWidget(button)
        box.addLayout(row)
        if mode == 'document':
            self.source_reader, self.target_reader = PageReader(True), PageReader()
            self.source_reader.changed.connect(self.edit_page)
            self.compare = Button('翻译对照', None, 'quiet')
            self.compare.setCheckable(True)
            self.compare.toggled.connect(self.switch_comparison)
            self.target_reader.header.insertWidget(0,self.compare)
            panes = hbox(spacing=10)
            panes.addWidget(self.source_reader, 1)
            panes.addWidget(self.target_reader, 1)
            box.addLayout(panes, 1)
            self.direction.changed.connect(self.render_results)
            self.model.currentIndexChanged.connect(self.render_results)
        else:
            panes = hbox(spacing=10)
            original = frame('editor_card'); left = vbox(original, (16, 12, 16, 12), 8)
            translation = frame('editor_card'); right = vbox(translation, (16, 12, 16, 12), 8)
            self.source = ReadingEdit()
            self.source.setObjectName('sourceEditor')
            self.source.setAccessibleName('原文输入框')
            self.source.setPlaceholderText('输入或粘贴需要翻译的内容…' if mode=='text' else '点「提取原文」或直接输入…')
            self.output = ReadingEdit()
            self.output.setReadOnly(True)
            self.output.setObjectName('targetEditor')
            self.output.setAccessibleName('译文')
            self.output.setPlaceholderText('译文将在这里显示')
            if mode == 'image':
                left.addWidget(label('原图', 'panel_title'))
                self.preview = ImagePreview()
                self.preview.setAccessibleName('所选图片预览')
                self.preview.setMinimumHeight(110)
                self.preview.setMaximumHeight(260)
                left.addWidget(self.preview, 2)
            source_header = hbox()
            source_header.addWidget(label('识别文字' if mode=='image' else '原文', 'panel_title'), 1)
            if mode=='text':
                self.clear = Button('', 'close', 'icon', self.source.clear)
                self.clear.setAccessibleName('清空原文')
                self.clear.setFixedWidth(34)
                source_header.addWidget(self.clear)
            left.addLayout(source_header)
            left.addWidget(self.source, 2)
            target_header = label('译文', 'panel_title')
            if mode=='text':
                target_header.setMinimumHeight(self.clear.minimumHeight())
            right.addWidget(target_header)
            self.result_label = label('', 'tiny', True)
            self.result_label.hide()
            right.addWidget(self.result_label)
            right.addWidget(self.output, 1)
            if mode == 'text':
                source_actions = hbox(spacing=6)
                source_actions.addWidget(Button('粘贴', 'paste', 'quiet', self.source.paste))
                lookup = Button('查词', 'search', 'quiet', lambda: self.lookup(self.source))
                lookup.setToolTip('查询选中的原文')
                source_actions.addWidget(lookup)
                source_actions.addStretch()
                left.addLayout(source_actions)
                actions = hbox(spacing=6)
                lookup_target = Button('查词', 'search', 'quiet', lambda: self.lookup(self.output))
                lookup_target.setToolTip('查询选中的译文')
                actions.addWidget(lookup_target)
                actions.addStretch()
                self.copy_result = Button('复制译文', 'copy', 'quiet', self.output_copy)
                actions.addWidget(self.copy_result)
                self.export_result = Button('导出', 'download', 'quiet', self.output_export)
                actions.addWidget(self.export_result)
                right.addLayout(actions)
            panes.addWidget(original, 1)
            panes.addWidget(translation, 1)
            box.addLayout(panes, 1)
            self.source.textChanged.connect(self.edit_source)
            self.direction.changed.connect(self.render_result_label)
            self.model.currentIndexChanged.connect(self.render_result_label)

        self.direction.changed.connect(self.choose_preferences)
        self.model.activated.connect(self.choose_preferences)
        client.snapshot.connect(self.receive_snapshot)
        client.event.connect(self.receive_event)
        client.changed.connect(self.connection_changed)
        self.update_controls()

    def make_range_field(self, name):
        edit = QLineEdit()
        edit.setFixedWidth(64)
        edit.setAlignment(Qt.AlignCenter)
        edit.setAccessibleName(name)
        edit.setValidator(QIntValidator(1, 1, edit))
        edit.editingFinished.connect(self.change_range)
        return edit

    def page_range(self):
        return int(self.first.text() or '0'), int(self.last.text() or '0')

    def full_range(self):
        if self.selection:
            self.first.setText('1')
            self.last.setText(str(self.selection['total']))
            self.change_range()

    def change_range(self):
        if not self.selection:
            return False
        first, last = self.page_range()
        total = self.selection['total']
        if not 1 <= first <= last <= total or last-first >= 200:
            self.status.setText('请输入文件内的连续页码，每次最多处理 200 页。')
            return False
        interval = first, last
        if getattr(self,'visible_range',None) != interval:
            self.visible_range = interval
            self.source_reader.set_pages(first, last, total, self.originals)
            self.target_reader.set_pages(first, last, total, self.originals)
            self.render_results()
        return True

    def switch_comparison(self, on):
        owner = self.source_reader if on else self.target_reader
        owner.header.insertWidget(0,self.compare)
        self.source_reader.compare(on)
        self.target_reader.setVisible(not on)

    def edit_page(self, number, raw):
        self.originals[number] = raw
        self.edits[number] = self.edits.get(number, 0)+1

    def edit_source(self):
        self.edit_page(1, self.source.toPlainText())
        self.render_result_label()
        self.update_controls()

    def choose_preferences(self, *_):
        self.chosen = True

    def apply_preferences(self, fields):
        if self.chosen or self.pending or self.selection or self.originals or self.last_record:
            return
        self.direction.set_direction(fields['direction'])
        self.model.set_model(fields['translation_model'])

    def choose(self):
        document = self.mode=='document'
        path, _ = QFileDialog.getOpenFileName(self, '更换文档' if document else '更换图片', '',
            '文档 (*.pdf *.txt)' if document else '图片 (*.png *.jpg *.jpeg)')
        if path:
            self.select_path(path)

    def paste(self):
        image = QApplication.clipboard().image()
        if image.isNull():
            self.notice.emit('剪贴板里没有可导入的图片。')
            return
        clipboard = self.task_root/'incoming'/f'{uuid.uuid4().hex}.png'
        if not image.save(str(clipboard)):
            self.notice.emit('无法读取剪贴板图片，请选择本机图片。')
            return
        self.select_path(clipboard, clipboard=True)

    def select_path(self, path, clipboard=False):
        return self.begin('select', Path(path) if clipboard else None, source_path=str(path), clipboard=clipboard)

    def start_extraction(self):
        self.submit(True)

    def start_translation(self):
        self.submit(False)

    def submit(self, extract):
        if self.client.state != 'ready' or self.pending or self.client.task_request_pending():
            return
        direction = self.direction.direction()
        model = self.model.effective()
        first, last = 1, 1
        plan = dict(mode=self.mode, direction=direction, model=model)
        if self.mode == 'text':
            plan['source'] = self.source.toPlainText()
            if not plan['source'].strip() or extract:
                return
        else:
            if not self.selection:
                return
            if self.mode == 'document':
                if not self.change_range():
                    return
                first, last = self.page_range()
            plan.update(token=self.selection['token'], first=first, last=last,
                        originals={str(n): self.originals[n] for n in range(first, last+1) if n in self.originals})
        values = [plan['source']] if self.mode=='text' else plan['originals'].values()
        if sum(len(value.encode()) for value in values)>MAX_INPUT_BYTES:
            self.status.setText('单次提交的原文上限为 1 MiB。')
            return
        raw = json.dumps(plan, ensure_ascii=False, separators=(',',':')).encode()
        path = self.task_root/'incoming'/f'{uuid.uuid4().hex}.r13.json'
        try:
            atomic_write(raw, path)
        except (OSError, CoreError) as exc:
            self.status.setText(f'尚未提交原文。{exc}')
            return
        self.begin('extract' if extract else 'translate', path,
                   plan=path.name, plan_bytes=len(raw), plan_sha256=sha(raw), frozen=plan)

    def begin(self, action, handoff=None, frozen=None, **fields):
        request = self.client.start_operation('workbench.'+action, mode=self.mode, **fields)
        if not request:
            if handoff:
                handoff.unlink(missing_ok=True)
            self.notice.emit('请等待工作进程就绪及当前操作结束。')
            return False
        self.pending = dict(request=request, action=action, handoff=handoff, frozen=frozen,
                            edits=dict(self.edits), stop=False, final=None, selection=None)
        self.status.setText({'select':'正在读取文件…','extract':'正在提取原文…','translate':'正在准备本次翻译…'}[action])
        self.preview_results = {}
        self.update_controls()
        return True

    def request_cancel(self):
        if self.pending and self.client.cancel():
            self.status.setText('正在取消…')
            self.preview_results.clear()
            self.render_results()

    def request_stop(self):
        op = self.client.operation
        if self.pending and self.pending['action']=='translate' and self.client.state=='running' and op:
            if self.client.send('workbench.stop', operation_id=op[0], lease_id=op[1]):
                self.pending['stop'] = True
                self.status.setText('正在停止，将保留已完成页…')
                self.update_controls()

    def receive_snapshot(self, snapshot):
        update = snapshot.get('workbench_update')
        context = self.client.snapshot_context or {}
        pending = self.pending
        if update is None or not pending or context.get('request') != pending['request']:
            return
        if 'selection' in update:
            pending['selection'] = update['selection']
        if 'results' in update:
            pending['final'] = update
        page = update.get('page')
        if 'original' in update and self.edits.get(page, 0)==pending['edits'].get(page, 0):
            body = update['original']
            self.originals[page] = body
            if self.mode == 'document':
                self.source_reader.set_source(page, body)
            else:
                with QSignalBlocker(self.source):
                    self.source.setPlainText(body)
        if self.client.state == 'cancelling' or pending['stop']:
            return
        if 'message' in update:
            self.status.setText(update['message'])
        if 'preview' in update:
            self.preview_results[page] = update['preview']
            self.render_results()

    def receive_event(self, event):
        pending = self.pending
        if not pending or event.get('request_id') != pending['request']:
            return
        kind = event['type']
        if kind == 'error':
            self.finish('failed', {'error':event['error'].get('message', event['error']['code'])})
        elif kind == 'operation.finished':
            self.finish(event['status'], event['result']['detail'])

    def connection_changed(self, state):
        if self.pending and state in ('failed', 'disconnected'):
            self.finish('failed', {'error':'工作进程已断开。重新连接后可继续编辑原文。'})
        self.update_controls()

    def finish(self, status, detail):
        pending, self.pending = self.pending, None
        handoff = pending['handoff']
        if handoff:
            handoff.unlink(missing_ok=True)
        self.preview_results.clear()
        if status in ('completed','stopped'):
            if pending['selection']:
                self.install_selection(pending['selection'])
            final = pending['final']
            if final:
                self.last_record = detail.get('task_id')
                if self.mode == 'document':
                    for row in final['results']:
                        self.results[final['direction'], final['model'], row['page']] = row['target']
                else:
                    row = final['results'][0]
                    self.output.setPlainText(row['target'])
                    self.result_binding = row['source'], final['direction'], final['model']
                    self.render_result_label()
            message = detail.get('message','操作完成。')
        elif status=='cancelled':
            message = '已取消。本次译文未保存，当前原文已保留。'
        else:
            message = detail.get('error') or '操作已中断，可重新提交。'
        if pending['action']=='select' and status=='completed' and self.mode=='document' and self.selection['total']>200:
            message = '此文档超过 200 页。请先选择最多 200 页的连续范围。'
        self.status.setText(message)
        if pending['action']=='select' and status not in ('completed','stopped'):
            self.notice.emit(message)
        self.render_results()
        self.update_controls()

    def install_selection(self, selected):
        self.selection = selected
        self.filename.full_text = selected['name']
        self.filename.setText(selected['name'])
        self.filename.setToolTip(selected['name'])
        self.last_record = None
        self.originals = {int(key): value for key,value in selected['originals'].items()}
        self.edits.clear()
        self.results = {(row['direction'],row['model'],row['page']):row['target'] for row in selected['results']}
        if self.mode=='document':
            total = selected['total']
            self.first.validator().setTop(total)
            self.last.validator().setTop(total)
            self.span.setVisible(selected['pdf'])
            self.visible_range = None
            self.full_range()
            if total > 200:
                self.source_reader.set_pages(1, 1, total, self.originals)
                self.target_reader.set_pages(1, 1, total, self.originals)
        else:
            self.preview.set_image(QPixmap(selected['preview']))
            with QSignalBlocker(self.source):
                self.source.clear()
            self.output.clear()
            self.result_label.hide()
        self.selection_ready.emit()

    def render_results(self, *_):
        if self.mode != 'document':
            return
        direction, model = self.direction.direction(), self.model.effective()
        frozen = (self.pending or {}).get('frozen') or {}
        preview = self.preview_results if (direction,model)==(frozen.get('direction'),frozen.get('model')) else {}
        for n in self.source_reader.pages:
            body = preview.get(n, self.results.get((direction,model,n),''))
            self.source_reader.set_target(n,body)
            self.target_reader.set_target(n,body)

    def render_result_label(self, *_):
        if not self.last_record or not hasattr(self,'result_binding'):
            return
        source,direction,model = self.result_binding
        changed = (source,direction,model) != (self.source.toPlainText(),self.direction.direction(),self.model.effective())
        self.result_label.setText(f'上次提交 · {direction_label(direction)} · {model}' if changed else '')
        self.result_label.setVisible(changed)

    def output_copy(self):
        if self.output.toPlainText():
            QApplication.clipboard().setText(self.output.toPlainText())

    def output_export(self):
        target = self.output.toPlainText()
        if not target or not self.last_record:
            return
        source = self.result_binding[0]
        path, _ = QFileDialog.getSaveFileName(self,'导出双语 TXT','文字翻译-双语.txt','UTF-8 文字 (*.txt)')
        if path:
            try:
                atomic_write((source+'\n\n'+target+'\n').encode(),Path(path))
            except (OSError,CoreError) as exc:
                self.status.setText(f'导出未完成。{exc}')
            else:
                self.status.setText('双语 TXT 已导出。')

    def lookup(self, editor):
        raw = editor.textCursor().selectedText().strip()
        if raw:
            self.lookup_requested.emit(raw)
        else:
            self.status.setText('先选中要查的词。')

    def transfer(self, raw, direction):
        current = self.source.toPlainText().strip()
        if current and current != raw.strip():
            return False
        self.source.setPlainText(raw)
        self.direction.set_direction(direction)
        self.choose_preferences()
        return True

    def update_controls(self):
        ready = self.client.state=='ready' and not self.pending
        owned = self.pending and self.client.state=='running'
        selected = bool(self.selection)
        content = bool(self.source.toPlainText().strip()) if self.mode=='text' else selected
        self.translate.setEnabled(bool(ready and content))
        self.cancel.setEnabled(bool(owned))
        if self.mode=='text':
            can_export = bool(self.output.toPlainText())
            self.copy_result.setEnabled(can_export)
            self.export_result.setEnabled(can_export)
        if self.extract:
            self.extract.setEnabled(ready and selected)
            self.replace.setEnabled(ready)
        if self.stop:
            self.stop.setEnabled(bool(owned and not self.pending['stop'] and self.pending['action']=='translate'))
        self.controls_changed.emit()


def open_record(parent, record, open_audio=None):
    dialog = QDialog(parent)
    dialog.setWindowTitle('翻译记录')
    dialog.setAttribute(Qt.WA_DeleteOnClose)
    dialog.setAttribute(Qt.WA_QuitOnClose, False)
    dialog.resize(770, 560)
    box = vbox(dialog, (20, 16, 20, 16), 10)
    box.addWidget(label(record['title'], 'section_title', True))
    model = [direction_label(record['direction']), record['model'], record.get('asr')]
    box.addWidget(label(' · '.join(filter(None,model)), 'secondary', True))
    if record.get('warning'):
        box.addWidget(label(record['warning'], 'tiny', True))
    body = ReadingEdit()
    body.setReadOnly(True)
    body.setAccessibleName('翻译记录正文')
    destination = record.get('locator') or {}
    block = None
    chunks = []
    lines = 0
    for section in record['sections']:
        caption = section['caption']
        if caption:
            chunks.append(caption)
            lines += 1
        if section.get('id') == destination.get('unit_id') and destination.get('field')=='原文':
            block = lines+1
        text = '原文\n'+section['source']
        chunks.append(text)
        lines += text.count('\n')+1
        if section.get('id') == destination.get('unit_id') and destination.get('field')=='译文':
            block = lines+1
        text = '译文\n'+section['target']+'\n'
        chunks.append(text)
        lines += text.count('\n')+1
    body.setPlainText('\n'.join(chunks))
    box.addWidget(body, 1)
    actions = hbox(spacing=8)
    if open_audio:
        actions.addWidget(Button('在音频工作区打开', None, 'secondary', open_audio))
    actions.addStretch()
    close = Button('关闭', None, 'secondary', dialog.close)
    actions.addWidget(close)
    box.addLayout(actions)
    dialog.show()
    if block is not None:
        body.setTextCursor(QTextCursor(body.document().findBlockByNumber(block)))
        body.centerCursor()
    return dialog
