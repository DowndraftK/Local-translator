"""Normal settings controls for confirmed defaults and owned resource versions."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLineEdit, QMessageBox, QWidget

from translator_core.languages import DEFAULT_DIRECTION
from translator_core.translation_routes import AUTO_MODEL
from .components import Button, Combo, hbox, label, vbox
from .dictionary_page import show_information
from .model_controls import LanguagePair, TranslationModel, captioned


class PreferencesPanel(QWidget):
    def __init__(self, channel, parent=None, *, local=False):
        super().__init__(parent)
        self.channel = channel
        self.restoring = False
        self.local_dirty = False
        self.callback = None
        self.guard_open = False
        box = vbox(self, spacing=12)
        self.direction = Combo()
        self.direction.addItem('英语 → 简体中文', 'en-zh')
        self.direction.addItem('简体中文 → 英语', 'zh-en')
        self.direction.setAccessibleName('默认翻译方向')
        self.model = Combo()
        self.model.addItem('HY-MT2 7B' if local else 'HY-MT2 7B · 默认', 'hy-mt2:7b-q8')
        self.model.addItem('HY-MT2 1.8B' if local else 'HY-MT2 1.8B · 速度选项', 'hy-mt2:1.8b-q8')
        self.model.setAccessibleName('默认翻译模型')
        for title, control in (('默认方向',self.direction),('优先翻译模型' if local else '翻译模型',self.model)):
            row = hbox(spacing=16)
            row.addWidget(label(title,'secondary'))
            row.addWidget(control,1)
            box.addLayout(row)
        self.backend = Combo()
        self.backend.addItem('连接明确配置的本机服务', 'external_ollama')
        if not local:
            self.backend.addItem('使用本应用准备的翻译引擎', 'application_ollama')
        self.backend.setAccessibleName('默认翻译后端')
        if not local:
            row = hbox(spacing=16)
            row.addWidget(label('翻译后端','secondary'))
            row.addWidget(self.backend,1)
            box.addLayout(row)
        else:
            self.backend.hide()
            box.addWidget(label('优先连接此本机 Ollama 地址。','secondary'))
        self.endpoint = QLineEdit()
        self.endpoint.setAccessibleName('本机翻译服务地址')
        self.endpoint.setPlaceholderText('http://127.0.0.1:端口')
        self.endpoint.setMaxLength(256)
        box.addWidget(self.endpoint)
        box.addWidget(label('文字、文档和图片采用默认方向与模型，页面已选方向优先。文件与录音采用英语 → 简体中文，语音后端固定为 MLX 编码与 MPS 高精度解码。','tiny',True))
        row = hbox(spacing=10)
        self.save = Button('保存偏好',None,'primary',self.save_values)
        self.discard = Button('放弃更改',None,'quiet',self.discard_values)
        self.reload = Button('重新读取',None,'quiet',lambda:self.channel.action('reload'))
        self.confirm = Button('确认已写入偏好',None,'secondary',lambda:self.channel.action('confirm'))
        for button in (self.save,self.discard,self.reload,self.confirm):
            row.addWidget(button)
        row.addStretch()
        box.addLayout(row)
        self.status = label('正在读取偏好','tiny',True)
        self.status.setAccessibleName('偏好保存与生效状态')
        box.addWidget(self.status)
        for combo in (self.direction,self.model,self.backend):
            combo.currentIndexChanged.connect(self.edited)
        self.endpoint.textEdited.connect(self.edited)
        channel.changed.connect(self.refresh)
        channel.client.changed.connect(self.refresh)
        channel.client.event.connect(self.refresh)
        self.refresh()

    def values(self):
        return dict(direction=self.direction.currentData(), translation_model=self.model.currentData(),
                    translation_backend=self.backend.currentData(), endpoint=self.endpoint.text(),
                    speech_backend='wlk_mlx_mps')

    def edited(self, *_):
        if not self.restoring:
            self.local_dirty = True
            self.refresh()

    def save_values(self):
        return self.channel.action('save', values=self.values())

    def discard_values(self):
        self.local_dirty = False
        return self.channel.action('discard')

    def refresh(self, *_):
        value = self.channel.description or {}
        ready = self.channel.client.state == 'ready' and not self.channel.pending and not self.channel.client.task_request_pending()
        if value and not self.local_dirty:
            self.restoring = True
            fields = value['values']
            for key,control in (('direction',self.direction),('translation_model',self.model),('translation_backend',self.backend)):
                control.setCurrentIndex(control.findData(fields[key]))
            self.endpoint.setText(fields['endpoint'])
            self.restoring = False
        dirty = self.local_dirty or value.get('dirty',False)
        if value and self.local_dirty and self.values() == value['saved_values'] and not self.channel.pending:
            self.local_dirty = False
            dirty = value['dirty']
        blocked = value.get('blocked',False) or value.get('unconfirmed',False)
        for control in (self.direction,self.model,self.backend):
            control.setEnabled(ready and not blocked)
        self.endpoint.setEnabled(ready and not blocked and self.backend.currentData()=='external_ollama')
        self.save.setEnabled(ready and dirty and not blocked)
        self.discard.setEnabled(ready and dirty and not value.get('unconfirmed',False))
        self.reload.setEnabled(ready and (not dirty or blocked))
        self.confirm.setVisible(value.get('unconfirmed',False))
        self.confirm.setEnabled(ready)
        message = ('偏好尚未保存。' if dirty else f"偏好已确认 · 修订 {value.get('revision',0)} · 下一次新建任务采用此设置。") if value else '正在读取偏好'
        if value.get('unconfirmed'):
            message = '偏好写入尚待确认，请保留本页并点击确认。'
        if value.get('warning'):
            message += ' '+value['warning']
        if self.channel.error:
            message += ' '+self.channel.error['message']
        if self.channel.client.state in ('running','cancelling'):
            message += ' 当前操作进行中，偏好更改将在空闲后可用。'
        self.status.setText(message)
        if self.callback and not self.channel.pending and not dirty and not blocked and not self.channel.error:
            callback,self.callback=self.callback,None
            QTimer.singleShot(0,callback)

    def guard_switch(self, callback=None):
        value = self.channel.description or {}
        if self.channel.pending:
            self.callback = callback
            return False
        if value.get('unconfirmed'):
            return False
        if not self.local_dirty and not value.get('dirty'):
            return True
        if self.guard_open:
            return False
        self.guard_open = True
        try:
            dialog = QMessageBox(self)
            dialog.setWindowTitle('翻译偏好尚未保存')
            dialog.setAttribute(Qt.WA_QuitOnClose,False)
            dialog.setText('保存当前偏好，供下一次新建任务使用？')
            save=dialog.addButton('保存偏好',QMessageBox.AcceptRole)
            discard=dialog.addButton('放弃更改',QMessageBox.DestructiveRole)
            back=dialog.addButton('返回设置',QMessageBox.RejectRole)
            dialog.setEscapeButton(back)
            dialog.exec()
            selected=dialog.clickedButton()
            dialog.deleteLater()
            if selected is save:
                self.callback=callback
                self.save_values()
            elif selected is discard:
                self.callback=callback
                self.discard_values()
            return False
        finally:
            self.guard_open=False

    def prepare_close(self, callback):
        return self.guard_switch(callback)


class LocalPreferencesPanel(QWidget):
    def __init__(self, channel):
        super().__init__()
        self.channel = channel
        self.local_dirty = False
        self.saved_pair = None
        self.callback = None
        box = vbox(self, spacing=12)
        row = hbox(spacing=14)
        self.direction = LanguagePair()
        self.model = TranslationModel(channel.client, '默认翻译模型')
        row.addWidget(captioned('默认方向', self.direction))
        row.addWidget(captioned('默认翻译模型', self.model))
        row.addStretch()
        box.addLayout(row)
        row = hbox(spacing=10)
        self.save = Button('保存偏好', None, 'primary', self.save_values)
        self.reset = Button('恢复默认', None, 'secondary', self.restore_defaults)
        row.addWidget(self.save)
        row.addWidget(self.reset)
        row.addStretch()
        box.addLayout(row)
        self.status = label('', 'tiny', True)
        self.status.setAccessibleName('偏好保存与生效状态')
        box.addWidget(self.status)
        self.direction.changed.connect(self.edited)
        self.model.activated.connect(self.edited)
        channel.changed.connect(self.refresh)
        channel.client.changed.connect(self.refresh)
        channel.client.event.connect(self.refresh)
        self.refresh()

    def pair(self):
        return self.direction.direction(), self.model.selected()

    def edited(self, *_):
        self.local_dirty = self.pair()!=self.saved_pair
        self.refresh()

    def save_values(self):
        value = self.channel.description or {}
        if value.get('unconfirmed'):
            return self.channel.action('confirm')
        if self.needs_reload():
            return self.channel.action('reload')
        direction,model = self.pair()
        return self.channel.action('save', values=dict(value['saved_values'], direction=direction,
                                                       translation_model=model))

    def restore_defaults(self):
        self.direction.set_direction(DEFAULT_DIRECTION)
        self.model.set_model(AUTO_MODEL)
        self.edited()
        self.save_values()

    def needs_reload(self):
        value = self.channel.description or {}
        error = self.channel.error or {}
        return value.get('blocked') or error.get('code')=='preferences_changed'

    def refresh(self, *_):
        value = self.channel.description
        ready = bool(value and self.channel.client.state=='ready' and not self.channel.pending
                     and not self.channel.client.task_request_pending())
        if value:
            saved = value['saved_values']
            pair = saved['direction'], saved['translation_model']
            if not self.local_dirty and pair!=self.saved_pair:
                self.direction.set_direction(pair[0])
                self.model.set_model(pair[1])
            if self.pair()==pair:
                self.local_dirty = False
            self.saved_pair = pair
        blocked = bool(value and (value['unconfirmed'] or self.needs_reload()))
        self.direction.setEnabled(ready and not blocked)
        self.model.setEnabled(ready and not blocked)
        self.save.setEnabled(ready and (self.local_dirty or blocked or value['dirty'] or not value['disk_sha256']))
        self.reset.setEnabled(ready and not blocked)
        if self.channel.pending:
            message = '正在保存偏好…'
        elif self.channel.error:
            message = self.channel.error['message']
            if self.needs_reload():
                message += ' 点击「保存偏好」重新读取后再保存。'
        elif self.local_dirty:
            message = '有未保存的选择。'
        elif value:
            message = value['warning'] or '新工作区采用此设置。'
        else:
            message = '正在读取偏好…'
        self.status.setText(message)
        if self.callback and not self.channel.pending:
            callback, self.callback = self.callback, None
            QTimer.singleShot(0, callback)

    def guard_switch(self):
        return not self.channel.pending

    def prepare_close(self, callback):
        if self.channel.pending:
            self.callback = callback
            return False
        return True


class ManagedPanel(QWidget):
    def __init__(self, channel, parent=None):
        super().__init__(parent)
        self.channel=channel
        box=vbox(self,spacing=12)
        box.addWidget(label('准备、修复和回退会核验受控版本。卸载前确认功能影响；外部模型保留只读引用。','tiny',True))
        self.component=Combo()
        self.component.setAccessibleName('要管理的应用资源')
        self.version=Combo()
        self.version.setAccessibleName('应用自有资源版本')
        box.addWidget(self.component)
        box.addWidget(self.version)
        row=hbox(spacing=10)
        self.prepare=Button('准备',None,'primary',lambda:self.operate('prepare'))
        self.repair=Button('修复',None,'secondary',lambda:self.operate('repair'))
        self.rollback=Button('回退到所选版本',None,'secondary',lambda:self.operate('rollback'))
        self.uninstall=Button('卸载所选版本',None,'quiet',self.confirm_uninstall)
        for button in (self.prepare,self.repair,self.rollback,self.uninstall):
            row.addWidget(button)
        row.addStretch()
        box.addLayout(row)
        row=hbox(spacing=10)
        self.cancel=Button('取消资源操作',None,'quiet',channel.client.cancel)
        self.recover=Button('恢复中断事务',None,'secondary',lambda:self.operate('recover'))
        self.refresh_button=Button('刷新资源版本',None,'quiet',channel.describe)
        self.details=Button('来源与许可材料',None,'quiet',lambda:show_information(self,'资源版本、来源与许可',channel.description or {}))
        for button in (self.cancel,self.recover,self.refresh_button,self.details):
            row.addWidget(button)
        row.addStretch()
        box.addLayout(row)
        self.status=label('正在读取应用资源版本','tiny',True)
        self.status.setAccessibleName('资源生命周期状态')
        box.addWidget(self.status)
        self.component.currentIndexChanged.connect(self.refresh)
        self.version.currentIndexChanged.connect(self.update_buttons)
        channel.changed.connect(self.refresh)
        channel.client.changed.connect(self.refresh)
        channel.client.event.connect(self.refresh)
        self.refresh()

    def selected(self):
        return next((item for item in (self.channel.description or {}).get('components',[])
                     if item['id']==self.component.currentData()), None)

    def refresh(self,*_):
        value=self.channel.description or {}
        old=self.component.currentData()
        self.component.blockSignals(True)
        self.component.clear()
        for item in value.get('components',[]):
            self.component.addItem(item['title'],item['id'])
        index=self.component.findData(old)
        if index>=0:
            self.component.setCurrentIndex(index)
        self.component.blockSignals(False)
        item=self.selected()
        old_version=self.version.currentData()
        self.version.blockSignals(True)
        self.version.clear()
        for row in (item or {}).get('versions',[]):
            self.version.addItem(('当前 · ' if row['active'] else '保留 · ')+row['instance'][:12]+' · '+row['state'],row['instance'])
        index=self.version.findData(old_version)
        if index>=0:
            self.version.setCurrentIndex(index)
        self.version.blockSignals(False)
        progress=self.channel.progress
        message='当前组件尚未准备。' if item and not item['active'] else '当前组件已准备，主动核验后可取得运行证据。' if item else '本候选没有安装来源清单。'
        current=value.get('current') or {}
        if current.get('stage')=='completed':
            message='资源操作已完成并确认。'+message
        if self.channel.expected:
            message='正在准备资源，可继续查看历史、查词或切换外观。'
            if progress:
                message+=f" 已处理 {progress['byte_count']/1024/1024:.1f} MB · {progress['stage']}"
        if value.get('pending'):
            message+=' 有中断事务等待恢复。'
        if self.channel.error:
            message+=' '+self.channel.error['message']
        if value.get('warning'):
            message+=' '+value['warning']
        self.status.setText(message)
        self.update_buttons()

    def update_buttons(self,*_):
        value=self.channel.description or {}
        ready=self.channel.client.state=='ready' and not self.channel.pending and not self.channel.client.task_request_pending() and not value.get('blocked')
        item=self.selected()
        self.component.setEnabled(ready)
        self.version.setEnabled(ready)
        self.prepare.setEnabled(ready and item is not None)
        self.repair.setEnabled(ready and item is not None)
        self.rollback.setEnabled(ready and bool(self.version.currentData()) and self.version.currentData()!=(item or {}).get('active'))
        self.uninstall.setEnabled(ready and bool(self.version.currentData()))
        self.recover.setEnabled(self.channel.client.state=='ready' and not self.channel.pending and
                                (bool(value.get('pending')) or bool(value.get('blocked'))))
        self.cancel.setEnabled(bool(self.channel.expected) and self.channel.client.state=='running')
        self.refresh_button.setEnabled(self.channel.client.state in ('ready','running','cancelling') and not self.channel.pending)

    def operate(self,action):
        return self.channel.operate(action,self.component.currentData(),
                                    self.version.currentData() if action in ('rollback','uninstall') else None)

    def confirm_uninstall(self):
        item=self.selected()
        if not item:
            return
        dialog=QMessageBox(self)
        dialog.setWindowTitle('卸载应用自有资源')
        dialog.setAttribute(Qt.WA_QuitOnClose,False)
        dialog.setText('卸载'+item['title']+'的所选版本？\n'+
                       {'speech-runtime':'语音识别、精修和音频解码需要可用语音环境。',
                        'translation-engine':'选择本应用后端的新翻译需要重新准备引擎。',
                        'dictionary':'双向离线查词需要重新准备词典。'}.get(item['id'],''))
        remove=dialog.addButton('确认卸载',QMessageBox.DestructiveRole)
        back=dialog.addButton('保留资源',QMessageBox.RejectRole)
        dialog.setEscapeButton(back)
        dialog.exec()
        accepted=dialog.clickedButton() is remove
        dialog.deleteLater()
        if accepted:
            self.channel.operate('uninstall',item['id'],self.version.currentData(),confirmed=True)
