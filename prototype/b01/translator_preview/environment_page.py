"""A first-run resource overview for the Mac installation."""
from PySide6.QtCore import QStandardPaths, QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QLineEdit, QProgressBar, QWidget

from translator_core.local_environment import OLLAMA_DOWNLOAD, OLLAMA_MODEL_DOWNLOADS
from translator_core.resource_macos import DEFAULT_WHISPER

from .components import Button, frame, hbox, label, vbox
from .dictionary_page import show_information
from .model_controls import MODEL_NAMES


SOURCES = {'bundled':'内置', 'imported':'已导入', 'external':'外部资源'}


class EnvironmentPage(QWidget):
    def __init__(self, client, can_configure):
        super().__init__()
        self.client = client
        self.can_configure = can_configure
        self.report = None
        self.scan_request = self.action_request = None
        self.scan_timer = QTimer(self)
        self.scan_timer.setSingleShot(True)
        self.scan_timer.setInterval(20000)
        self.scan_timer.timeout.connect(self.scan_expired)
        self.action_kind = None
        self.reload_after_scan = False
        self.last_preferences = None
        self.speech = QWidget()
        speech = vbox(self.speech, spacing=12)
        layout = vbox(self, spacing=12)
        top = hbox(spacing=12)
        self.scan_status = label('正在检测本机资源…', 'tiny', True)
        self.scan_status.setAccessibleName('本地资源检测状态')
        self.refresh_button = Button('重新检测', None, 'quiet', self.retry_scan)
        top.addWidget(self.scan_status, 1)
        top.addWidget(self.refresh_button)
        layout.addLayout(top)
        ollama, self.ollama_status, actions = self.card(layout, 'Ollama 翻译')
        self.get_ollama = Button('下载 Ollama', None, 'quiet', lambda: self.open_download(OLLAMA_DOWNLOAD))
        actions.addWidget(self.get_ollama)
        actions.addStretch()
        ollama_details = self.details(ollama, '查看模型清单与连接设置')
        self.model_list = label('', 'tiny', True)
        self.model_list.setAccessibleName('Ollama 模型清单')
        ollama_details.addWidget(self.model_list)
        self.endpoint = QLineEdit()
        self.endpoint.setAccessibleName('本机 Ollama 地址')
        self.endpoint.setMaxLength(256)
        endpoint_row = hbox(spacing=8)
        endpoint_row.addWidget(self.endpoint,1)
        self.save_endpoint = Button('保存地址', None, 'secondary', self.save_address)
        endpoint_row.addWidget(self.save_endpoint)
        ollama_details.addLayout(endpoint_row)
        model_actions = hbox(spacing=10)
        self.model_buttons = {}
        for model, url in OLLAMA_MODEL_DOWNLOADS.items():
            btn = Button('下载 '+MODEL_NAMES[model], None, 'secondary',
                         lambda checked=False, link=url: self.open_download(link))
            size = '1.9 GB' if model == 'hy-mt2:1.8b-q8' else '8.0 GB'
            btn.setToolTip(f'腾讯官方 GGUF，约 {size}。下载后导入 Ollama。')
            self.model_buttons[model] = btn
            model_actions.addWidget(btn)
        model_actions.addStretch()
        ollama_details.addLayout(model_actions)

        overview = frame('feature')
        summary = vbox(overview, (16,14,16,14), 6)
        row = hbox(spacing=8)
        self.speech_summary = label('正在读取语音资源…', 'section_title', True)
        row.addWidget(self.speech_summary, 1)
        self.restore_speech = Button('恢复内置语音资源', None, 'quiet',
                                     lambda: self.operate('environment.restore_speech'))
        row.addWidget(self.restore_speech)
        self.verify_speech = Button('检查语音环境', None, 'secondary', self.check_speech)
        row.addWidget(self.verify_speech)
        summary.addLayout(row)
        self.speech_hint = label('语音用于录音转写和同声传译。', 'tiny', True)
        summary.addWidget(self.speech_hint)
        speech.addWidget(overview)

        whisper, self.whisper_status, actions = self.card(speech, '语音识别模型')
        self.choose_model = Button('导入模型目录…', None, 'secondary', lambda: self.choose_speech('speech.model'))
        actions.addWidget(self.choose_model)
        actions.addStretch()
        whisper.addWidget(label('small.en 用于英语。导入 large-v3-turbo 可用于多语识别。', 'tiny', True))

        assets, self.assets_status, actions = self.card(speech, '分词器与辅助资源')
        self.choose_tokenizer = Button('导入分词文件…', None, 'secondary', self.choose_tokenizer_file)
        self.choose_assets = Button('导入资源目录…', None, 'quiet', lambda: self.choose_speech('speech.assets'))
        actions.addWidget(self.choose_tokenizer)
        actions.addWidget(self.choose_assets)
        actions.addStretch()
        assets.addWidget(label('分词文件支持 gpt2.tiktoken 和 multilingual.tiktoken。完整资源目录还包含梅尔滤波器和语音活动检测文件。', 'tiny', True))

        runtime, self.runtime_status, actions = self.card(speech, 'Whisper 运行组件')
        runtime_details = self.details(runtime, '导入外部运行组件')
        row = hbox(spacing=8)
        self.get_runtime = Button('导入运行 ZIP…', None, 'secondary', self.acquire_runtime)
        self.choose_runtime = Button('导入运行目录…', None, 'quiet', lambda: self.choose_speech('speech.runtime'))
        row.addWidget(self.get_runtime)
        row.addWidget(self.choose_runtime)
        row.addStretch()
        runtime_details.addLayout(row)

        dictionary, self.dictionary_status, actions = self.card(layout, '离线词典')
        self.choose_dict = Button('导入词典', None, 'secondary', self.select_dictionary)
        self.choose_dict.setToolTip('校验并复制词典和索引到应用数据目录')
        self.reset_dict = Button('恢复内置词典', None, 'quiet', lambda: self.operate('environment.dictionary', path=None))
        self.dictionary_license = Button('来源与许可', None, 'quiet', self.show_dictionary_license)
        actions.addWidget(self.choose_dict)
        actions.addStretch()
        dictionary_details = self.details(dictionary, '版本与来源')
        row = hbox(spacing=8)
        row.addWidget(self.reset_dict)
        row.addWidget(self.dictionary_license)
        row.addStretch()
        dictionary_details.addLayout(row)

        _, vision, _ = self.card(layout, 'Apple Vision')
        vision.setText('macOS 系统 OCR，用于从 PDF 和图片提取原文。')

        self.feedback = QWidget()
        feedback = vbox(self.feedback, (24,6,24,8), 4)
        row = hbox(spacing=10)
        self.progress = QProgressBar()
        self.progress.setAccessibleName('资源操作进度')
        self.progress.setRange(0, 100)
        self.cancel = Button('取消操作', None, 'quiet', client.cancel)
        row.addWidget(self.progress, 1)
        row.addWidget(self.cancel)
        feedback.addLayout(row)
        self.notice = label('', 'tiny', True)
        self.notice.setAccessibleName('资源操作结果')
        feedback.addWidget(self.notice)
        client.event.connect(self.on_event)
        client.changed.connect(self.worker_changed)
        client.dictionary.changed.connect(self.render)
        client.preferences.changed.connect(self.render)
        client.resources.changed.connect(self.render)
        self.render()

    def card(self, layout, title):
        card = frame('editor_card')
        box = vbox(card, (16, 14, 16, 14), 8)
        box.addWidget(label(title, 'panel_title'))
        status = label('正在检测…', 'secondary', True)
        status.setAccessibleName(title+'状态')
        box.addWidget(status)
        row = hbox(spacing=10)
        box.addLayout(row)
        layout.addWidget(card)
        return box, status, row

    def open_download(self, url):
        if not QDesktopServices.openUrl(QUrl(url)):
            self.notice.setText('无法打开浏览器，请查看资源说明。')

    def details(self, layout, title):
        button = Button(title, 'down', 'quiet')
        button.setCheckable(True)
        panel = QWidget()
        panel.hide()
        button.toggled.connect(panel.setVisible)
        layout.addWidget(button, alignment=Qt.AlignLeft)
        layout.addWidget(panel)
        return vbox(panel, spacing=6)

    def save_address(self):
        value = self.client.preferences.description
        if value:
            self.client.preferences.action('save', values=dict(value['saved_values'], endpoint=self.endpoint.text()))

    def retry_scan(self):
        if self.client.can_reconnect():
            self.client.connect_worker()
        else:
            self.refresh()

    def refresh(self):
        if self.client.state not in ('ready', 'running', 'cancelling'):
            return
        if self.scan_request:
            self.reload_after_scan = True
            return
        self.scan_request = self.client.send('environment.scan')
        if self.scan_request:
            self.scan_timer.start()
            self.scan_status.setText('正在检测本机组件与资源…')
        else:
            self.scan_status.setText('检测请求未发出，请重试。')
        self.render()

    def scan_expired(self):
        self.scan_request = None
        self.reload_after_scan = False
        self.scan_status.setText('本机资源检测超时，可以重新检测。')
        self.render()

    def worker_changed(self, state):
        if state in ('connecting', 'failed', 'disconnected'):
            self.scan_timer.stop()
            self.scan_request = self.action_request = self.report = None
            self.reload_after_scan = False
            self.action_kind = None
            self.last_preferences = None
            self.notice.clear()
            if state == 'connecting':
                self.scan_status.setText('正在连接工作进程…')
            elif state == 'failed':
                self.scan_status.setText(self.client.connection_error_text())
            else:
                self.scan_status.setText('工作进程未连接，可以重新检测。')
        if state == 'ready' and self.report is None and not self.scan_request:
            QTimer.singleShot(0, self.refresh)
        self.render()

    def operate(self, kind, **fields):
        if self.action_request or not self.can_configure():
            self.notice.setText('请先保存当前偏好和资源更改。')
            return
        request = self.client.start_operation(kind, **fields)
        if request:
            self.action_request = request
            self.action_kind = kind
            self.progress.setRange(0, 0)
            self.notice.setText('正在恢复内置资源…' if kind=='environment.restore_speech'
                                else '正在核对所选资源…')
            self.render()
        else:
            self.notice.setText('请在当前操作完成后重试。')

    def select_dictionary(self):
        if self.client.state != 'ready':
            return
        path, _ = QFileDialog.getOpenFileName(self, '选择要导入的词典配置', '', '词典配置 (*.json)')
        if path:
            self.operate('environment.dictionary', path=path)

    def choose_speech(self, item):
        if self.client.state != 'ready':
            return
        title = {'speech.runtime': '导入兼容的 Whisper 运行环境文件夹',
                 'speech.model': '导入 Whisper 模型文件夹', 'speech.assets': '导入分词与 VAD 文件夹'}[item]
        folder = QFileDialog.getExistingDirectory(self.speech, title,
                    QStandardPaths.writableLocation(QStandardPaths.DownloadLocation))
        if folder:
            self.operate('environment.speech_folder', item=item, path=folder)

    def acquire_runtime(self):
        if self.client.state != 'ready':
            return
        path, _ = QFileDialog.getOpenFileName(self.speech, '选择兼容的 Mac 语音运行包 0.2.10-r5',
                                             QStandardPaths.writableLocation(QStandardPaths.DownloadLocation),
                                             '语音运行包 (*.zip)')
        if path:
            self.operate('environment.runtime_zip', path=path)

    def choose_tokenizer_file(self):
        if self.client.state!='ready':
            return
        path, _ = QFileDialog.getOpenFileName(self.speech, '导入 Whisper 分词文件',
                                             QStandardPaths.writableLocation(QStandardPaths.DownloadLocation),
                                             'Whisper 分词文件 (*.tiktoken)')
        if path:
            self.operate('environment.tokenizer',path=path)

    def check_speech(self):
        if self.can_configure():
            return self.client.resources.check(runtime=True)
        return False

    def show_dictionary_license(self):
        descriptions = (self.client.dictionary.session.resources or {}).get('resources', [])
        values = [{key: entry[key] for key in ('resource_id', 'source_version', 'origin', 'license',
                    'notice', 'known_content_limits') if key in entry} for entry in descriptions]
        show_information(self, '词典来源与许可', {'词典': values})

    def speech_verified(self):
        return self.capability_verified('speech.continuous_asr')

    def capability_verified(self, capability):
        rows = (self.client.resources.description or {}).get('capabilities', [])
        return any(row['capability'] == capability and row['prerequisite_satisfied'] for row in rows)

    def on_event(self, event):
        kind = event['type']
        identity = event['request_id']
        if identity == self.scan_request:
            self.scan_timer.stop()
            self.scan_request = None
            if kind == 'environment.description':
                self.report = event['description']
                self.scan_status.setText('本机资源检测完成')
                revision = event.get('resources_revision')
                if revision and revision != (self.client.resources.description or {}).get('selection_revision'):
                    self.client.resources.describe()
            else:
                self.scan_status.setText((event.get('error') or {}).get('message', '检测失败，请重试。'))
            if self.reload_after_scan:
                self.reload_after_scan = False
                QTimer.singleShot(0, self.refresh)
        if identity == self.action_request:
            if kind == 'environment.progress':
                done, total = event['received'], event['total']
                self.progress.setRange(0, 100)
                self.progress.setValue(min(100, int(100*done/total)) if total else 0)
                self.notice.setText(f'已复制 {done/10**6:.0f} / {total/10**6:.0f} MB')
            elif kind in ('operation.finished', 'environment.error'):
                detail = (event.get('result') or {}).get('detail') or {}
                error = event.get('error') or {}
                status = event.get('status')
                self.action_request = None
                success = '已恢复内置语音资源。' if self.action_kind=='environment.restore_speech' else '资源已校验并保存。'
                self.action_kind = None
                self.notice.setText(success if status == 'completed' else
                                    '操作已取消。' if status in ('cancelled', 'lease_expired') else
                                    error.get('message') or detail.get('error') or '操作失败，请重试。')
                QTimer.singleShot(0, self.refresh)
        if kind == 'operation.finished' and (event.get('result') or {}).get('kind') in (
                'managed-resource-operation', 'resource-check'):
            QTimer.singleShot(0, self.refresh)
        if kind in ('ready','preferences.description'):
            value = self.client.preferences.description
            if not value:
                return
            saved = value['saved_values']
            if self.last_preferences and self.last_preferences != saved:
                QTimer.singleShot(0, self.refresh)
            if not self.last_preferences or self.last_preferences['endpoint'] != saved['endpoint']:
                self.endpoint.setText(saved['endpoint'])
            self.last_preferences = saved
        if kind == 'preferences.error' and self.endpoint.hasFocus():
            self.notice.setText(event['error']['message'])
        self.render()

    def render(self, *_):
        ready = self.client.state == 'ready'
        can_change = ready and not self.client.task_request_pending() and self.can_configure()
        waiting = self.client.state in ('connecting', 'failed', 'disconnected', 'closing')
        pending_status = '等待工作进程连接。' if waiting else '等待资源检测。'
        checking = self.client.operation_kind == 'resource.check' and self.client.operation is not None
        self.refresh_button.setEnabled(self.client.can_reconnect() or (
            not self.scan_request and self.client.state in ('ready', 'running')))
        self.feedback.setVisible(bool(self.notice.text() or self.action_request or checking))
        self.progress.setVisible(self.action_request is not None)
        self.cancel.setVisible(self.action_request is not None or checking)
        self.cancel.setText('取消操作' if self.action_request else '取消检查')
        self.cancel.setEnabled(self.client.state == 'running')
        self.choose_dict.setEnabled(can_change)
        self.reset_dict.setEnabled(can_change)
        for action in (self.choose_model,self.choose_tokenizer,self.choose_assets,self.choose_runtime,self.get_runtime):
            action.setEnabled(can_change)
        self.save_endpoint.setEnabled(can_change and bool(self.client.preferences.description))
        report = self.report or {}
        ollama, speech = report.get('ollama', {}), report.get('whisper', {})
        compatible = [row for row in ollama.get('external', []) if row['compatible']]
        external_models = {name for row in compatible for name in row['models']}
        for name, button in self.model_buttons.items():
            button.setVisible(bool(compatible) and name not in external_models)
        if ollama:
            details = []
            for row in ollama['external']:
                models = row.get('catalog', [])
                state = '、'.join(models) if models else '无可用本地模型'
                details.append(f"{row['endpoint']} · {row['version']}\n{state}")
            catalog = self.client.model_catalog
            if catalog:
                message = f'已连接 · {len(catalog)} 个本地模型。自动选择清单首项 {catalog[0]}。'
            elif compatible:
                message = '已连接。请在 Ollama 安装本地翻译模型，再重新检测。'
            elif ollama['external']:
                message = '请更新至 Ollama 0.35.0 或更高正式版，再重新检测。'
            elif ollama['installation']['installed']:
                message = 'Ollama 未运行。请启动 Ollama，再重新检测。'
            else:
                message = '安装并启动 Ollama 后，可以在本机翻译文字。'
            self.ollama_status.setText(message)
            self.model_list.setText('\n'.join(details) or '未连接到本机 Ollama。')
            self.get_ollama.setVisible(not ollama['installation']['installed'] and not ollama['external'])
        else:
            self.ollama_status.setText('正在检测 Ollama 和本地翻译模型…' if self.scan_request else pending_status)
            self.model_list.clear()
            self.get_ollama.hide()
        self.render_speech(speech, pending_status, can_change, checking)
        state = report.get('dictionary', {})
        self.reset_dict.setVisible(state.get('mode') == 'custom' or bool(state.get('warning')))
        descriptors = (self.client.dictionary.session.resources or {}).get('resources', [])
        ready_count = sum(row.get('runtime_status')=='ready' for row in descriptors)
        source = '已导入' if state.get('mode')=='custom' else '内置'
        message = f'{source} · {ready_count} 部词典可查询' if ready_count else '词典暂不可用'
        if descriptors and state.get('warning'):
            message += '。' + state['warning']
        self.dictionary_status.setText(message if descriptors else pending_status)

    def render_speech(self, speech, pending_status, can_change, checking):
        resources = self.client.resources.description or {}
        invalid = {row['resource_id'] for row in resources.get('resources', [])
                   if row['state'] in ('corrupt','incompatible','failed','missing')}
        sources = speech.get('sources', {})
        ready = {resource: bool(speech.get(key) and resource not in invalid) for key,resource in
                 (('runtime','speech.runtime'),('speech.model','speech.model'),('speech.assets','speech.assets'))}
        all_present = all(ready.values())
        embedded = bool(sources) and all(row['origin']=='bundled' for row in sources.values())
        self.restore_speech.setEnabled(bool(can_change and speech.get('bundled') and (not embedded or not all_present)))
        self.verify_speech.setEnabled(can_change and all_present)
        headline, detail = pending_status, 'Whisper 用于录音转写和同声传译。'
        if checking:
            headline = '正在检查语音环境…'
        elif speech:
            if self.speech_verified() and all_present:
                headline = '语音环境已通过检查'
            elif all_present:
                headline = '语音资源齐全'
                detail = '首次使用时会自动核对运行组件和模型。'
            else:
                headline = '语音资源需要检查'
                detail = '可恢复内置资源，或导入缺少的项目。'
            if self.client.resources.error:
                detail = self.client.resources.error['message']
        self.speech_summary.setText(headline)
        self.speech_hint.setText(detail)

        installed = [f'Whisper {name}（{SOURCES.get(origin, "外部资源")}）'
                     for name,origin in speech.get('model_sources', {}).items()]
        self.whisper_status.setText('  ·  '.join(installed) if installed else
                                    '尚未找到兼容模型。' if speech else pending_status)
        for widget,resource,description in (
                (self.assets_status,'speech.assets','分词器、梅尔滤波器和 VAD'),
                (self.runtime_status,'speech.runtime','WhisperLiveKit 及本机依赖')):
            source = sources.get(resource, {})
            if ready[resource] and source:
                prefix = SOURCES.get(source['origin'],'外部资源')
                version = f" {source['version']}" if resource=='speech.runtime' else ''
                widget.setText(f'{prefix}{version} · {description}')
            else:
                widget.setText('文件缺失或未通过检查。' if speech else pending_status)
            widget.setToolTip(source.get('path',''))
        self.whisper_status.setToolTip(sources.get('speech.model',{}).get('path',''))
