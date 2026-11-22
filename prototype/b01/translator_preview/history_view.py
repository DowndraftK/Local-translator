"""Real summary-only history UI. The worker owns search and metadata."""
from datetime import datetime
from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox, QWidget
from .components import Button, Combo, ElidedLabel, frame, hbox, label, scrollable, vbox
from .dictionary_page import QueryEdit

STATUS_NAMES={None:'全部状态','draft':'已保存原文','preparing':'正在分段','running':'正在处理',
    'completed':'已完成','stopped':'已暂停','partial':'部分失败','failed':'失败','review':'待核对'}
FOUNDATION_EPOCH=978307200


def date_group(updated,now=None):
    local=datetime.fromtimestamp(updated+FOUNDATION_EPOCH)
    today=(now or datetime.now()).date();days=(today-local.date()).days
    if days==0:return '今天'
    if days==1:return '昨天'
    if 2<=days<=6:return '其余近7天'
    return local.strftime('%Y年%m月')


def plain(text,kind='tiny',wrap=False):
    widget=label(text,kind,wrap);widget.setTextFormat(Qt.PlainText);return widget


class RenameDialog(QDialog):
    def __init__(self,view,row):
        super().__init__(view);self.view=view;self.row=row;self.saving=False
        self.setWindowTitle('重命名显示标题');self.resize(480,250)
        box=vbox(self,(24,20,24,20),12)
        box.addWidget(plain('只修改显示标题；留空可恢复原任务名。','secondary',True))
        self.editor=QueryEdit();self.editor.setObjectName('historyTitle');self.editor.setAccessibleName('显示标题')
        self.editor.setPlaceholderText('留空恢复原任务名');self.editor.setPlainText(row.get('renamed_title') or '')
        self.editor.setFixedHeight(68);box.addWidget(self.editor)
        self.error=plain('最多120个字符且512字节；不含换行或控制字符。','tiny',True);box.addWidget(self.error)
        self.editor.submitted.connect(self.save)
        self.editor.ime_event.connect(lambda event:view.record_ime(event,'title'))
        buttons=hbox(spacing=10);buttons.addStretch()
        self.cancel_button=Button('取消',callback=self.reject);buttons.addWidget(self.cancel_button)
        self.save_button=Button('保存',None,'primary',self.save);buttons.addWidget(self.save_button);box.addLayout(buttons)
        self.setTabOrder(self.editor,self.cancel_button);self.setTabOrder(self.cancel_button,self.save_button)
        view.channel.updated.connect(self.saved);view.channel.update_failed.connect(self.failed)
        self.editor.setFocus()
    def save(self):
        if self.saving or self.editor.composing or self.editor.commit_turn:return
        from translator_core.history import display_title
        try:title=display_title(self.editor.toPlainText())
        except ValueError as exc:self.error.setText(str(exc));return
        if not self.view.guard():self.error.setText(self.view.block_reason() or '请先完成当前内容保存。');return
        self.saving=self.view.channel.manage(self.row,dict(display_title=title))
        if self.saving:
            self.editor.setReadOnly(True);self.save_button.setEnabled(False);self.cancel_button.setEnabled(False)
            self.error.setText('正在保存显示标题…')
    def saved(self,result):
        if self.saving and result['task_id']==self.row['id']:self.saving=False;self.accept()
    def failed(self,message):
        if not self.saving:return
        self.saving=False;self.editor.setReadOnly(False);self.save_button.setEnabled(True);self.cancel_button.setEnabled(True)
        self.error.setText(message);self.editor.setFocus()
    def reject(self):
        if not self.saving:super().reject()


class SummaryView(QWidget):
    open_requested=Signal(str)
    locate_requested=Signal(str,dict)
    def __init__(self,bridge,compact=False,parent=None):
        super().__init__(parent);self.bridge=bridge;self.channel=bridge.client.history;self.compact=compact
        self.scope='recent' if compact else 'history';self.channel.register(self.scope)
        self.kind=None;self.cursor_stack=[None];self.current_cursor=None;self.current_result=None
        self.management_error="";self.ime_events=[];self.submissions=[];self.record_buttons=[];self.focus_controls=[]
        self.block_reason=lambda:'请等待当前操作安全结束。' if bridge.client.state!='ready' or bridge.client.task_request_pending() else ''
        self.guard=lambda:not self.block_reason()
        box=vbox(self,(0,0,0,0) if compact else (36,28,36,24),12)
        if not compact:box.addWidget(label('我的翻译','heading'))
        self.search=QueryEdit();self.search.setObjectName('historyQuery');self.search.setAccessibleName('最近记录搜索' if compact else '搜索翻译记录')
        self.search.setPlaceholderText('搜索标题、原文或已确认译文');self.search.setFixedHeight(48 if compact else 52)
        row=hbox(spacing=8);row.addWidget(self.search,1)
        self.search_button=Button('搜索','search','quiet',self.search_submit);row.addWidget(self.search_button)
        box.addLayout(row);self.search.submitted.connect(self.search_submit)
        self.search.textChanged.connect(self.edited);self.search.ime_event.connect(lambda e:self.record_ime(e,'search'))
        self.type_combo=Combo();self.type_combo.setAccessibleName('历史类型筛选')
        for name,value in [('全部类型',None),('文字','文字'),('文档','文档'),('图片','图片'),('音频','音频')]:self.type_combo.addItem(name,value)
        self.type_combo.setVisible(not compact)
        self.status_combo=Combo();self.status_combo.setAccessibleName('历史状态筛选')
        for value,name in STATUS_NAMES.items():self.status_combo.addItem(name,value)
        self.favorite_combo=Combo();self.favorite_combo.setAccessibleName('历史收藏筛选')
        self.favorite_combo.addItem('全部收藏',False);self.favorite_combo.addItem('仅收藏',True)
        self.trash_combo=Combo();self.trash_combo.setAccessibleName('历史记录视图')
        self.trash_combo.addItem('正常记录',False);self.trash_combo.addItem('回收站',True)
        filters=hbox(spacing=8)
        for combo in (self.type_combo,self.status_combo,self.favorite_combo,self.trash_combo):
            combo.setMinimumWidth(0);filters.addWidget(combo,1);combo.currentIndexChanged.connect(self.filter_changed)
        if compact:
            # Recent is always normal records; full management filters stay in My translations.
            self.type_combo.hide();self.status_combo.hide();self.favorite_combo.hide();self.trash_combo.hide()
        else:box.addLayout(filters)
        row=hbox(spacing=8);self.count=plain('尚未查询');row.addWidget(self.count,1)
        self.cancel_button=Button('取消搜索',None,'quiet',lambda:self.channel.cancel(self.scope));row.addWidget(self.cancel_button)
        self.refresh_button=Button('刷新','history','quiet',self.refresh);row.addWidget(self.refresh_button);box.addLayout(row)
        self.list_body=QWidget();self.rows=vbox(self.list_body,spacing=0)
        self.scroll=scrollable(self.list_body);self.scroll.setAccessibleName('最近记录列表' if compact else '翻译历史列表');self.scroll.setFocusPolicy(Qt.StrongFocus)
        box.addWidget(self.scroll,1)
        paging=hbox(spacing=10)
        self.previous=Button('上一页',None,'quiet',self.previous_page);self.more=Button('下一页',None,'quiet',self.next_page)
        paging.addWidget(self.previous);paging.addStretch();paging.addWidget(self.more)
        if compact:self.previous.hide();self.more.hide()
        else:box.addLayout(paging)
        self.errors=plain('','tiny',True);box.addWidget(self.errors)
        self.notice=plain('打开回看已保存内容，命中位置可直接定位。','tiny',True)
        if not compact:box.addWidget(self.notice)
        self.channel.changed.connect(self.changed);self.channel.invalidated.connect(self.invalidated)
        self.channel.result.connect(self.consume);bridge.state_changed.connect(self.update_controls)
        self.channel.updated.connect(self.management_succeeded);self.channel.update_failed.connect(self.management_failed)
        self.update_controls();self.tab_order()
    def record_ime(self,event,field):
        self.ime_events.append(dict(field=field,**event));self.ime_events[:]=self.ime_events[-64:]
    def enter_page(self):
        if not self.current_result:self.refresh()
    def leave_page(self):pass
    def filters(self):
        return dict(text=self.search.toPlainText(),kind=self.kind if self.compact else self.type_combo.currentData(),
            status=None if self.compact else self.status_combo.currentData(),
            favorite=False if self.compact else self.favorite_combo.currentData(),
            trash=False if self.compact else self.trash_combo.currentData(),limit=20)
    def edited(self):
        self.channel.views[self.scope]['filters']=self.filters();self.channel.invalidate(self.scope)
        self.count.setText('输入已变化，请点击搜索或按 Enter。')
    def filter_changed(self,*_):self.refresh()
    def set_kind(self,kind):
        if kind!=self.kind:self.kind=kind;self.refresh()
    def search_submit(self):
        if self.search.composing or self.search.commit_turn:return
        self.refresh()
    def refresh(self):
        self.current_cursor=None;self.cursor_stack=[None];return self.query()
    def query(self):
        query_id=self.channel.submit(self.scope,self.filters(),cursor=self.current_cursor)
        if query_id:self.submissions.append(dict(query_id=query_id,filters=self.filters(),cursor=self.current_cursor));self.submissions[:]=self.submissions[-64:]
        return query_id
    def next_page(self):
        cursor=(self.current_result or {}).get('next_cursor')
        if cursor:self.current_cursor=cursor;self.cursor_stack.append(cursor);self.query()
    def previous_page(self):
        if len(self.cursor_stack)>1:self.cursor_stack.pop();self.current_cursor=self.cursor_stack[-1];self.query()
    def clear_rows(self):
        for control in self.focus_controls:
            control.removeEventFilter(self)
        self.record_buttons=[];self.focus_controls=[]
        while self.rows.count():
            item=self.rows.takeAt(0)
            if item.widget():item.widget().hide();item.widget().deleteLater()
        self.tab_order()
    def invalidated(self,scope):
        if scope!=self.scope:return
        self.current_result=None;self.clear_rows();self.update_controls()
    def changed(self,scope):
        if scope!=self.scope:return
        view=self.channel.views[scope];state=view['state']
        if state=='querying':self.count.setText('正在搜索…')
        elif state=='saving':self.count.setText('正在保存记录修改…')
        elif state=='cancelled':self.count.setText('本次搜索已取消。')
        elif state=='failed':self.count.setText('搜索未完成');self.errors.setText(view['message'])
        elif state=='stale':self.count.setText(view['message'] or '保存内容已更新，请刷新搜索。')
        elif not self.channel.available:self.count.setText('历史查询不可用，请使用匹配的工作进程。')
        self.update_controls()
    def consume(self,scope,result):
        if scope!=self.scope:return
        self.current_result=result;self.rebuild()
    def rebuild(self):
        self.clear_rows();result=self.current_result
        if not result:return
        self.count.setText(f"{result['matched_count']} 条匹配 · 本页 {len(result['items'])} 条"+(' · 已达扫描上限' if result['limited'] else ''))
        day=None
        for record in result['items']:
            group=date_group(record['updated'])
            if group!=day:day=group;self.rows.addWidget(plain(group,'history_date'))
            tile=frame('drawer_record' if self.compact else 'record');column=vbox(tile,(0,12,0,12),7)
            title=ElidedLabel(record['name'],'record_title');title.setTextFormat(Qt.PlainText);title.setToolTip(record['name']);column.addWidget(title)
            info=f"{record['kind']} · {record['direction']} · {STATUS_NAMES.get(record['phase'],record['phase'])} · "+record.get('scale',f"{record['completed']}/{record['total']} 段")
            info+=f" · {record['source_chars']} 字符" if record['kind']=='文字' else ''
            column.addWidget(plain(info,'tiny',True))
            if record.get('source_name') and record['kind']!='文字':
                column.addWidget(plain(record['source_name'],'tiny',True))
            if record.get('parent'):
                parent=record['parent']
                relationship={'refinement':'精修关联版本','new-recording':'关联录音',
                    'review-copy':'核对关联版本','fresh-ocr':'重新识别关联版本'}.get(parent.get('purpose'),'关联版本')
                related=plain(relationship+' · 父记录 '+str(parent.get('id',''))[:8],'tiny',True)
                related.setToolTip('父记录 '+str(parent.get('id',''))+' · 内容修订 '+str(parent.get('revision','')))
                column.addWidget(related)
            if record.get('recording_state'):
                from .capture_controls import STATES
                column.addWidget(plain('录音状态 · '+STATES.get(record['recording_state'],record['recording_state']),'tiny',True))
            snippet=plain((record['matched_field']+'命中：' if record['matched_field'] else '')+record['snippet'],'secondary',True)
            snippet.setMaximumHeight(46);column.addWidget(snippet)
            if record['trashed_at'] is None:
                for hit in record.get('hits',[]):
                    locator=hit['locator']
                    unit=locator['unit_type']
                    target=(f"第{locator['unit_id']}页" if unit=='page' else '图片内容' if unit=='unit' else
                        f"字幕 {locator['start_sample']/16000:.3f}秒" if unit=='cue' else '任务内容')
                    button=Button('定位'+target+' · '+hit['field'],None,'quiet',
                        lambda checked=False,r=record,l=locator:self.locate_requested.emit(r['id'],l))
                    button.setToolTip(hit['snippet']);button.setAccessibleName('定位'+target+' '+hit['field']+' '+record['name'])
                    column.addWidget(button);self.track(button,False,record)
            if record.get('more_hits'):
                column.addWidget(plain('还有更多命中，可缩小查询或打开全文阅读。','tiny',True))
            if record['warning']:column.addWidget(plain('保存/来源提示：'+record['warning'],'tiny',True))
            row=hbox(spacing=6);when=datetime.fromtimestamp(record['updated']+FOUNDATION_EPOCH).strftime('%m-%d %H:%M')
            row.addWidget(plain(when),1)
            if record['trashed_at'] is not None:
                restore=Button('恢复','back','quiet',lambda checked=False,r=record:self.action(r,{'trashed':False}))
                restore.setAccessibleName('恢复记录：'+record['name']);row.addWidget(restore);self.track(restore,True,record)
            else:
                favorite=Button('已收藏' if record['favorite'] else '收藏','bookmark','quiet',lambda checked=False,r=record:self.action(r,{'favorite':not r['favorite']}))
                favorite.setAccessibleName(('取消收藏：' if record['favorite'] else '收藏记录：')+record['name'])
                row.addWidget(favorite);self.track(favorite,True,record)
                rename=Button('重命名',None,'quiet',lambda checked=False,r=record:self.rename(r))
                rename.setAccessibleName('重命名记录：'+record['name']);row.addWidget(rename);self.track(rename,True,record)
                trash=Button('回收',None,'quiet',lambda checked=False,r=record:self.trash(r))
                trash.setAccessibleName('移到回收站：'+record['name']);row.addWidget(trash);self.track(trash,True,record)
                opened=Button('打开','chevron','quiet',lambda checked=False,r=record:self.open_requested.emit(r['id']))
                opened.setAccessibleName('打开记录：'+record['name']);row.addWidget(opened);self.track(opened,False,record)
            column.addLayout(row);self.rows.addWidget(tile)
        if not result['items']:
            empty=plain('回收站暂无匹配记录' if self.filters()['trash'] else '没有匹配的翻译记录','secondary',True)
            empty.setMinimumHeight(100);empty.setAlignment(Qt.AlignCenter);self.rows.addWidget(empty)
        for issue in result['errors']:
            item=QWidget();box=vbox(item,spacing=4);box.addWidget(plain('一条记录无法安全列出：'+issue['message'],'tiny',True))
            opened=Button('只读查看原文与已有结果',None,'quiet',lambda checked=False,i=issue['task_id']:self.open_requested.emit(i))
            opened.setAccessibleName('只读打开异常记录 '+issue['task_id']);box.addWidget(opened);self.rows.addWidget(item)
            self.track(opened,False,dict(kind='文字'))
        self.rows.addStretch()
        self.errors.setText('\n'.join(text for text in (self.management_error,'已达本次扫描上限；结果仅代表已扫描部分，请保留记录并另行整理。' if result['limited'] else '') if text))
        self.tab_order();self.update_controls()
        colors=QApplication.instance().property('translator_colors')
        if colors:
            for button in self.findChildren(Button):button.recolor(colors)
    def track(self,button,management,row):
        self.record_buttons.append((button,management,row));self.focus_controls.append(button)
    def tab_order(self):
        controls=[self.search,self.search_button]
        if not self.compact:controls += [self.type_combo,self.status_combo,self.favorite_combo,self.trash_combo]
        controls += [self.cancel_button,self.refresh_button,self.scroll,*self.focus_controls]
        if not self.compact:controls += [self.previous,self.more]
        self.tab_controls=controls
        for control in controls:control.installEventFilter(self)
        for first,second in zip(controls,controls[1:]):self.setTabOrder(first,second)
    def eventFilter(self,watched,event):
        # Explicit page traversal includes selectors under macOS text-only tab preferences.
        if event.type()==QEvent.KeyPress and event.key() in (Qt.Key_Tab,Qt.Key_Backtab) and watched in self.tab_controls:
            controls=[c for c in self.tab_controls if c.isVisible() and c.isEnabled()]
            if watched in controls and controls:
                step=-1 if event.key()==Qt.Key_Backtab or event.modifiers() & Qt.ShiftModifier else 1
                controls[(controls.index(watched)+step)%len(controls)].setFocus(Qt.BacktabFocusReason if step<0 else Qt.TabFocusReason)
                event.accept();return True
        return super().eventFilter(watched,event)
    def update_controls(self,*_):
        state=self.channel.views[self.scope]['state'];busy=state=='querying'
        self.cancel_button.setVisible(busy);self.search_button.setEnabled(self.channel.available and not self.channel.update_request)
        self.refresh_button.setEnabled(self.channel.available and not self.channel.update_request)
        self.more.setEnabled(bool(self.current_result and self.current_result.get('next_cursor')) and not busy)
        self.previous.setEnabled(len(self.cursor_stack)>1 and not busy)
        reason=self.block_reason()
        for button,management,row in self.record_buttons:
            disabled=(reason if management else ('请等待当前操作结束后打开。' if self.bridge.client.state!='ready' or self.bridge.client.task_request_pending() else ''))
            button.setEnabled(not disabled and bool(self.current_result));button.setToolTip(disabled)
    def action(self,row,changes):
        if not self.guard():self.errors.setText(self.block_reason() or '请先完成当前内容保存。');return False
        return self.channel.manage(row,changes)
    def rename(self,row):
        if self.block_reason():self.errors.setText(self.block_reason());return
        dialog=RenameDialog(self,row);dialog.exec();dialog.deleteLater()
        self.search.setFocus(Qt.OtherFocusReason)
    def trash(self,row):
        answer=QMessageBox.question(self,'移到回收站？','将“'+row['name']+'”移到回收站？\n原文、译文和已导出文件会保留，可在回收站恢复。',QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if answer==QMessageBox.Yes:self.action(row,{'trashed':True})
        self.search.setFocus(Qt.OtherFocusReason)
    def management_succeeded(self,result):
        self.management_error='';self.errors.setText('');self.update_controls()
    def management_failed(self,message):
        self.management_error=message;self.errors.setText(message)
