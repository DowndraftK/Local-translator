"""Image acquisition uses the same TaskStore, revision owner and TextTasks run loop."""
import re
import json
import time
import uuid
from copy import deepcopy
from pathlib import Path
from . import CoreError
from .contracts import default_recipe, validate_task, strict_json
from .document_tasks import DocumentTasks
from .images import SOURCE_CONTRACT, MAX_SOURCE_BYTES, content, units, validate_source
from .store import file_identity, atomic_copy, is_link, safe_file
from .tasks import FOUNDATION_EPOCH


class ImageTasks(DocumentTasks):
    def __init__(self, *args, image_adapter=None, **kwargs):
        from .ollama import Ollama
        kwargs.setdefault('engine_factory',lambda cancel:Ollama('http://127.0.0.1:1',cancel=cancel))
        self._image_save_failure=None
        super().__init__(*args, **kwargs)
        self.image_adapter=image_adapter;self.image_selection=None
        self.selection_warning=None
        journal=self.store.root/'image-selection.json'
        if journal.exists() or is_link(journal):
            try:
                saved=strict_json(safe_file(journal,65536));source=saved['source'];validate_source(source)
                if saved.get('contract')!='image-selection-v1' or not re.fullmatch(r'[0-9a-f]{32}',saved.get('token','')):
                    raise CoreError('暂存来源版本或身份不兼容。','image_selection')
                name=saved.get('filename','')
                if not re.fullmatch(r'[0-9a-f]{32}\.(png|jpg|jpeg)',name):raise CoreError('暂存路径不安全。','unsafe_path')
                path=self.store.root/'incoming'/name
                if any(is_link(p) for p in (path,*path.parents)) or file_identity(path,MAX_SOURCE_BYTES)!=(source['byteCount'],source['fingerprint']):
                    raise CoreError('暂存来源缺失或大小/hash不匹配。','image_source_mismatch')
                self.image_selection=dict(token=saved['token'],path=str(path),source=source,preview=None,restored=True)
            except (CoreError,OSError,ValueError,KeyError,TypeError) as exc:
                self.selection_warning='暂存图片来源未恢复；原记录保留：'+str(exc)

    def snapshot(self):
        result=super().snapshot()
        result['retry_save_available']=bool(self._image_save_failure and self.save_blocked and self.task and self.task['kind']=='图片')
        return result

    def open(self,identity):
        self._image_save_failure=None
        return super().open(identity)

    def _persist(self,changed):
        if not self.task or self.task['kind']!='图片':return super()._persist(changed)
        current=self.store.directory(self.task['id'])/'current.json'
        base=file_identity(current) if current.is_file() and not is_link(current) else None
        try:
            super()._persist(changed);self._image_save_failure=None
        except CoreError:
            self._image_save_failure=dict(task_id=self.task['id'],base=base)
            # The first shared failure snapshot precedes this retry receipt.
            changed(self.snapshot())
            raise

    def retry_image_save(self):
        from .contracts import decode_snapshot
        with self.lock:
            self.idle();receipt=self._image_save_failure
            if not self.task or self.task['kind']!='图片' or not self.save_blocked or not receipt or receipt['task_id']!=self.task['id']:
                raise CoreError('没有可确认归属的图片保存失败；请保留内容供导出。','save_blocked')
            current=self.store.directory(self.task['id'])/'current.json'
            decode_snapshot(safe_file(current))
            if receipt['base'] is None or file_identity(current)!=receipt['base']:
                raise CoreError('磁盘快照已经变化，不能覆盖；请完整导出后重新打开核对。','save_blocked')
            self.store.verify_asset(self.task,self.store.directory(self.task['id']))
            self.save_blocked=False;self.warning=None
            return self.save()

    def select_image(self, source, generation, cancel, origin='file-original', fingerprint=None, byte_count=None):
        with self.lock:
            self.idle();self.active=(generation,cancel)
        sealed=None;accepted=False;incoming=None
        try:
            if self.image_adapter is None: raise CoreError('本机图片适配器不可用。','image_adapter_missing')
            if origin=='file-original':
                if not isinstance(source,str) or len(source)>4096: raise CoreError('来源路径无效。','unsafe_path')
                path=Path(source).absolute();original=str(path);name=path.name
            elif origin in ('clipboard-encoded','clipboard-pixels-png'):
                if not isinstance(source,str) or not re.fullmatch(r'clip-[0-9a-f]{32}\.(png|jpg|jpeg)',source):
                    raise CoreError('剪贴板交接路径无效。','unsafe_path')
                path=self.store.root/'incoming'/source;incoming=path;original=None;name='剪贴板图片'+path.suffix
            else: raise CoreError('图片来源方式无效。','image_source_mismatch')
            if any(is_link(p) for p in (path,*path.parents)):
                raise CoreError('图片来源不能经过符号链接。','unsafe_path')
            suffix=path.suffix.lower()
            if suffix not in ('.png','.jpg','.jpeg'): raise CoreError('请选择PNG / JPG / JPEG图片。','image_format')
            count,digest=file_identity(path,MAX_SOURCE_BYTES,cancel)
            if count==0: raise CoreError('图片来源为空。','empty_source')
            if incoming and (count,digest)!=(byte_count,fingerprint): raise CoreError('剪贴板交接大小/hash不匹配。','image_source_mismatch')
            token=uuid.uuid4().hex;source_id=str(uuid.uuid4()).upper()
            sealed=self.store.root/'incoming'/(token+suffix)
            atomic_copy(path,sealed,count,digest,cancel)
            geometry=self.image_adapter.metadata(sealed,digest,cancel)
            if (suffix=='.png')!=(geometry['format']=='PNG'): raise CoreError('后缀与实际图片编码不符。','image_format')
            if origin=='clipboard-pixels-png' and geometry['format']!='PNG': raise CoreError('规范像素编码必须为PNG。','image_format')
            preview=self.image_adapter.preview(sealed,digest,source_id,cancel)
            identity=dict(contract=SOURCE_CONTRACT,id=source_id,file=name,originalPath=original,origin=origin,
                fingerprint=digest,byteCount=count,geometry=geometry)
            with self.lock:
                self._current(generation)
                old=self.image_selection
                if self.selection_warning:
                    journal=self.store.root/'image-selection.json'
                    if journal.is_file() and not is_link(journal):
                        self.store.write(safe_file(journal,65536),self.store.root/'incoming'/('unreadable-selection-'+uuid.uuid4().hex+'.json'))
                self.store.write(json.dumps(dict(contract='image-selection-v1',token=token,
                    filename=sealed.name,source=identity),ensure_ascii=False).encode(),self.store.root/'image-selection.json')
                self.image_selection=dict(token=token,path=str(sealed),source=identity,preview=preview)
                self.selection_warning=None
                if old:
                    previous=Path(old['path'])
                    if previous.parent==sealed.parent and not is_link(previous):previous.unlink(missing_ok=True)
                accepted=True
                return deepcopy({k:v for k,v in self.image_selection.items() if k!='path'})
        finally:
            if sealed is not None and not accepted and not is_link(sealed): sealed.unlink(missing_ok=True)
            if incoming is not None and not is_link(incoming):incoming.unlink(missing_ok=True)
            with self.lock:self.active=None

    def create_image(self, token, direction):
        with self.lock:
            self.idle();s=self.image_selection
            if not s or s['token']!=token:raise CoreError('图片选择身份已改变。','selection_mismatch')
            if self.image_adapter is None:raise CoreError('图片适配器不可用。','image_adapter_missing')
            source=deepcopy(s['source']);path=Path(s['path'])
            if file_identity(path,MAX_SOURCE_BYTES)!=(source['byteCount'],source['fingerprint']):
                raise CoreError('图片暂存来源已变化。','image_source_mismatch')
            recipe=self.new_recipe(direction);recipe['splitterVersion']=self.boundaries.splitter_version
            identity=str(uuid.uuid4()).upper();now=time.time()-FOUNDATION_EPOCH
            slot=dict(id=1,text='',state='pending',mode='ocr',revision=str(uuid.uuid4()).upper(),edited=False,
                reviewed=False,reviewRevision=None,effectiveSource=dict(kind='unobtained',rawRevision=None))
            image=dict(id=identity,mediaKind='image',source=source,file=source['file'],fingerprint=source['fingerprint'],
                model=recipe['model'],direction=direction,mode='ocr',units=[slot],sources=[],segments=[],
                translationStarted=False,phase='图片来源已封存，等待主动识别',
                extractionProfile=self.image_adapter.identity,ocrBackend=self.image_adapter.profile(direction))
            task=dict(id=identity,formatVersion=3,revision=1,created=now,updated=now,name=source['file'],kind='图片',input='',
                configuration=recipe,image=image,drafts={},asset=dict(originalPath=source['originalPath'],
                filename='source-'+source['fingerprint']+('.png' if source['geometry']['format']=='PNG' else '.jpg'),
                byteCount=source['byteCount'],fingerprint=source['fingerprint']))
            self.apply_new_execution(task)
            self.store.save(task,path)
            self.task=task;self.warning=None;self.durable_revision=1;self.save_blocked=False
            self._image_save_failure=None
            return self.snapshot()

    def selected_image(self):
        with self.lock:
            return dict(image_selection=deepcopy({k:v for k,v in self.image_selection.items() if k!='path'}) if self.image_selection else None,
                selection_warning=self.selection_warning)

    def clear_image_selection(self):
        with self.lock:
            self.idle();journal=self.store.root/'image-selection.json'
            if is_link(journal):raise CoreError('暂存记录路径不安全。','unsafe_path')
            journal.unlink(missing_ok=True)
            if self.image_selection:
                path=Path(self.image_selection['path'])
                if path.parent==self.store.root/'incoming' and not is_link(path):path.unlink(missing_ok=True)
            self.image_selection=None;self.selection_warning=None
            return self.selected_image()

    def render_selection(self,token,cancel):
        with self.lock:
            self.idle();selected=self.image_selection
            if not selected or selected['token']!=token:raise CoreError('暂存图片归属已改变。','selection_mismatch')
            source=selected['source']
            if self.image_adapter is None:raise CoreError('图片适配器不可用。','image_adapter_missing')
            return self.image_adapter.preview(selected['path'],source['fingerprint'],source['id'],cancel)

    def _ocr_for_current(self):
        if not self.task or self.task['kind']!='图片':return super()._ocr_for_current()
        adapter=self.image_adapter;identity=self.task['image']['source']['id']
        if adapter is None:return None
        # Bind direct-image recognition to the immutable source UUID while sharing
        # DocumentTasks' one extraction loop and revision/commit checks.
        class Bound:
            def __getattr__(self,name):return getattr(adapter,name)
            def recognize(self,source,fingerprint,unit,direction,cancel):
                return adapter.recognize(source,fingerprint,unit,direction,cancel,source_id=identity)
        return Bound()

    def render_image(self,cancel):
        with self.lock:
            self.idle()
            if not self.task or self.task['kind']!='图片':raise CoreError('当前不是图片任务。','task_kind_mismatch')
            if self.image_adapter is None:raise CoreError('图片适配器不可用。','image_adapter_missing')
            source=self.store.verify_asset(self.task,self.store.directory(self.task['id']))
            image=self.task['image']
            return self.image_adapter.preview(source,image['fingerprint'],image['source']['id'],cancel)

    def fork_document(self,fresh=False,direction=None):
        if not self.task or self.task['kind']!='图片':return super().fork_document(fresh,direction)
        with self.lock:
            self.idle();old=self.task;d=content(old)
            if self.save_blocked:raise CoreError('保存受阻，保留来源与内容供导出。','save_blocked')
            if old['drafts']:raise CoreError('先应用或明确放弃草稿再另建。','unapplied_draft')
            if type(fresh) is not bool:raise CoreError('新任务目的无效。')
            if self.image_adapter is None:raise CoreError('图片适配器不可用。','image_adapter_missing')
            source=self.store.verify_asset(old,self.store.directory(old['id']))
            direction=direction or d['direction'];recipe=self.new_recipe(direction);recipe['splitterVersion']=self.boundaries.splitter_version
            new=deepcopy(old);new.pop('text',None);identity=str(uuid.uuid4()).upper();now=time.time()-FOUNDATION_EPOCH
            new.update(id=identity,revision=1,created=now,updated=now,configuration=recipe,drafts={},
                parent=dict(id=old['id'],revision=old['revision'],purpose='fresh-ocr' if fresh else 'review-copy'))
            nd=new['image'];nd.update(id=identity,model=recipe['model'],direction=direction,segments=[],sources=[],
                translationStarted=False,extractionProfile=self.image_adapter.identity,
                ocrBackend=self.image_adapter.profile(direction),phase='明确另建的图片任务，旧任务和译文保留')
            for p in units(nd):
                p.update(reviewed=False,reviewRevision=None)
                if fresh:
                    p.pop('ocr',None);p.pop('issue',None)
                    p.update(text='',state='pending',edited=False,revision=str(uuid.uuid4()).upper(),
                        effectiveSource=dict(kind='unobtained',rawRevision=None))
                elif p.get('ocr'):
                    p['effectiveSource']=dict(kind='inherited-ocr',rawRevision=p['ocr']['rawRevision'],parent=old['id'])
            self.apply_new_execution(new)
            validate_task(new);self.store.save(new,source)
            self.task=new;self.warning=None;self.durable_revision=1;self.save_blocked=False
            return self.snapshot()
