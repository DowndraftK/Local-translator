"""One image reader, inherited T04 draft queue and guard, no image-side scheduler."""
import uuid
from pathlib import Path
from PySide6.QtCore import QBuffer, QIODevice, Qt, Signal, QThread
from PySide6.QtGui import QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import QApplication, QFileDialog, QWidget, QDialog, QLabel, QScrollArea
from translator_core.contracts import sha
from translator_core.images import MAX_SOURCE_BYTES, MAX_SOURCE_PIXELS, content, units
from translator_core.pdf_adapter import MAX_IMAGE_BYTES
from translator_core.store import atomic_write, safe_file, is_link
from .components import Button, ImagePreview, vbox, label
from .document_workspace import DocumentWorkspace


class ClipboardCapture(QThread):
    completed = Signal(dict)
    failed = Signal(str)

    def __init__(self, root, encoded, image, parent):
        super().__init__(parent);self.root=root;self.encoded=encoded;self.image=image

    def run(self):
        path=None
        try:
            if self.encoded:
                raw,suffix=self.encoded;origin='clipboard-encoded'
            else:
                buffer=QBuffer();buffer.open(QIODevice.WriteOnly)
                if not self.image.save(buffer,'PNG'):raise ValueError('剪贴板像素编码失败')
                raw=bytes(buffer.data());buffer.close();suffix='.png';origin='clipboard-pixels-png'
            if not 0<len(raw)<=MAX_SOURCE_BYTES:raise ValueError('剪贴板图片编码超过64MiB或为空')
            name='clip-'+uuid.uuid4().hex+suffix;path=self.root/'incoming'/name
            if any(is_link(p) for p in (path.parent,*path.parent.parents)):raise ValueError('剪贴板交接路径不安全')
            atomic_write(raw,path)
            self.completed.emit(dict(source_path=name,origin=origin,byte_count=len(raw),sha256=sha(raw)))
        except (OSError,ValueError) as exc:
            if path and not is_link(path):path.unlink(missing_ok=True)
            self.failed.emit(str(exc))
        finally:self.image=QImage();self.encoded=None


class ImageWorkspace(DocumentWorkspace):
    task_kind='图片'
    format_version=3
    operation_prefix='image'
    selection_ready=Signal()

    def __init__(self,client,task_root,parent=None,auto_refresh=False):
        self.image_ready=False;self.capture_thread=None;self.preview_owner=None
        self.selection_pending=False
        super().__init__(client,task_root,parent,auto_refresh=auto_refresh)
        self.back.setAccessibleName('返回图片上传页')
        self.new.setText('更换图片…');self.create.setText('保存图片来源')
        self.paste_button=Button('粘贴图片','paste','quiet',self.paste)
        self.remove_button=Button('移除','close','quiet',self.remove_image)
        heading=self.layout().itemAt(0).layout()
        heading.insertWidget(3,self.paste_button);heading.insertWidget(4,self.remove_button)
        # Remove PDF-only range controls and labels from the visible image form.
        ranges=self.selection_card.layout().takeAt(1)
        if ranges and ranges.layout():
            while ranges.layout().count():
                item=ranges.layout().takeAt(0)
                if item.widget():item.widget().hide()
        for w in (self.mode,self.pages,self.prev,self.next,self.all_pages,self.first,self.last):w.hide()
        self.canvas=ImagePreview();self.canvas.setMinimumSize(140,140)
        self.canvas.setFocusPolicy(Qt.StrongFocus);self.canvas.setAccessibleName('已封存图片，按比例显示，白色图像背景')
        self.reader.widget(0).layout().insertWidget(2,self.canvas,2)
        self.image_scroll.hide();self.input.setTabChangesFocus(True);self.output.setTabChangesFocus(True)
        self.input.setAccessibleName('图片有效原文或未应用校正草稿')
        self.input.setPlaceholderText('保存图片来源后，主动识别；也可手动补入原文。')
        self.output.setAccessibleName('图片当前有效原文的成功主译文')
        self.copy_button.setText('复制译文');self.discard_button.setText('放弃草稿')
        self.direction.setAccessibleName('图片翻译与识别方向')
        self.focus_order=[self.back,self.new,self.paste_button,self.remove_button,self.details_button,
            self.direction,self.create,self.canvas,self.source_button,self.extract_button,self.input,self.output,
            self.copy_button,self.apply_button,self.discard_button,self.review_box,self.save,self.export_button,
            self.cancel_button,self.start]
        for a,b in zip(self.focus_order,self.focus_order[1:]):QWidget.setTabOrder(a,b)
        self.paste_shortcut=QShortcut(QKeySequence.Paste,self.canvas)
        self.paste_shortcut.setContext(Qt.WidgetShortcut);self.paste_shortcut.activated.connect(self.paste)
        self.image_ready=True;self.update_controls(client.state)

    def update_controls(self,state):
        super().update_controls(state)
        if not self.image_ready:return
        task=self.saved['task'] if self.saved else None
        source=task['image']['source'] if task else self.selection['source'] if self.selection else None
        ready=state=='ready' and not self.pending and not self.client.task_request_pending() and not self.capture_thread and not self.ime_blocked()
        self.title.full_text=source['file'] if source else '图片翻译';self.title.setText(self.title.full_text)
        self.title.setToolTip(self.title.full_text)
        if source:
            g=source['geometry'];origin='文件原字节' if source['origin']=='file-original' else '剪贴板像素规范编码' if source['origin']=='clipboard-pixels-png' else '剪贴板编码图片'
            self.file_summary.setText(f"{origin} · {g['format']} · {g['displayWidth']}×{g['displayHeight']} px · "+('来源已封存' if task else '来源已暂存，请主动保存为任务'))
        else:self.file_summary.setText('选择或粘贴图片后，保存来源、识别和翻译分别主动操作。')
        self.new.setEnabled(ready);self.paste_button.setEnabled(ready)
        self.remove_button.setEnabled(ready and bool(task or self.selection))
        self.create.setEnabled(ready and not task and bool(self.selection))
        self.create.setText('保存图片来源');self.source_button.setText('查看封存原图')
        self.save.setText('重试保存' if self.saved and self.saved.get('save_blocked') else '保存')
        if self.saved and self.saved.get('retry_save_available'):
            self.save.setEnabled(ready and bool(self.client.current_task and task['id']==self.client.current_task['id']))
        if not task:self.source_button.setEnabled(ready and bool(self.selection))
        self.source_button.setToolTip('从已核验封存来源生成按比例预览；不识别、不翻译。')
        self.extract_button.setText('识别图片文字')
        self.copy_button.setText('复制译文')
        self.fork_fresh.setEnabled(self.fork_review.isEnabled())
        if task:
            p=task['image']['units'][0]
            labels={'pending':'尚未识别','recognizing':'正在识别','extracted':'已有有效原文','noText':'没有文字','failed':'识别失败','stopped':'识别已停止'}
            self.page_notice.setText('图片内容 · '+labels[p['state']])
            self.coverage_label.setText(labels[p['state']]+' · '+('已核对当前原文' if p['reviewed'] else '请核对漏字、数字、否定、重复和顺序'))
            self.translation_scope.setText(self.translation_scope.text().replace('本页','图片'))
        else:
            self.page_notice.setText('图片来源尚未保存' if self.selection else '尚未选择图片')
            self.coverage_label.setText('支持PNG / JPG / JPEG；来源64MiB、4000万像素保护。预览和识别均不修改源图。')
        if self.capture_thread:
            self.status.setText('正在封存本次剪贴板图片；请等待确认。')
            for w in (self.start,self.source_button,self.extract_button,self.create,self.save):w.setEnabled(False)

    def choose_file(self):
        if not self.client.can_change_task():return
        path,_=QFileDialog.getOpenFileName(self,'选择图片（原文件只读）','','图片 (*.png *.jpg *.jpeg)')
        if path:self.select_path(path)

    def select_path(self,path,**fields):
        if self.capture_thread or self.ime_blocked() or self.client.state!='ready' or self.client.task_request_pending() or not self.client.can_change_task():return False
        self.selection_backup=self.selection;self.selection_pending=True
        request=self.operate('image.select',source_path=str(path),**fields)
        if not request:self.selection_pending=False
        return request

    def paste(self):
        if (self.capture_thread or self.client.state!='ready' or self.client.task_request_pending() or
                self.ime_blocked() or not self.client.can_change_task()):return False
        mime=QApplication.clipboard().mimeData()
        if mime is None:
            self.status.setText('剪贴板没有图片，当前任务保留。');return False
        encoded=[]
        for mime_type,suffix in (('image/png','.png'),('image/jpeg','.jpg')):
            if mime.hasFormat(mime_type):
                value=bytes(mime.data(mime_type))
                if not 0<len(value)<=MAX_SOURCE_BYTES:
                    self.status.setText('剪贴板图片为空或超过64MiB，当前任务保留。');return False
                encoded.append((value,suffix))
        if len(encoded)>1:
            self.status.setText('剪贴板包含多种图片编码，无法确认表示一致；请复制单一PNG/JPEG图片。当前任务保留。');return False
        image=QImage()
        if not encoded:
            image=QImage(QApplication.clipboard().image())
            if image.isNull():self.status.setText('剪贴板没有可用图片；文本不会替换当前图片。');return False
            if image.width()*image.height()>MAX_SOURCE_PIXELS:
                self.status.setText('剪贴板图片超过4000万像素，当前任务保留。');return False
        capture=ClipboardCapture(self.root,encoded[0] if encoded else None,image,self)
        self.capture_thread=capture
        capture.completed.connect(self.captured);capture.failed.connect(self.capture_failed)
        capture.finished.connect(capture.deleteLater);capture.start();self.update_controls(self.client.state)
        return True

    def captured(self,fields):
        thread=self.capture_thread
        if thread:thread.wait(1000)
        self.capture_thread=None
        if not self.select_path(**{'path':fields.pop('source_path')},**fields):
            self.status.setText('剪贴板来源暂存后尚未选入；当前任务与草稿保留。')
        self.update_controls(self.client.state)

    def capture_failed(self,error):
        thread=self.capture_thread
        if thread:thread.wait(1000)
        self.capture_thread=None;self.status.setText('剪贴板图片未添加：'+error);self.update_controls(self.client.state)

    def create_task(self):
        if self.selection:self.send('image.create',selection_token=self.selection['token'],direction=self.direction.currentData())

    def save_document(self):
        if self.saved and self.saved.get('retry_save_available'):self.send('image.retry_save')
        else:super().save_document()

    def read_page(self):
        super().read_page()
        if self.image_ready:self.image_scroll.hide()

    def render_page(self):
        if not self.saved:
            if self.selection:self.operate('image.render_selection',selection_token=self.selection['token'])
            return
        p=self.current_page()
        self.operate('image.render',page=1,page_revision=p['revision'])

    def show_preview(self,detail,owner):
        relative=Path(detail['image_file']);path=None
        if (len(relative.parts)!=2 or len(relative.parts[0])!=32 or
                any(c not in '0123456789abcdef' for c in relative.parts[0]) or relative.parts[1]!='page.png'):
            raise ValueError('图片预览交接路径无效')
        candidate=self.root/'image-handoff'/relative
        if any(is_link(p) for p in (candidate,*candidate.parents)):raise ValueError('图片预览路径不安全')
        raw=safe_file(candidate,MAX_IMAGE_BYTES);meta=detail['image']
        if (len(raw),sha(raw))!=(meta['byteCount'],meta['sha256']):raise ValueError('图片预览大小/hash不匹配')
        path=candidate
        try:
            image=QImage.fromData(raw)
            if image.isNull() or (image.width(),image.height())!=(meta['pixelWidth'],meta['pixelHeight']) or max(image.width(),image.height())>2400:
                raise ValueError('预览像素不匹配')
            self.canvas.set_image(QPixmap.fromImage(image));self.preview_owner=owner
        finally:
            if path:path.unlink(missing_ok=True);path.parent.rmdir()

    def receive_snapshot(self,snapshot):
        if 'image_selection' in snapshot:
            if snapshot.get('selection_warning'):self.status.setText(snapshot['selection_warning'])
            if snapshot.get('image_selection') and not self.saved:
                self.selection=snapshot['image_selection'];self.update_controls(self.client.state)
                self.status.setText('上次暂存图片已恢复；可查看封存原图或主动保存任务。已保存的任务可从最近记录打开。')
                self.selection_ready.emit()
            return
        old=self.saved['task']['id'] if self.saved else None
        super().receive_snapshot(snapshot)
        if self.image_ready and self.saved and self.saved['task']['id']!=old:
            source_id=self.saved['task']['image']['source']['id']
            if self.preview_owner and self.preview_owner[1]==source_id:
                self.preview_owner=(self.saved['task']['id'],source_id)
            else:self.canvas.clear();self.preview_owner=None
        if self.image_ready and self.saved and snapshot.get('task',{}).get('id')==self.saved['task']['id']:
            task=self.saved['task'];job=task.get('text') or {};parts=job.get('segments',[])
            saved='保存受阻，请保留内容并完整导出' if self.saved.get('save_blocked') else '已保存' if self.saved.get('durable_revision')==task['revision'] else '等待保存确认'
            phase={'preparing':'正在准备翻译','running':'正在翻译','completed':'所获文字翻译完成，请核对语义',
                'stopped':'已停止，可主动继续','partial':'部分翻译未完成','failed':'翻译未完成'}.get(job.get('phase'),'等待主动识别或核对原文')
            self.status.setText(f"{saved} · {phase} · 成功 {sum(p['state']=='completed' for p in parts)} / {len(parts)} 段"+
                ('\n'+self.saved['warning'] if self.saved.get('warning') else '\n'+job['error'] if job.get('error') else ''))

    def receive_event(self,event):
        if event['type']=='ready':self.send('image.selection')
        if event['type']=='task.done' and event['request_id']==self.pending_request and self.pending_kind=='image.retry_save':
            task=self.saved['task'] if self.saved else None
            if task and not self.saved.get('save_blocked') and self.saved.get('durable_revision')==task['revision']:
                for key,body in tuple(self.local_drafts.items()):
                    draft=task['drafts'].get(str(key[1]))
                    if key[0]==task['id'] and draft and draft.get('baseRevision')==key[2] and draft.get('text')==body:
                        self.local_drafts.pop(key,None)
        if event['type']=='operation.finished' and event['request_id']==self.operation_request:
            kind=event['result']['kind'];detail=event['result'].get('detail')
            if kind=='image-selection':
                self.operation_request=None;self.selection_pending=False
                if event['status']=='completed' and detail:
                    self.remember_reading();self.saved=None;self.reading_page=self.reading_owner=None
                    self.pages.blockSignals(True);self.pages.clear();self.pages.blockSignals(False)
                    self.displaying=True;self.input.clear();self.displaying=False
                    self.output.clear();self.success_text='';self.image_revision=None;self.image_scroll.hide()
                    self.selection=detail;self.canvas.clear();self.preview_owner=None
                    try:self.show_preview(detail['preview'],('selection',detail['source']['id']))
                    except (ValueError,OSError,KeyError) as exc:self.status.setText('来源已暂存，预览未显示：'+str(exc))
                    else:self.status.setText('图片已暂存，尚未保存为任务；请点击保存图片来源，然后主动识别。')
                    self.selection_ready.emit()
                else:
                    self.selection=self.selection_backup
                    self.status.setText('图片未选入，当前任务保留：'+(detail.get('error','已取消') if isinstance(detail,dict) else str(detail)))
                self.update_controls(self.client.state);return
            if kind=='image-render':
                self.operation_request=None
                try:
                    if event['status']!='completed' or not detail:raise ValueError(str(detail))
                    task=self.saved['task'];p=self.current_page();source=task['image']['source']
                    if (detail['task_id'],detail['revision'],detail['page_revision'],detail['sourceID'])!=(task['id'],task['revision'],p['revision'],source['id']):
                        raise ValueError('原图回看响应归属已改变')
                    self.show_preview(detail,(task['id'],source['id']))
                    self.status.setText('已核验封存原图并显示预览；未识别、未翻译。')
                    self.open_original_preview()
                except (ValueError,KeyError,OSError,TypeError) as exc:self.status.setText('原图未能回看：'+str(exc))
                self.update_controls(self.client.state);return
            if kind=='image-selection-render':
                self.operation_request=None
                try:
                    if event['status']!='completed' or not detail or not self.selection or detail['sourceID']!=self.selection['source']['id']:
                        raise ValueError(str(detail))
                    self.show_preview(detail,('selection',self.selection['source']['id']))
                except (ValueError,KeyError,TypeError,OSError) as exc:self.status.setText('暂存原图未显示：'+str(exc))
                self.update_controls(self.client.state);return
        super().receive_event(event)

    def open_original_preview(self):
        if self.canvas.original.isNull():return
        dialog=QDialog(self);dialog.setWindowTitle('封存原图 · 有界方向预览');dialog.resize(900,650)
        box=vbox(dialog,(16,16,16,16),10)
        box.addWidget(label('已应用来源方向；透明背景合成白色。预览最长边2400像素，完整来源字节保留在任务中。','tiny',True))
        image=QLabel();image.setPixmap(self.canvas.original);image.resize(self.canvas.original.size())
        scroll=QScrollArea();scroll.setWidget(image);box.addWidget(scroll,1)
        close=Button('关闭',callback=dialog.accept);box.addWidget(close)
        dialog.exec();self.source_button.setFocus()

    def remove_image(self):
        if self.client.state!='ready' or self.client.task_request_pending() or not self.client.can_change_task():return False
        self.send('image.clear_selection')
        if not self.pending_request:return False
        self.saved=None;self.selection=None;self.reading_page=self.reading_owner=None
        self.pages.clear();self.displaying=True;self.input.clear();self.displaying=False
        self.output.clear();self.success_text='';self.canvas.clear();self.preview_owner=None
        self.update_controls(self.client.state);self.back_requested.emit();return True

    def guard_switch(self):
        if self.capture_thread:
            self.status.setText('请等待本次剪贴板来源封存完成。');return False
        return super().guard_switch()

    def prepare_close(self,callback):
        if self.capture_thread:
            self.status.setText('正在封存本次剪贴板来源，完成后请再关闭。');return False
        return super().prepare_close(callback)
