"""Production audio file page. All task facts arrive from the shared worker."""
import json
import uuid
from pathlib import Path

from PySide6.QtCore import QEvent, QSignalBlocker, QTimer, Qt, Signal, QSize
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import (QApplication, QFileDialog, QPlainTextEdit,
    QListWidget, QListWidgetItem, QWidget, QCheckBox, QDoubleSpinBox)

from translator_core import CoreError
from translator_core.audio import millis, timestamp, subtitle_text
from translator_core.contracts import sha
from translator_core.languages import direction_label, speech_language
from translator_core.resource_macos import DEFAULT_WHISPER, LEGACY_WHISPER
from translator_core.speech_presets import MODES, PRESETS
from translator_core.store import atomic_write, safe_file
from .components import Button, Combo, DropZone, hbox, label, vbox, scrollable
from .capture_controls import CaptureControls, STATES as CAPTURE_STATES
from .dictionary_page import show_information
from .model_controls import LanguagePair, TranslationModel, captioned


class SubtitleList(QListWidget):
    def fit_rows(self):
        for row in range(self.count()):
            item = self.item(row)
            key = [self.viewport().width(), item.text(), self.font().toString()]
            if item.data(Qt.UserRole) == key: continue
            document = QTextDocument()
            document.setDefaultFont(self.font())
            document.setPlainText(item.text())
            document.setTextWidth(max(100, self.viewport().width() - 24))
            item.setSizeHint(QSize(max(100, self.viewport().width() - 8), int(document.size().height()) + 18))
            item.setData(Qt.UserRole, key)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_rows()


class AudioWorkspace(QWidget):
    controls_changed = Signal()
    back_requested = Signal()

    def __init__(self, client, task_root, *, local=False, external_speech=False):
        super().__init__(); self.client = client; self.root = Path(task_root)
        self.local = local
        self.chosen = False
        self.saved = None; self.selection = None; self.pending = None; self.operation_request = None
        self.local_drafts = {}; self.inflight = None; self.after_draft = None; self.close_callback = None
        self.displaying = False; self.composing = False; self.commit_guard = False; self.ime_events = []
        self.playback = None; self.export_target = None; self.export_request = None
        content = QWidget()
        outer = vbox(content, (24, 18, 24, 18), 8)
        vbox(self).addWidget(scrollable(content))
        self.capture = CaptureControls(self)
        self.live_mode = False
        self.external_speech = external_speech
        self.speech_choices_loaded = False
        row = hbox(spacing=8)
        self.back = Button('返回', 'back', 'quiet', self.back_requested.emit)
        self.choose = Button('选择音频文件…', 'folder', 'secondary', self.choose_file)
        self.create = Button('保存来源', None, 'primary', self.create_task)
        self.details = Button('来源与执行详情', None, 'quiet', self.show_details)
        row.addWidget(self.back)
        self.session_title = label('录音转写', 'heading')
        if local:
            row.addWidget(self.session_title)
        for w in (self.choose, self.create, self.details):
            row.addWidget(w)
        row.addStretch()
        if not local:
            row.addWidget(label('英语 → 简体中文', 'tiny'))
        outer.addLayout(row)
        if local:
            fields = hbox(spacing=12)
            self.direction = LanguagePair()
            self.speech_model = Combo()
            self.speech_model.setAccessibleName('语音转文字模型')
            variants = PRESETS if external_speech else (DEFAULT_WHISPER, LEGACY_WHISPER)
            for variant in variants:
                self.speech_model.addItem(PRESETS[variant]['name'], variant)
            self.model = TranslationModel(client)
            for title, widget in (('翻译方向',self.direction),('语音转文字模型',self.speech_model),('翻译模型',self.model)):
                fields.addWidget(captioned(title, widget))
            fields.addStretch()
            outer.addLayout(fields)
            self.direction.changed.connect(self.choices_changed)
            self.speech_model.currentIndexChanged.connect(self.refresh_controls)
            self.model.activated.connect(self.choices_changed)
            self.speech_status = label('', 'tiny', True)
            outer.addWidget(self.speech_status)
            client.speech_catalog_changed.connect(self.update_speech_catalog)
            if external_speech:
                self.speech_model.activated.connect(self.save_speech_choice)
        self.source_label = label('选择音频后，先保存可恢复来源。识别、翻译与精修分别主动开始。', 'secondary', True)
        outer.addWidget(self.source_label)
        outer.addWidget(self.capture)
        self.capture.hide()
        self.drop = DropZone(); self.drop.setMaximumHeight(48)
        self.drop.setAccessibleName('拖入单个音频文件，也可点击选择')
        drop = hbox(self.drop, (12, 6, 12, 6)); drop.addWidget(label('拖入 WAV / AIFF / MP3 / M4A / FLAC / OGG · 最多30分钟', 'tiny'))
        self.drop.clicked.connect(self.choose_file); self.drop.fileDropped.connect(self.select_path); outer.addWidget(self.drop)
        self.processing = QWidget()
        row = hbox(self.processing, spacing=8)
        self.asr = Button('识别 / 继续识别', None, 'primary', lambda:self.operate('audio.asr'))
        self.translate = Button('翻译 / 补译', None, 'primary', self.start_translation)
        self.refine = Button('录后精修为新版本', None, 'secondary', lambda:self.operate('audio.refine'))
        self.cancel = Button('取消音频操作', None, 'quiet', self.cancel_owned)
        for w in (self.asr, self.translate, self.refine, self.cancel): row.addWidget(w)
        row.addStretch(); outer.addWidget(self.processing)
        self.status = label('尚未保存来源 · 打开记录不会自动识别或翻译', 'tiny', True)
        self.status.setAccessibleName('音频保存、识别与翻译状态'); outer.addWidget(self.status)
        self.temporary = QPlainTextEdit()
        self.temporary.setReadOnly(True)
        self.temporary.setTabChangesFocus(True)
        self.temporary.setMaximumHeight(56)
        self.temporary.setAccessibleName('临时识别假设，等待稳定确认')
        self.temporary.setPlaceholderText('临时识别假设 · 等待稳定确认')
        self.temporary.hide()
        outer.addWidget(self.temporary)
        self.cue_list = SubtitleList(); self.cue_list.setMinimumHeight(140)
        self.cue_list.setWordWrap(True); self.cue_list.setTextElideMode(Qt.ElideNone)
        self.cue_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cue_list.setSelectionMode(QListWidget.SingleSelection)
        self.cue_list.setAccessibleName('带时间点字幕，双击字幕从此处播放')
        self.cue_list.itemSelectionChanged.connect(self.read_cue)
        self.cue_list.itemDoubleClicked.connect(lambda _: self.play_selected())
        if local:
            outer.addWidget(label('字幕', 'section_title'))
        outer.addWidget(self.cue_list, 2)
        self.review_panel = QWidget()
        review_panel = vbox(self.review_panel, spacing=8)
        row = hbox(spacing=10)
        left = vbox(spacing=4); left.addWidget(label('校正原文 · 编辑先保存草稿', 'tiny'))
        self.input = QPlainTextEdit(); self.input.setMaximumHeight(95); self.input.setMinimumHeight(60)
        self.input.setAccessibleName('当前字幕校正草稿'); self.input.setTabChangesFocus(True)
        self.input.installEventFilter(self); self.input.textChanged.connect(self.edited); left.addWidget(self.input)
        right = vbox(spacing=4); right.addWidget(label('当前有效修订的成功主译文', 'tiny'))
        self.output = QPlainTextEdit(); self.output.setReadOnly(True); self.output.setMaximumHeight(95)
        self.output.setAccessibleName('当前字幕成功译文'); self.output.setTabChangesFocus(True); right.addWidget(self.output)
        row.addLayout(left, 1); row.addLayout(right, 1); review_panel.addLayout(row)
        row = hbox(spacing=8)
        self.apply = Button('应用已保存校正', None, 'secondary', lambda:self.edit_action('audio.apply'))
        self.discard = Button('放弃草稿', None, 'quiet', lambda:self.edit_action('audio.discard'))
        self.review = QCheckBox('已核对当前字幕'); self.review.toggled.connect(self.review_cue)
        self.save = Button('保存', None, 'quiet', self.save_task)
        for w in (self.apply, self.discard, self.review, self.save): row.addWidget(w)
        row.addStretch(); review_panel.addLayout(row)
        row = hbox(spacing=8)
        self.play = Button('从选中时间播放', None, 'secondary', self.play_selected)
        self.pause = Button('暂停', None, 'quiet', lambda:self.control('resume' if self.playback and not self.playback.get('playing') else 'pause'))
        self.stop = Button('停止', None, 'quiet', self.cancel_owned)
        self.seek = QDoubleSpinBox(); self.seek.setRange(0, 1800); self.seek.setDecimals(3); self.seek.setSuffix(' 秒')
        self.seek.setAccessibleName('完整音频定位秒数')
        self.locate = Button('定位', None, 'quiet', lambda:self.control('seek',self.seek.value()))
        self.clock = label('00:00:00.000', 'tiny')
        for w in (self.play, self.pause, self.stop, self.seek, self.locate, self.clock):row.addWidget(w)
        row.addStretch(); review_panel.addLayout(row)
        row = hbox(spacing=8)
        self.exports = []
        for fmt in ('txt', 'srt', 'vtt', 'wav'):
            button=Button('导出 '+('双语TXT' if fmt=='txt' else fmt.upper()),None,'quiet',lambda checked=False,f=fmt:self.export(f))
            self.exports.append(button);row.addWidget(button)
        row.addStretch();review_panel.addLayout(row)
        self.copy_all = Button('复制完整双语字幕', None, 'quiet', lambda: QApplication.clipboard().setText(subtitle_text(self.task())) if self.task() else None)
        row.addWidget(self.copy_all)
        self.note = label('字幕时间为模型估计，请核对数字、否定、漏词与末句。现有ASR可能额外生成词，长精修曾重复尾句。精修由你主动建立新版本。', 'tiny', True)
        review_panel.addWidget(self.note)
        outer.addWidget(self.review_panel)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(600); self.timer.timeout.connect(self.flush_drafts)
        self.client.snapshot.connect(self.receive_snapshot);self.client.event.connect(self.receive_event)
        self.client.changed.connect(lambda *_:self.refresh_controls())
        order=[self.back,self.choose,self.create,self.details]
        if local:
            order.extend((self.direction.source,self.direction.swap,self.direction.target,self.speech_model,self.model))
        order.extend([self.capture.kind,self.capture.devices,self.capture.refresh,
               self.capture.start,self.capture.pause,self.capture.stop,self.capture.recover,
               self.asr,self.translate,self.refine,self.cancel,self.temporary,self.cue_list,
               self.input,self.output,self.apply,self.discard,self.review,self.save,self.play,self.pause,self.stop,self.seek,self.locate,*self.exports,self.copy_all])
        for a,b in zip(order,order[1:]):QWidget.setTabOrder(a,b)
        self.refresh_controls()

    def eventFilter(self, watched, event):
        if watched is self.input:
            if event.type()==QEvent.InputMethod:
                self.composing=bool(event.preeditString());self.commit_guard=True
                self.ime_events.append(dict(preedit=event.preeditString(),commit=event.commitString()))
                QTimer.singleShot(0,self.clear_commit)
            if event.type()==QEvent.KeyPress and self.ime_blocked() and event.key() in (Qt.Key_Enter,Qt.Key_Return):
                if event.modifiers() & Qt.ControlModifier:return True
        return super().eventFilter(watched,event)

    def clear_commit(self):
        self.commit_guard=False
        if not self.composing and self.local_drafts:self.timer.start()
        self.refresh_controls()

    def ime_blocked(self):return self.composing or self.commit_guard
    def set_live_mode(self, live):
        changed = self.live_mode != live
        self.live_mode = live
        self.capture.setVisible(live)
        for widget in (self.choose, self.create, self.drop): widget.setVisible(not live)
        if self.local:
            self.session_title.setText('同声传译' if live else '录音转写')
            if not self.task() and not self.selection:
                self.source_label.setText('声音来源' if live else '选择音频文件后保存来源，再开始识别。')
                self.status.setText('尚未开始同传。' if live else '等待选择音频文件。')
            if live and not self.capture.description:
                self.send('capture.describe')
        if changed and self.external_speech and (live or not self.task()):
            self.load_speech_choice()
        self.refresh_controls()

    def load_speech_choice(self):
        mode = 'live' if self.live_mode else 'file'
        variant = self.client.speech_choices.get(mode, DEFAULT_WHISPER)
        index = self.speech_model.findData(variant)
        if index >= 0:
            with QSignalBlocker(self.speech_model):
                self.speech_model.setCurrentIndex(index)

    def update_speech_catalog(self):
        if self.external_speech and isinstance(self.client.speech_catalog, dict) and not self.speech_choices_loaded:
            self.speech_choices_loaded = True
            if not self.task() or self.live_mode:
                self.load_speech_choice()
        elif self.client.speech_catalog is None:
            self.speech_choices_loaded = False
        self.refresh_controls()

    def save_speech_choice(self, *_):
        variant = self.speech_model.currentData()
        mode = 'live' if self.live_mode else 'file'
        self.client.send('environment.speech_choice', item=variant, mode=mode)

    def choices_changed(self, *_):
        self.chosen = True
        self.refresh_controls()

    def apply_preferences(self, fields):
        if self.local and not self.chosen and not self.task() and not self.selection:
            self.direction.set_direction(fields['direction'])
            self.model.set_model(fields['translation_model'])
            self.refresh_controls()

    def execution_choices(self):
        if not self.local:
            return {}
        return dict(direction=self.direction.direction(), asr_model=self.speech_model.currentData(),
                    model=self.model.effective())

    def speech_issue(self, capability=None, variant=None):
        if self.local:
            variant = variant or self.speech_model.currentData()
            try:
                speech_language(variant, self.direction.direction())
            except CoreError as exc:
                return str(exc)
            if self.external_speech:
                capability = capability or ('speech.continuous_asr' if self.live_mode else 'speech.file_asr')
                catalog = self.client.speech_catalog or {}
                status = catalog.get(variant, {}).get('modes', {}).get(capability, '正在检测')
                if status != '可用':
                    return (f"{PRESETS[variant]['name']} · {MODES[capability]}{status}。"
                            "请在「偏好设置 → 语音资源」查看。")
        return ''
    def task(self):return self.saved['task'] if self.saved else None
    def cue(self):
        task=self.task(); row=self.cue_list.currentRow()
        return task['audio']['cues'][row] if task and 0<=row<len(task['audio']['cues']) else None
    def key(self,cue):return (self.task()['id'],cue['id'],cue['revision'])

    def refresh_controls(self):
        ready=self.client.state=='ready' and not self.pending and not self.client.task_request_pending() and not self.ime_blocked()
        task=self.task(); bound=bool(task and self.client.current_task and task['id']==self.client.current_task['id'])
        writable=ready and bound and not self.saved.get('save_blocked');cue=self.cue()
        clean=not self.local_drafts and not self.inflight
        playing=bool(self.client.operation and self.client.operation_kind=='audio.play' and self.operation_request)
        self.choose.setEnabled(ready and clean);self.drop.setEnabled(ready and clean)
        self.create.setEnabled(ready and not task and bool(self.selection) and not self.speech_issue())
        self.details.setEnabled(bool(task or self.selection))
        if self.local:
            choosing = ready and (self.live_mode or not task)
            for field in (self.direction, self.speech_model, self.model):
                field.setEnabled(choosing)
            if self.external_speech:
                capability = 'speech.continuous_asr' if self.live_mode else 'speech.file_asr'
                rows = self.client.speech_catalog or {}
                with QSignalBlocker(self.speech_model):
                    for index in range(self.speech_model.count()):
                        key = self.speech_model.itemData(index)
                        row = PRESETS[key]
                        status = rows.get(key, {}).get('modes', {}).get(capability, '正在检测')
                        try:
                            speech_language(key, self.direction.direction())
                        except CoreError:
                            status = '仅英语'
                        self.speech_model.setItemText(index, f"{row['name']} · {status}")
            self.create.setToolTip(self.speech_issue() or '保存选中的音频来源')
            variant = self.speech_model.currentData()
            speech = self.client.speech_catalog
            message = self.speech_issue() or (f'尚未找到 {variant}。可在「偏好设置 → 语音资源」导入。'
                      if speech is not None and variant not in speech else '')
            self.speech_status.setText(message)
            self.speech_status.setVisible(bool(message))
            self.details.setVisible(bool(task or self.selection))
            self.processing.setVisible(not self.live_mode or bool(task) and ready)
            self.review_panel.setVisible(bool(task) and (not self.live_mode or ready))
        self.asr.setEnabled(writable and clean and not task['audio']['asrComplete'] and not task['drafts'] if task else False)
        if task and task['audio'].get('recording') and not task['audio']['pcm']: self.asr.setEnabled(False)
        self.translate.setEnabled(writable and clean and task['audio']['asrComplete'] and bool(task['audio']['cues']) and not task['drafts'] if task else False)
        self.refine.setEnabled(writable and clean and bool(task['audio']['pcm']) and not task['drafts'] if task else False)
        if self.external_speech and task:
            model = task['audio'].get('asrModel') or LEGACY_WHISPER
            for button, capability in ((self.asr, 'speech.file_asr'), (self.refine, 'speech.refine')):
                issue = self.speech_issue(capability, model)
                if issue:
                    button.setEnabled(False)
                button.setToolTip(issue)
        self.input.setReadOnly(not (writable and cue)); self.cue_list.setEnabled(not self.ime_blocked())
        has_draft=bool(cue and (self.key(cue) in self.local_drafts or cue['id'] in task['drafts']))
        self.apply.setEnabled(writable and has_draft);self.discard.setEnabled(writable and has_draft)
        self.review.setEnabled(writable and bool(cue) and not has_draft);self.save.setEnabled(ready and bound)
        self.save.setText("重试保存" if self.saved and self.saved.get("save_blocked") else "保存")
        self.play.setEnabled(writable and clean and bool(task['audio']['pcm']) if task else False)
        for w in (self.pause,self.stop,self.seek,self.locate):w.setEnabled(playing)
        self.pause.setText('继续' if self.playback and not self.playback.get('playing') else '暂停')
        self.cancel.setEnabled(self.client.state=='running' and bool(self.operation_request))
        for w in self.exports:w.setEnabled(bound and not self.export_request and not self.pending and self.client.state in ('ready','running','cancelling'))
        self.exports[-1].setEnabled(bool(bound and task['audio']['pcm'] and self.client.state == 'ready' and not self.export_request))
        self.copy_all.setEnabled(bound)
        self.capture.update_controls()
        self.controls_changed.emit()

    def guard_switch(self):
        if self.ime_blocked():self.status.setText('请先完成当前中文组合输入。');return False
        if self.local_drafts or self.inflight:
            self.flush_drafts();self.status.setText('请等待字幕草稿保存确认后再切换。');return False
        if self.saved and self.saved.get('save_blocked'):
            self.status.setText('保存受阻，请先完整导出并保留当前窗口。');return False
        return True

    def choose_file(self):
        if not self.client.can_change_task():return
        path,_=QFileDialog.getOpenFileName(self,'选择音频文件（原字节只读）','','音频 (*.wav *.aiff *.aif *.mp3 *.m4a *.flac *.ogg)')
        if path:self.select_path(path)

    def select_path(self,path):
        if not self.guard_switch() or not self.client.can_change_task():return False
        return self.operate('audio.select',source_path=str(path))

    def create_task(self):
        if self.selection:
            action = self.operate if self.local else self.send
            action('audio.create',selection_token=self.selection['token'],**self.execution_choices())

    def send(self,kind,**fields):
        if self.client.state!='ready' or self.pending or self.client.task_request_pending():return False
        self.pending=self.client.send(kind,**fields);self.refresh_controls();return self.pending

    def operate(self,kind,**fields):
        if not self.guard_switch() or self.local_drafts or self.ime_blocked():return False
        self.operation_request=self.client.start_operation(kind,**fields);self.refresh_controls();return self.operation_request

    def start_translation(self):
        if self.translate.isEnabled():return self.operate('task.start')

    def save_task(self):
        if self.local_drafts:self.flush_drafts()
        else:self.send('audio.retry_save' if self.saved and self.saved.get('save_blocked') else 'task.save')

    def receive_snapshot(self,snapshot):
        if 'audio_status' in snapshot:
            if (self.client.snapshot_context or {}).get('request')==self.operation_request:
                self.status.setText(snapshot['audio_status'])
            return
        if 'capture_description' in snapshot:
            self.capture.receive_description(snapshot['capture_description'])
            return
        if 'audio_selection' in snapshot:
            if not self.saved and snapshot['audio_selection']:
                self.selection=snapshot['audio_selection'];self.source_label.setText(self.selection['source']['file']+' · 已恢复暂存来源，请主动保存')
            self.refresh_controls();return
        if 'task' not in snapshot:return
        task=snapshot.get('task')
        if not task or task['kind']!='音频':
            self.saved=None;self.cue_list.clear();self.input.clear();self.output.clear();self.playback=None
            self.clock.setText('00:00:00.000');self.refresh_controls();return
        old=self.task();current=self.cue(); selected=current['id'] if current and old and old['id']==task['id'] else None
        self.saved=snapshot;a=task['audio'];self.source_label.setText(task['name']+' · '+('来源已封存' if a['pcm'] else '录音分块持续保存' if a.get('recording') else '来源已封存')+
            (f" · {a['pcm']['samples']/16000:.3f}秒" if a['pcm'] else ' · 待主动解码'))
        if self.local and not a.get('recording'):
            self.direction.set_direction(task['configuration']['direction'])
            self.model.set_model(task['configuration']['model'])
            if a.get('asrModel'):
                with QSignalBlocker(self.speech_model):
                    self.speech_model.setCurrentIndex(self.speech_model.findData(a['asrModel']))
        self.cue_list.blockSignals(True)
        if not old or old['id'] != task['id']: self.cue_list.clear()
        while self.cue_list.count() > len(a['cues']): self.cue_list.takeItem(self.cue_list.count() - 1)
        for row,c in enumerate(a['cues']):
            state={'pending':'尚未翻译','processing':'翻译中','completed':'已译','failed':'翻译失败','stopped':'翻译中断'}[c['state']]
            value=timestamp(millis(c['startSample']))+' · '+state+' · '+('已核对' if c['reviewed'] else '待核对')+'\n'+c['source']
            if c['state'] == 'completed': value += '\n' + c['result']['translation']
            item=self.cue_list.item(row)
            if item is None: self.cue_list.addItem(QListWidgetItem(value))
            elif item.text()!=value: item.setText(value)
            if c['id']==selected:self.cue_list.setCurrentRow(row)
        if selected is None and a['cues']:self.cue_list.setCurrentRow(0)
        self.cue_list.blockSignals(False)
        self.cue_list.fit_rows()
        phase={'saved':'来源已保存，等待主动识别','decoding':'正在解码','recognizing':'正在识别，临时尾句尚未确认','refining':'正在精修新版本','completed':'识别完成，请核对字幕','stopped':'识别已停止，等待主动继续','failed':'音频处理未完成', **CAPTURE_STATES}[a['phase']]
        done=sum(c['state']=='completed' for c in a['cues'])
        self.status.setText(('保存受阻 · ' if snapshot.get('save_blocked') else '已保存 · ')+phase+
            f" · 字幕{len(a['cues'])}行 · 已译{done}行" + ('\n'+snapshot['warning'] if snapshot.get('warning') else '\n'+a['error'] if a.get('error') else ''))
        if a.get('qualityIssues'): self.status.setText(self.status.text()+'\n'+' '.join(a['qualityIssues']))
        if a.get('notice'): self.status.setText(self.status.text() + '\n' + a['notice'])
        capture = a.get('recording')
        if capture and (not old or old['id'] != task['id']):
            self.capture.kind.setCurrentIndex(self.capture.kind.findData(capture['source']['kind']))
        temporary = a.get('temporary') or {}
        self.temporary.setVisible(bool(temporary.get('text')))
        self.temporary.setPlainText(temporary.get('text', '') + ('\n当前临时假设已达展示上界。' if temporary.get('truncated') else ''))
        if capture:
            water = capture['watermarks']
            self.source_label.setText(task['name'] + ' · ' + capture['source']['name'] + ' · ' + CAPTURE_STATES[capture['state']])
            processed = capture.get('asrProcessedSamples', water['asrConsumed'])
            self.status.setText(self.status.text() + f"\n实际录音 {water['received']/16000:.2f}秒 · 已保存 {water['durable']/16000:.2f}秒 · 识别已送入 {water['asrConsumed']/16000:.2f}秒 · 处理约 {processed/16000:.2f}秒 · 识别积压约 {max(0,water['durable']-processed)/16000:.2f}秒 · 暂停间隙 {capture['pausedWallSeconds']:.2f}秒")
            self.capture.receive_snapshot()
        self.read_cue();self.refresh_controls()

    def read_cue(self):
        c=self.cue();self.displaying=True
        value=self.local_drafts.get(self.key(c),self.task()['drafts'].get(c['id'],{}).get('text',c['source'])) if c else ''
        if self.input.toPlainText()!=value:self.input.setPlainText(value)
        self.displaying=False
        self.output.setPlainText(c['result']['translation'] if c and c['state']=='completed' else '')
        self.review.blockSignals(True);self.review.setChecked(bool(c and c['reviewed']));self.review.blockSignals(False)
        self.refresh_controls()

    def edited(self):
        c=self.cue()
        if self.displaying or not c or self.composing:return
        body=self.input.toPlainText()
        key=self.key(c)
        saved=self.task()['drafts'].get(c['id'],{}).get('text',c['source'])
        if body==saved and key not in self.local_drafts:return
        self.local_drafts[key]=body;self.timer.start();self.refresh_controls()

    def flush_drafts(self):
        if self.ime_blocked() or not self.local_drafts or self.pending or self.inflight or self.client.state!='ready' or self.client.task_request_pending():return
        key,body=next(iter(self.local_drafts.items()));raw=body.encode()
        if len(raw)>2048:self.status.setText('当前草稿超过2048字节，尚未保存，请缩短。');return
        if not self.task() or key[0]!=self.task()['id']:self.status.setText('草稿属于旧任务，当前窗口保留。');return
        name=uuid.uuid4().hex+'.txt'
        try:atomic_write(raw,self.root/'incoming'/name)
        except (OSError,ValueError) as exc:self.status.setText('草稿保存失败 '+str(exc));return
        self.inflight=(key,body)
        if not self.send('audio.draft',cue_id=key[1],cue_revision=key[2],input_file=name,byte_count=len(raw),sha256=sha(raw)):self.inflight=None

    def edit_action(self,kind):
        c=self.cue()
        if not c or self.ime_blocked():return
        self.after_draft=(kind,c['id'],c['revision'])
        if self.local_drafts or self.inflight:self.flush_drafts();return
        self.after_draft=None;self.send(kind,cue_id=c['id'],cue_revision=c['revision'])

    def review_cue(self,value):
        c=self.cue()
        if c and not self.ime_blocked():self.send('audio.review',cue_id=c['id'],cue_revision=c['revision'],reviewed=value)

    def play_selected(self):
        c=self.cue();self.seek.setValue(c['startSample']/16000 if c else 0)
        return self.operate('audio.play',start_sample=c['startSample'] if c else 0)

    def locate_history(self, locator):
        task = self.task()
        if not task or task['id'] != locator['task_id'] or task['revision'] != locator['task_revision']:
            return False
        for row, cue in enumerate(task['audio']['cues']):
            if (cue['id'], cue['revision']) == (locator.get('unit_id'), locator.get('unit_revision')):
                self.cue_list.setCurrentRow(row)
                self.read_cue()
                self.cue_list.scrollToItem(self.cue_list.item(row))
                self.seek.setValue(locator['start_sample'] / locator['rate'])
                self.clock.setText(timestamp(round(self.seek.value() * 1000)))
                self.cue_list.setFocus(Qt.OtherFocusReason)
                return True
        return False

    def control(self,action,seconds=None):
        if self.client.operation and self.client.operation_kind=='audio.play':
            self.client.send('audio.control',operation_id=self.client.operation[0],lease_id=self.client.operation[1],action=action,seconds=seconds)

    def cancel_owned(self):
        if self.operation_request:self.client.cancel()

    def leave_page(self):
        if self.client.operation_kind=='audio.play' and self.operation_request:self.client.cancel()

    def export(self,format):
        path,_=QFileDialog.getSaveFileName(self,'导出完整双语字幕','音频字幕.'+format,format.upper()+' (*.'+format+')')
        if not path:return
        self.export_target=Path(path);self.export_request=self.client.send('audio.export_wav' if format == 'wav' else 'task.export',format=format)
        self.refresh_controls()

    def show_details(self):
        value=self.task() or self.selection
        show_information(self,'音频来源与执行详情',json.dumps(value,ensure_ascii=False,indent=2))

    def prepare_close(self,callback):
        if self.ime_blocked():self.status.setText('请先完成当前输入。');return False
        if self.saved and self.saved.get('save_blocked'):return False
        if self.local_drafts or self.inflight or self.pending:
            self.close_callback=callback;self.flush_drafts();return False
        return True

    def receive_event(self,event):
        kind=event['type'];req=event['request_id']
        self.capture.receive_event(event)
        if kind=='ready':self.send('audio.selection')
        if kind in ('task.done','error') and req==self.pending:
            self.pending=None
            if self.inflight:
                key,body=self.inflight;self.inflight=None
                if kind=='task.done' and self.local_drafts.get(key)==body:self.local_drafts.pop(key,None)
            if kind=='error':self.after_draft=None;self.close_callback=None;self.status.setText(event['error']['message'])
            elif self.local_drafts:QTimer.singleShot(0,self.flush_drafts)
            elif self.after_draft:
                action,cue,revision=self.after_draft;self.after_draft=None
                self.send(action,cue_id=cue,cue_revision=revision)
            if self.close_callback and not self.local_drafts and not self.pending:
                callback=self.close_callback;self.close_callback=None;QTimer.singleShot(0,callback)
            self.read_cue()
        if kind=='operation.finished' and req==self.operation_request:
            self.operation_request=None;detail=event.get('result',{}).get('detail')
            if event['result']['kind']=='audio-selection' and event['status']=='completed':
                self.selection=detail;self.saved=None;self.cue_list.clear();self.input.clear();self.output.clear()
                self.playback=None;self.clock.setText('00:00:00.000');self.seek.setValue(0)
                self.source_label.setText(detail['source']['file']+' · 来源已暂存，请主动保存')
                self.status.setText('暂存来源已封存，保存来源后主动识别。')
            elif event['status'] not in ('completed','cancelled') and detail:self.status.setText(detail.get('error') or str(detail))
            elif event['status']=='cancelled' and self.local:self.status.setText('操作已取消。已保存的音频与字幕可回看。')
            if event['result']['kind']=='audio-play':self.playback=None
        if kind=='error' and req==self.operation_request:
            self.operation_request=None
            self.status.setText(event['error']['message'])
        if kind=='audio.playback' and req==self.operation_request:
            if event['task_id']==(self.task() or {}).get('id') and event['playback'].get('session')==(self.client.operation or (None,))[0]:
                self.playback=event['playback'];self.clock.setText(timestamp(round(self.playback['position']*1000)))

        if kind=='task.exported' and req==self.export_request:
            try:
                name=event['filename']
                if Path(name).name!=name:raise ValueError('导出路径无效')
                raw=safe_file(self.root/'exports'/name,57_600_044)
                if (len(raw),sha(raw))!=(event['byte_count'],event['sha256']):raise ValueError('导出校验失败')
                atomic_write(raw,self.export_target);self.status.setText('完整字幕已导出 '+str(self.export_target))
            except (OSError,ValueError) as exc:self.status.setText('导出未保存 '+str(exc))
            self.export_target=None;self.export_request=None
        if kind=='error' and req==self.export_request:
            self.export_request=None;self.export_target=None;self.status.setText(event['error']['message'])
        self.refresh_controls()
