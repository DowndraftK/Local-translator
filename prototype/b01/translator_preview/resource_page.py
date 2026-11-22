"""Minimal actual-resource controls; no model or dictionary example data."""
from datetime import datetime
import time
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget
from .components import Button, hbox, label, vbox
from .dictionary_page import show_information

NAMES = {'speech.file_asr':'音频文件识别','speech.continuous_asr':'连续识别后端',
         'speech.refine':'录后精修','audio.decode':'音频解码','audio.playback':'音频播放框架'}
STATES = {'unconfigured':'尚未选择资源','unchecked':'尚未核验','files_verified':'文件已核验 · 运行能力未测',
          'runtime_verified':'资源前置已实测 · 识别与加载未测'}


class ResourcePanel(QWidget):
    def __init__(self, channel, parent=None):
        super().__init__(parent); self.channel=channel; self.close_callback=None; self.guard_open=False
        self.guard_events=[]
        self.resume_guard=False
        box=vbox(self,spacing=12)
        box.addWidget(label('选择资源并主动核验；模型加载、实际识别与精修沿各业务入口执行。','tiny',True))
        self.selection=label('尚未选择资源引用','secondary',True); box.addWidget(self.selection)
        row=hbox(spacing=10)
        self.choose=Button('选择现有资源引用','folder','secondary',self.select)
        self.save=Button('保存引用',None,'primary',lambda:self.channel.action('resource.save'))
        self.discard=Button('放弃更改',None,'quiet',lambda:self.channel.action('resource.discard'))
        for button in (self.choose,self.save,self.discard): row.addWidget(button)
        row.addStretch(); box.addLayout(row)
        row=hbox(spacing=10)
        self.files=Button('核验文件',None,'secondary',lambda:self.channel.check(False))
        self.runtime=Button('检查文件与运行能力',None,'secondary',lambda:self.channel.check(True))
        self.cancel=Button('取消检查',None,'quiet',self.cancel_check)
        self.details=Button('查看详情',None,'quiet',self.show_details)
        self.refresh_button=Button('刷新状态',None,'quiet',self.channel.describe)
        for button in (self.files,self.runtime,self.cancel,self.refresh_button,self.details): row.addWidget(button)
        row.addStretch(); box.addLayout(row)
        self.status=label('','tiny',True); self.status.setAccessibleName('资源检查与保存状态'); box.addWidget(self.status)
        self.rows={}
        for key,title in NAMES.items():
            row=hbox(spacing=12); row.addWidget(label(title,'secondary'))
            text=label('尚未选择资源','tiny',True); row.addWidget(text,1); self.rows[key]=text; box.addLayout(row)
        self.last_check=label('上次检查：尚无记录','tiny',True); box.addWidget(self.last_check)
        box.addWidget(label('应用自有软件与词典可通过上方资源操作准备或修复。外部模型缺失时请重新选择匹配的只读资源，再主动核验。','tiny',True))
        self.channel.changed.connect(self.refresh); self.channel.client.changed.connect(self.refresh)
        self.channel.client.event.connect(self.refresh_after_task)
        self.refresh()

    def refresh_after_task(self, event):
        if event['type'] in ('operation.finished', 'task.done', 'error'):
            self.refresh()

    def select(self):
        if not self.channel.guard(): return
        root=str(self.channel.client.preview_data_root or '')
        path,_=QFileDialog.getOpenFileName(self,'选择现有资源引用文件',root,'资源引用 (*.json)')
        if path: self.channel.action('resource.select',reference_path=path)

    def cancel_check(self):
        if self.channel.expected: self.channel.client.cancel()

    def show_details(self):
        show_information(self,'资源来源、许可与检查证据',self.channel.description or {})

    def refresh(self,*_):
        value=self.channel.description or {}; client=self.channel.client
        ready=client.state=='ready' and not self.channel.pending and not client.task_request_pending()
        dirty=value.get('dirty',False); checking=bool(self.channel.expected)
        self.choose.setEnabled(ready and not checking)
        self.save.setEnabled(ready and dirty); self.discard.setEnabled(ready and dirty)
        self.files.setEnabled(ready and value.get('saved',False) and not dirty)
        self.runtime.setEnabled(self.files.isEnabled()); self.cancel.setEnabled(checking and client.state=='running')
        self.details.setEnabled(bool(value))
        self.refresh_button.setEnabled(client.state in ('ready','running','cancelling') and not self.channel.pending)
        self.selection.setText((value.get('title') or '尚未选择资源引用')+(' · 尚未保存' if dirty else ' · 引用已保存' if value.get('saved') else ''))
        self.selection.setToolTip(value.get('reference_path') or '')
        messages=[]
        if dirty: messages.append('选择已更改，请保存引用后再检查。')
        if checking:
            progress=self.channel.progress
            messages.append('正在后台检查，可继续查词、查看历史或切换外观。')
            if progress: messages.append(f"已读取 {progress['byte_count']/1024/1024:.1f} MB")
        elif client.state in ('running','cancelling'): messages.append('其他操作进行中，资源更改和检查暂不可用。')
        attempt=value.get('attempt') or {}
        if attempt.get('status') in ('cancelled','timed_out','failed'):
            messages.append({'cancelled':'本次检查已取消。','timed_out':'本次检查超时。','failed':'本次检查失败。'}[attempt['status']])
            if attempt.get('error'): messages.append(attempt['error']['message'])
        if self.channel.error: messages.append(self.channel.error['message'])
        status=' '.join(messages) or '检查仅在主动操作后进行；读取历史与切页不会检查资源。'
        self.status.setText(status);self.status.setAccessibleName(status)
        for cap in value.get('capabilities',[]):
            problems=[]
            resources={r['resource_id']:r for r in value.get('resources',[])}
            labels={'missing':'文件缺失','corrupt':'校验不符','incompatible':'版本不兼容','failed':'检查失败','stale':'文件已变化','unchecked':'尚未核验'}
            for resource_id in cap['unsatisfied']:
                row=resources.get(resource_id)
                if row:problems.append(row['role']+'：'+labels.get(row['state'],'尚未核验'))
                else:problems.append('未选择所需资源')
            suffix='；'.join(dict.fromkeys(problems))
            self.rows[cap['capability']].setText(STATES[cap['state']]+(' · '+suffix if suffix else ''))
        last=value.get('last_check')
        self.last_check.setText('上次检查：'+datetime.fromtimestamp(last['checked_at']).strftime('%Y-%m-%d %H:%M:%S') if last else '上次检查：尚无记录')
        if self.close_callback and not self.channel.pending:
            callback,self.close_callback=self.close_callback,None
            resume,self.resume_guard=self.resume_guard,False
            if resume or not (self.channel.description or {}).get('dirty') and not self.channel.error: QTimer.singleShot(0,callback)

    def guard_switch(self,callback=None):
        if self.channel.pending:
            if callback:
                self.close_callback=callback
                self.resume_guard=getattr(self.channel.client,'requests',{}).get(self.channel.pending) not in ('resource.save','resource.discard')
            return False
        if not (self.channel.description or {}).get('dirty'): return True
        if self.guard_open: return False
        self.guard_open=True
        self.guard_events.append(dict(at=time.time(),for_close=callback is not None,state=self.channel.client.state))
        self.guard_events[:]=self.guard_events[-32:]
        try:
            dialog=QMessageBox(self); dialog.setWindowTitle('资源引用尚未保存')
            dialog.setAttribute(Qt.WA_QuitOnClose,False)
            dialog.setText('保存当前资源引用后继续？')
            save=dialog.addButton('保存引用',QMessageBox.AcceptRole)
            discard=dialog.addButton('放弃更改',QMessageBox.DestructiveRole)
            cancel=dialog.addButton('返回设置',QMessageBox.RejectRole);dialog.setEscapeButton(cancel)
            dialog.exec(); selected=dialog.clickedButton(); dialog.deleteLater()
            if selected is save or selected is discard:
                self.close_callback=callback
                self.channel.action('resource.save' if selected is save else 'resource.discard')
            return False
        finally: self.guard_open=False

    def prepare_close(self,callback):
        return self.guard_switch(callback)

    def showEvent(self,event):
        super().showEvent(event)
        self.channel.describe()
