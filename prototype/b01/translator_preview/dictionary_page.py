"""Persistent Qt dictionary view over the shared, query-owned C channel."""
from copy import deepcopy
import json
import time

from PySide6.QtCore import Qt, QEvent, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QDialog, QPlainTextEdit, QSizePolicy,
                               QWidget)

from .components import Button, Combo, ReadingEdit, frame, hbox, label, scrollable, vbox
from .workspace import PageLifecycle

DIRECTION_NAMES = {'zh-en': '中→英', 'en-zh': '英→中'}
STATUS_TEXT = {
    'empty': '输入中文或英文词语', 'idle': '输入后主动查询',
    'ready': '点击查询查看结果', 'direction_ambiguous': '方向待确认：可更改方向，或用完整输入查询两侧词典。',
    'unsupported_input': '请输入中文或英文词语；数字、符号或不支持的文字不能查词。',
    'input_too_long': '请查询词语（最多 128 字素且 2048 UTF-8 字节）；较长内容可带入翻译。完整输入已保留。',
    'querying': '正在查词…', 'match': '已收录', 'completed': '本次查询已完成',
    'no_match': '当前词典未收录此完整输入',
    'resource_missing': '词典资源未准备，请查看资源说明。',
    'resource_unchecked': '词典尚未核验，请在偏好设置中点击“核验文件”。',
    'resource_corrupt': '词典资源无法读取或校验不符，请查看资源说明。',
    'resource_incompatible': '词典版本或资源配置不兼容，请查看资源说明。',
    'query_failed': '本次查询未完成，请主动重试。', 'cancelled': '本次查词已取消，可主动再次查询。',
    'unsupported_capability': '当前工作进程不支持词典，请使用本次候选。',
    'unsupported_contract': '词典接口版本不支持，请使用匹配的工作进程。',
    'unsupported_protocol': '工作进程通信版本不支持。',
    'unsupported_rules': '词典规则版本不支持。',
    'analysis_mismatch': '输入分析与工作进程不一致，请重新连接后主动查询。',
    'resource_changed': '词典资源已变化，旧结果与分页已失效；请检查资源后主动查询。',
    'invalid_cursor': '分页已失效，请主动重新查询。',
    'invalid_limit': '分页容量无效，请重新查询。',
    'page_budget_exceeded': '本页内容超过传输容量，查询未完成。',
    'disconnected': '词典工作进程未连接。', 'closing': '正在结束本窗口的查词。',
}
DISTRIBUTION_TEXT = {
    'conditions_recorded_not_distributed': '许可条件已记录；尚未随包分发',
    'upstream_data_rights_unresolved_local_use_authorized': '本机开发使用；上游数据发行材料尚未核清',
    'not_loaded': '未载入发行材料', 'self_authored_fixture': '自编工程夹具',
}


def message(state):
    return STATUS_TEXT.get(state, '查词未完成：' + state)


def show_information(parent, title, value):
    dialog = QDialog(parent); dialog.setWindowTitle(title); dialog.resize(650, 460)
    box = vbox(dialog, (24, 24, 24, 24), 12)
    output = QPlainTextEdit(); output.setReadOnly(True); output.setAccessibleName(title)
    output.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))
    box.addWidget(output, 1); box.addWidget(Button('关闭', callback=dialog.accept))
    dialog.exec(); dialog.deleteLater()


class QueryEdit(ReadingEdit):
    """Real preedit lifecycle; Enter confirms IME before it becomes submit intent."""
    submitted = Signal()
    ime_event = Signal(dict)

    def __init__(self):
        super().__init__()
        self.composing = False; self.commit_turn = False
        self.setObjectName('dictionaryQuery')
        self.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.setAccessibleName('查询词语'); self.setPlaceholderText('输入中文或英文词语')
        self.setFixedHeight(72); self.setTabChangesFocus(True)

    def inputMethodEvent(self, event):
        was_composing = self.composing
        self.composing = bool(event.preeditString())
        if was_composing and not self.composing or event.commitString():
            self.commit_turn = True
            QTimer.singleShot(0, self.finish_commit_turn)
        self.ime_event.emit(dict(preedit=event.preeditString(), commit=event.commitString(),
                                composing=self.composing))
        super().inputMethodEvent(event)

    def finish_commit_turn(self):
        self.commit_turn = False

    def event(self, event):
        if event.type() == QEvent.ShortcutOverride and event.key() in (Qt.Key_Return, Qt.Key_Enter):
            # Let this editor own Enter, including Cmd/Ctrl+Enter during preedit.
            event.accept(); return True
        return super().event(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if not self.composing and not self.commit_turn and not event.isAutoRepeat():
                self.submitted.emit()
            event.accept(); return
        super().keyPressEvent(event)


class DirectionResults(QWidget):
    def __init__(self, page, direction):
        super().__init__(); self.page, self.direction = page, direction
        self.group = None; self.items = []; self.copy_buttons = []; self.focus_targets = []
        outer = vbox(self, spacing=8)
        self.title = label(DIRECTION_NAMES[direction], 'section_title'); outer.addWidget(self.title)
        self.status = label('', 'tiny', True); outer.addWidget(self.status)
        self.body = QWidget(); self.rows = vbox(self.body, (16, 8, 16, 12), 0)
        self.scroll = scrollable(self.body); self.scroll.setAccessibleName(DIRECTION_NAMES[direction]+'词条与来源')
        self.scroll.setFocusPolicy(Qt.StrongFocus); outer.addWidget(self.scroll, 1)
        self.more = Button('查看更多', 'down', 'quiet', lambda:page.more(direction))
        self.more.setAccessibleName(DIRECTION_NAMES[direction]+'查看更多'); outer.addWidget(self.more)
        self.clear()

    def clear(self):
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget(): item.widget().hide(); item.widget().deleteLater()
        self.group = None; self.items = []; self.copy_buttons = []; self.focus_targets = []
        self.status.setText('尚未查询'); self.more.hide(); self.hide()

    def consume(self, group, append=False):
        if not append: self.clear()
        self.show(); self.group = deepcopy(group)
        resource = group['resource']
        self.title.setText(DIRECTION_NAMES[self.direction]+' · '+resource.get('resource_id','词典'))
        version = resource.get('source_version', '')
        matching = {'exact': '原词头命中', 'normalized': '规范化命中'}.get(group.get('match_mode'), '')
        self.status.setText(' · '.join(x for x in (message(group['status']), matching, version) if x))
        self.status.setToolTip(version)
        for item in group['items']:
            self.add_item(item, resource)
        self.more.setVisible(bool(group.get('has_more')))
        self.more.setEnabled(self.page.channel.state != 'querying')

    def add_item(self, item, resource):
        self.items.append(deepcopy(item))
        tile = frame('record'); box = vbox(tile, (0, 14, 0, 14), 6)
        headword = item.get('headword', '')
        same_record = len(self.items)>1 and self.items[-2]['source_locator']==item['source_locator']
        if headword and not same_record:
            head = headword
            if item.get('traditional') and item['traditional'] != headword: head += ' / '+item['traditional']
            box.addWidget(label(head, 'panel_title', True))
            if item.get('phonetic'): box.addWidget(label(item['phonetic'], 'tiny', True))
        meta = []
        if item.get('pos'): meta.append(item['pos'])
        if item.get('labels'): meta += item['labels']
        if item['kind'] in ('definition', 'reference', 'entry_information', 'source_information', 'record_information'):
            meta.append('资料 · 无可复制译词')
        if meta: box.addWidget(label(' · '.join(meta), 'tiny', True))
        text = item.get('text')
        if item.get('status') == 'item_too_large': text = '此资料过大，无法在本页完整展示；未截断释义。可查看源记录定位。'
        if item.get('status') == 'translation_missing': text = '词头已收录，但源记录未提供中文释义。'
        body = label(text or '源记录资料', 'detail_term' if item.get('copy_text') else 'secondary', True)
        body.setMinimumWidth(0); body.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        body.setFocusPolicy(Qt.StrongFocus); box.addWidget(body)
        self.focus_targets.append(body)
        source_group = item.get('source_group')
        if source_group:
            context = label('源组 '+str(item['source_group_ordinal'])+'：'+source_group, 'tiny', True)
            context.setMinimumWidth(0); context.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
            context.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
            context.setFocusPolicy(Qt.StrongFocus); box.addWidget(context)
            self.focus_targets.append(context)
        actions = hbox(spacing=6)
        locator = item['source_locator']
        origin = Button('来源 · '+str(locator['record_id']), None, 'quiet',
                        lambda:show_information(self, '词条来源与原始字段', dict(resource=resource, item=item)))
        origin.setAccessibleName('查看 '+headword+' 源记录 '+str(locator['record_id']))
        copy_text = item.get('copy_text')
        copy = Button('复制' if copy_text else '资料', 'copy' if copy_text else None, 'quiet',
                      lambda:self.page.copy_item(self.direction, item))
        copy.setEnabled(isinstance(copy_text,str) and bool(copy_text))
        copy.setAccessibleName('复制 '+copy_text if copy_text else '资料，无可复制译词')
        self.copy_buttons.append(copy)
        self.focus_targets.extend((origin,copy))
        actions.addWidget(origin); actions.addStretch(); actions.addWidget(copy); box.addLayout(actions)
        self.rows.addWidget(tile)


class DictionaryResources(QWidget):
    def __init__(self, channel):
        super().__init__(); self.channel = channel
        box = vbox(self, spacing=12); box.addWidget(label('离线词典资源', 'section_title'))
        self.summaries = {}
        for direction in DIRECTION_NAMES:
            row = frame('feature'); layout = vbox(row, (16, 14, 16, 14), 6)
            summary = label('', 'secondary', True); self.summaries[direction] = summary
            layout.addWidget(summary)
            layout.addWidget(Button('查看来源、许可与发行说明', None, 'quiet', lambda checked=False,d=direction:self.details(d)))
            box.addWidget(row)
        box.addWidget(label('首次使用或资源变化后，请点击本页上方的“核验文件”，完成后即可查词。', 'tiny', True))
        box.addWidget(Button('刷新词典资源状态', None, 'quiet', channel.describe))
        channel.changed.connect(self.refresh); channel.event.connect(self.refresh); self.refresh()

    def descriptors(self):
        return (self.channel.session.resources or {}).get('resources', [])

    def refresh(self, *_):
        specs = {s['direction']:s for s in self.descriptors()}
        for direction, summary in self.summaries.items():
            spec = specs.get(direction, {})
            summary.setText(DIRECTION_NAMES[direction]+' · '+spec.get('resource_id','未配置')+'\n'+
                spec.get('source_version','')+'\n运行：'+('就绪，可离线查询' if spec.get('runtime_status')=='ready' else message(spec.get('runtime_status','resource_missing')))+'\n发行：'+
                DISTRIBUTION_TEXT.get(spec.get('distribution_status'), '未载入发行材料'))

    def details(self, direction):
        spec = next((s for s in self.descriptors() if s['direction']==direction), {})
        show_information(self, DIRECTION_NAMES[direction]+'来源、许可与发行说明', spec or {'说明':'尚未配置对应词典资源。'})


class DictionaryPage(QWidget, PageLifecycle):
    transfer_requested = Signal(str, str)
    resources_requested = Signal()

    def __init__(self, channel, parent=None):
        super().__init__(parent); self.channel = channel
        self.paging_direction = None; self.result_binding = None
        self.ime_events = []; self.submissions = []; self.timings = []; self.started_query = None
        self.transfer_block = ''; self.unavailable_state = 'disconnected'
        box = vbox(self, (36, 28, 36, 20), 14)
        heading = hbox(); heading.addWidget(label('词典','heading')); heading.addStretch()
        heading.addWidget(label('中英双向 · 离线词头查询', 'tiny')); box.addLayout(heading)
        search = frame('editor_card'); row = hbox(search, (16, 10, 16, 10), 10)
        row.addWidget(label('查询词语','panel_title'))
        self.search = QueryEdit(); row.addWidget(self.search,1)
        self.clear_button = Button('清空',None,'quiet', self.search.clear); row.addWidget(self.clear_button)
        self.query_button = Button('查询','search','primary',lambda:self.query()); row.addWidget(self.query_button)
        self.cancel_button = Button('取消',None,'quiet',channel.cancel); self.cancel_button.hide(); row.addWidget(self.cancel_button)
        box.addWidget(search)
        direction = hbox(spacing=10)
        self.direction_label = label('', 'secondary', True); direction.addWidget(self.direction_label,1)
        direction.addWidget(label('更改方向','tiny'))
        self.mode = Combo(); self.mode.setAccessibleName('更改方向'); self.mode.setFixedWidth(142)
        for key,title in (('auto','自动识别'),('zh-en','中→英'),('en-zh','英→中')): self.mode.addItem(title,key)
        direction.addWidget(self.mode); box.addLayout(direction)
        self.status = label('', 'secondary', True); self.status.setAccessibleName('词典查询状态'); box.addWidget(self.status)
        self.results = QWidget(); columns = hbox(self.results,spacing=18)
        self.panels = {d:DirectionResults(self,d) for d in DIRECTION_NAMES}
        for panel in self.panels.values(): columns.addWidget(panel,1)
        self.results.setMinimumHeight(100); box.addWidget(self.results,1)
        footer = hbox(spacing=10)
        self.resources_button = Button('词典资源与来源', 'book','quiet',self.resources_requested.emit)
        footer.addWidget(self.resources_button); footer.addStretch()
        self.transfer_button = Button('带入翻译','arrow','secondary',self.transfer); footer.addWidget(self.transfer_button)
        box.addLayout(footer)
        self.transfer_status = label('', 'tiny', True); box.addWidget(self.transfer_status)
        self.search.textChanged.connect(lambda:channel.edit(self.search.toPlainText()))
        self.search.submitted.connect(lambda:self.query('enter'))
        self.search.ime_event.connect(self.record_ime)
        self.mode.activated.connect(lambda index:self.correct(self.mode.itemData(index)))
        channel.changed.connect(self.refresh); channel.result.connect(self.consume)
        self.setTabOrder(self.search,self.mode); self.setTabOrder(self.mode,self.query_button)
        self.setTabOrder(self.query_button,self.cancel_button)
        self.refresh()

    def record_ime(self, event):
        self.ime_events.append(event); self.ime_events[:] = self.ime_events[-32:]
        if not event['composing']: self.channel.passive('compositionend')

    def prefill(self, raw):
        self.search.blockSignals(True); self.search.setPlainText(raw); self.search.blockSignals(False)
        self.channel.prefill(raw); self.search.setFocus(Qt.OtherFocusReason)

    def correct(self, mode):
        self.channel.set_mode(mode); self.search.setFocus(Qt.OtherFocusReason)

    def binding(self):
        s=self.channel.session
        return (s.raw,s.mode,s.revision,(s.resources or {}).get('resource_set_id'))

    def clear_results(self):
        self.result_binding = None
        for panel in self.panels.values(): panel.clear()

    def refresh(self, *_):
        s = self.channel.session; analysis=s.analysis
        if s.result is None and self.channel.state != 'querying': self.clear_results()
        self.mode.blockSignals(True); self.mode.setCurrentIndex(self.mode.findData(s.mode)); self.mode.blockSignals(False)
        names=[DIRECTION_NAMES[d] for d in analysis.directions]
        self.direction_label.setText(('自动识别' if s.mode=='auto' else '手动指定')+' · '+
            ('方向待确认' if len(names)!=1 else names[0]))
        state=self.channel.state
        if state in ('unsupported_capability','unsupported_contract','unsupported_protocol','disconnected'):
            self.unavailable_state=state
        display=analysis.status if state in ('idle','ready','empty','direction_ambiguous') else state
        if not self.channel.available and state!='closing':display=self.unavailable_state
        self.status.setText(message(display))
        busy=state=='querying'
        self.query_button.setText('重试查询' if state in ('query_failed','cancelled','invalid_cursor') else '查询')
        self.query_button.setEnabled(self.channel.available and bool(analysis.directions) and not busy)
        self.cancel_button.setVisible(busy); self.cancel_button.setEnabled(busy)
        self.clear_button.setEnabled(bool(s.raw))
        for panel in self.panels.values(): panel.more.setEnabled(not busy and self.result_binding==self.binding())
        self.transfer_button.setEnabled(bool(s.raw) and not self.transfer_block)
        self.transfer_status.setText(self.transfer_block or '带入完整查询原文；需要确定方向并保护已有翻译草稿，由你主动翻译。')

    def query(self, intent='button', *, cursors=None):
        if self.search.composing or self.search.commit_turn: return None
        self.paging_direction = next(iter(cursors)) if cursors else None
        if not cursors: self.clear_results()
        self.started_query=time.perf_counter()
        query_id=self.channel.submit(intent,composing=self.search.composing,limit=20,cursors=cursors)
        if query_id:
            self.submissions.append(dict(query_id=query_id, raw=self.channel.session.raw, mode=self.channel.session.mode,
                                         cursors=cursors or {})); self.submissions[:]=self.submissions[-64:]
        else: self.refresh()
        return query_id

    def consume(self, result):
        if result['status'] in ('cancelled','resource_changed'):
            self.clear_results(); self.refresh(); return
        for group in result.get('groups',[]):
            d=group['direction']
            if d not in result.get('page_directions',[]) or group['status']=='not_requested': continue
            append=self.paging_direction==d and self.result_binding==self.binding()
            self.panels[d].consume(group,append=append)
        self.result_binding=self.binding(); self.paging_direction=None
        self.update_tab_order()
        self.refresh()
        if self.started_query is not None:
            started=self.started_query; self.started_query=None
            QTimer.singleShot(0,lambda:self.timings.append(dict(ms=(time.perf_counter()-started)*1000,
                scope='submit-to-next-Qt-event-after-widget-build', query_id=result['owner']['query_id'],
                statuses=[g['status'] for g in result.get('groups',[])])))

    def more(self, direction):
        panel=self.panels[direction]
        if self.result_binding!=self.binding() or self.channel.state=='querying' or not panel.group: return None
        cursor=panel.group.get('next_cursor')
        return self.query(cursors={direction:cursor}) if cursor else None

    def update_tab_order(self):
        controls = [self.search,self.mode,self.query_button,self.cancel_button]
        for panel in self.panels.values():
            if panel.group:
                controls.extend([panel.scroll,*panel.focus_targets,panel.more])
        controls.extend([self.resources_button,self.transfer_button])
        for before,after in zip(controls,controls[1:]): self.setTabOrder(before,after)

    def copy_item(self,direction,item):
        if self.result_binding!=self.binding() or item not in self.panels[direction].items: return
        text=item.get('copy_text')
        if isinstance(text,str) and text: QApplication.clipboard().setText(text)

    def transfer(self):
        if self.transfer_block: return
        s=self.channel.session; directions=s.analysis.directions
        # Query admission can reject a valid translation-sized input. A manual
        # direction remains explicit even when dictionary capacity is exceeded.
        direction=s.mode if s.mode in DIRECTION_NAMES else directions[0] if len(directions)==1 else None
        if direction is None:
            self.status.setText('带入翻译前，请先明确选择中→英或英→中。')
            self.mode.showPopup(); return
        self.transfer_requested.emit(s.raw,direction)

    def enter_page(self):
        super().enter_page(); self.channel.passive('page')

    def leave_page(self):
        super().leave_page(); self.channel.passive('page')
