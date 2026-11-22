"""Source selection and explicit live controls over the same audio workspace."""
from PySide6.QtWidgets import QWidget

from .components import Button, Combo, hbox, label, vbox

STATES = dict(preparing='正在准备识别与翻译，请等待正在录音提示', waiting_permission='等待系统权限确认', starting='来源已就绪，等待实际帧',
              recording='正在录音', pausing='正在停止输入并保存尾部', paused='已暂停，字幕可能仍在处理',
              stopping='正在停止输入', finalizing='录音已停止，正在识别和翻译收尾', sealed='录音已封存',
              interrupted='录音已中断，等待主动恢复', failed='本次录音未完成', save_blocked='录音保存受阻')


class CaptureControls(QWidget):
    def __init__(self, page):
        super().__init__()
        self.page = page
        self.description = None
        self.control_request = None
        self.awaiting = None
        box = vbox(self, spacing=6)
        row = hbox(spacing=8)
        self.kind = Combo()
        self.kind.addItem('麦克风', 'microphone')
        self.kind.addItem('电脑声音', 'system_audio')
        self.kind.setAccessibleName('声音来源')
        self.kind.setMaximumWidth(160)
        self.devices = Combo()
        self.devices.setAccessibleName('选定麦克风输入设备')
        self.devices.setMinimumWidth(150)
        self.refresh = Button('刷新设备与权限', None, 'secondary', lambda: page.send('capture.describe'))
        row.addWidget(self.kind)
        row.addWidget(self.devices, 1)
        row.addWidget(self.refresh)
        box.addLayout(row)
        row = hbox(spacing=8)
        self.start = Button('开始同传', 'mic', 'primary', self.begin)
        self.pause = Button('暂停录音', None, 'secondary', self.toggle_pause)
        self.stop = Button('停止并收尾', None, 'secondary', lambda: self.control('stop'))
        self.recover = Button('恢复封存录音', None, 'quiet', lambda: page.operate('capture.recover'))
        for button in (self.start, self.pause, self.stop, self.recover):
            row.addWidget(button)
        row.addStretch()
        box.addLayout(row)
        self.hint = label('请刷新设备并选择声音来源。', 'tiny', True)
        self.hint.setAccessibleName('采集来源、权限和范围说明')
        box.addWidget(self.hint)
        self.kind.currentIndexChanged.connect(self.update_controls)

    def begin(self):
        if not self.description or self.page.speech_issue() or not self.page.guard_switch():
            return
        self.page.operate('capture.start', source_kind=self.kind.currentData(),
            device_uid=self.devices.currentData(), selection_generation=self.description['generation'],
            **self.page.execution_choices())

    def toggle_pause(self):
        task = self.page.task()
        value = task['audio'].get('recording') if task else None
        self.control('resume' if value and value['state']=='paused' else 'pause')

    def control(self, action):
        task = self.page.task()
        operation = self.page.client.operation
        value = task['audio'].get('recording') if task else None
        if not operation or not value:
            return
        self.control_request = self.page.client.send('capture.control', operation_id=operation[0], lease_id=operation[1],
            task_id=task['id'], capture_session=value['session'], epoch=value['activeEpoch'], action=action)
        self.awaiting = action
        self.update_controls()

    def receive_description(self, description):
        self.description = description
        selected = self.devices.currentData()
        task = self.page.task()
        if selected is None and task:
            selected = (task['audio'].get('recording') or {}).get('source', {}).get('deviceUID')
        self.devices.clear()
        for device in description['devices']:
            if device['alive']:
                self.devices.addItem(device['name'] + ' · ' + str(device['rate']) + ' Hz', device['uid'])
        if self.devices.findData(selected) >= 0:
            self.devices.setCurrentIndex(self.devices.findData(selected))
        elif selected is not None:
            self.devices.insertItem(0, '原输入设备暂不可用，请重新选择', None)
            self.devices.setCurrentIndex(0)
        self.update_controls()

    def receive_snapshot(self):
        task = self.page.task()
        value = task['audio'].get('recording') if task else None
        if self.awaiting and value:
            expected = {'pause': ('paused',), 'resume': ('recording',), 'stop': ('sealed', 'failed', 'interrupted')}
            if value['state'] in expected[self.awaiting]:
                self.awaiting = None
        self.update_controls()

    def receive_event(self, event):
        if event['type'] == 'error' and event['request_id'] == self.control_request:
            self.control_request = None
            self.awaiting = None
            self.page.status.setText(event['error']['message'])
        if event['type'] == 'operation.finished' and event['request_id'] == self.page.operation_request:
            self.awaiting = None
        self.update_controls()

    def update_controls(self, *_):
        page = self.page
        ready = page.client.state == 'ready' and not page.pending and not page.client.task_request_pending()
        task = page.task()
        value = task['audio'].get('recording') if task else None
        running = bool(page.client.operation and page.client.operation_kind == 'capture.start' and page.operation_request)
        state = value['state'] if value else None
        self.kind.setEnabled(ready)
        self.devices.setVisible(self.kind.currentData() == 'microphone')
        self.devices.setEnabled(ready)
        self.refresh.setEnabled(ready)
        self.start.setEnabled(ready and bool(self.description) and
            (self.kind.currentData() == 'system_audio' or self.devices.currentData() is not None) and
            not page.speech_issue() and
            not (page.saved or {}).get('save_blocked') and
            not (value and value['watermarks']['durable'] and not task['audio']['pcm']))
        self.pause.setText('继续录音' if state=='paused' else '暂停录音')
        self.pause.setEnabled(running and state in ('starting', 'recording', 'paused') and not self.awaiting)
        self.stop.setEnabled(running and state in ('preparing', 'waiting_permission', 'starting', 'recording', 'paused') and not self.awaiting)
        self.recover.setEnabled(ready and bool(value) and not task['audio']['pcm'] and
                                not (page.saved or {}).get('save_blocked'))
        issue = page.speech_issue()
        if issue:
            self.hint.setText(issue)
        elif self.description:
            permission = self.description['microphonePermission'] if self.kind.currentData() == 'microphone' else self.description['systemAudioPermission']
            explanation = ('接收选定输入设备的声音。' if self.kind.currentData() == 'microphone' else
                '直接采集系统允许捕获的整机播放音频，可佩戴耳机。麦克风音轨关闭，本应用输出排除。系统可能要求屏幕与系统音频录制权限。')
            self.hint.setText(explanation + ' 当前查询权限为 ' + permission + '。开始时再次核对实际权限和来源。')
        else:
            self.hint.setText('请刷新设备并选择声音来源。')
