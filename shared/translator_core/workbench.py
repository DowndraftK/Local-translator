"""Mac read/edit/translate sessions using the existing adapters and history store."""
import os
import tempfile
import time
import uuid
from copy import deepcopy
from pathlib import Path
from threading import Event

from . import Cancelled, CoreError
from .contracts import MAX_INPUT_BYTES, encode_snapshot, recipe_binding, sha, strict_json, trim
from .documents import page_range, source_key
from .images import MAX_SOURCE_BYTES, SOURCE_CONTRACT
from .languages import direction_pair
from .ocr_contracts import install_raw
from .planning import split
from .store import atomic_copy, atomic_write, file_identity, safe_file
from .tasks import FOUNDATION_EPOCH
from .translation_routes import verify_direction

MODES = {'text': '文字', 'document': '文档', 'image': '图片'}


def identity():
    return str(uuid.uuid4()).upper()


def target_text(parts):
    return '\n\n'.join(p['result']['translation'] for p in parts if p.get('state') == 'completed')


def completed_pages(task):
    content = task.get('document') or task.get('image')
    if not content:
        return {}
    parts = (task.get('text') or {}).get('segments', [])
    by_page = {}
    for link in content['sources']:
        index = link['segmentID']
        if index < len(parts):
            by_page.setdefault(link.get('page', link.get('unit')), []).append(parts[index])
    return by_page


def record_view(task, warning=None):
    recipe = task['configuration']
    meta = dict(title=task['name'], kind=task['kind'], model=recipe['model'], direction=recipe['direction'],
                digest=recipe.get('servedDigest', recipe.get('modelDigest')),
                profile=recipe.get('promptProfile'), warning=warning, asr=(task.get('audio') or {}).get('asrModel'))
    sections = []
    if task['kind'] == '文字':
        sections.append(dict(caption='', source=task['input'], target=target_text((task.get('text') or {}).get('segments', []))))
    elif task['kind'] == '音频':
        for cue in task['audio']['cues']:
            source = cue.get('effective', cue.get('text', cue.get('source', '')))
            target = (cue.get('result') or {}).get('translation', '')
            sections.append(dict(id=cue['id'], caption=f"{cue['startSample']/16000:.3f} 秒",
                                 source=source, target=target))
    else:
        content = task.get('document') or task['image']
        parts = completed_pages(task)
        for page in content.get('pages',content.get('units',[])):
            number = page['id']
            sections.append(dict(id=number, caption=f'第 {number} 页' if task['kind']=='文档' else '图片',
                                 source=page['text'], target=target_text(parts.get(number,[]))))
    return dict(meta, sections=sections)


class Workbench:
    def __init__(self, tasks):
        self.tasks = tasks
        self.store = tasks.store
        self.storage = tempfile.TemporaryDirectory(prefix='workbench-',dir=self.store.root.parent/'temporary')
        self.root = Path(self.storage.name)
        self.selections = {}
        self.evidence = set()

    def close(self):
        self.storage.cleanup()

    def read(self, identifier, locator):
        task, warning = self.store.read_directory(self.store.directory(identifier), identifier)
        try:
            metadata = self.store.history.metadata(task['id'],task['kind'])
            if metadata['trashed_at'] is not None:
                raise CoreError('请先在回收站恢复此记录，再打开。','task_trashed')
        except (OSError, CoreError) as exc:
            if getattr(exc,'code',None)=='task_trashed':
                raise
            warning = str(exc)
        location = self.store.history.resolve_locator(task,locator) if locator and not warning else None
        return dict(record_view(task,warning), locator=location.get('locator') if location and location['status']=='located' else None)

    def choose(self, mode, path, cancel, commit, *, clipboard=False):
        if mode not in ('document','image') or not isinstance(path,str) or not 0<len(path)<=4096:
            raise CoreError('请选择有效的本地文件。','source_selection')
        old = self.selections.get(mode)
        path = Path(path).absolute()
        suffix = path.suffix.lower()
        extensions = ('.pdf','.txt') if mode=='document' else ('.png','.jpg','.jpeg')
        if suffix not in extensions:
            raise CoreError('请选择 '+ '、'.join(extensions)+' 文件。','source_selection')
        maximum = MAX_SOURCE_BYTES if mode=='image' else MAX_INPUT_BYTES+3 if suffix=='.txt' else 256_000_000
        size, digest = file_identity(path, maximum, cancel)
        if not size:
            raise CoreError('所选文件为空，请更换文件。','empty_source')
        token = identity()
        sealed = self.root/(token+suffix)
        accepted = False
        preview_file = None
        try:
            atomic_copy(path,sealed,size,digest,cancel)
            selected = dict(token=token,path=sealed,originalPath=None if clipboard else str(path),
                            file='剪贴板图片.png' if clipboard else path.name,byteCount=size,
                            fingerprint=digest,suffix=suffix,total=1,ocr={},cache={})
            if mode=='image':
                adapter=self.tasks.image_adapter
                if adapter is None:
                    raise CoreError('本机图片事实适配器不可用。','image_adapter_missing')
                geometry=adapter.metadata(sealed,digest,cancel)
                origin='clipboard-pixels-png' if clipboard else 'file-original'
                selected['source']=dict(contract=SOURCE_CONTRACT,id=token,file=selected['file'],origin=origin,
                    originalPath=selected['originalPath'],fingerprint=digest,byteCount=size,geometry=geometry)
                preview=adapter.preview(sealed,digest,token,cancel)
                preview_file = adapter.root/preview['image_file']
                selected['preview']=self.root/(token+'-preview.png')
                os.replace(preview_file,selected['preview'])
                preview_file.parent.rmdir()
            elif suffix=='.pdf':
                if self.tasks.adapter is None:
                    raise CoreError('本机 PDFKit 适配器不可用。','pdf_adapter_missing')
                selected['total']=self.tasks.adapter.metadata(sealed,digest,cancel)['totalPages']
            else:
                self.tasks._txt(sealed)
            originals,results=self._restore(selected,cancel) if mode=='document' else ({},[])
            commit(lambda: self.selections.__setitem__(mode,selected))
            accepted=True
            if old:
                old['path'].unlink(missing_ok=True)
                if old.get('preview'):
                    old['preview'].unlink(missing_ok=True)
            return dict(token=token,name=selected['file'],total=selected['total'],pdf=suffix=='.pdf',
                        preview=str(selected['preview']) if mode=='image' else None,
                        originals=originals,results=results)
        finally:
            if preview_file and preview_file.exists():
                preview_file.unlink()
                preview_file.parent.rmdir()
            if not accepted:
                sealed.unlink(missing_ok=True)
                (self.root/(token+'-preview.png')).unlink(missing_ok=True)

    def _restore(self, selection, cancel):
        matches = []
        for folder in self.store.root.iterdir():
            if cancel.is_set():
                raise Cancelled()
            if folder.is_symlink() or not folder.is_dir():
                continue
            try:
                header=strict_json(safe_file(folder/'workbench.json',2048))
                if header['fingerprint']==selection['fingerprint']:
                    matches.append((header['updated'],folder))
            except (OSError,CoreError,KeyError,TypeError):
                continue
        originals, displayed = {}, {}
        for _,folder in sorted(matches,reverse=True):
            if cancel.is_set():
                raise Cancelled()
            try:
                task,_=self.store.read_directory(folder,folder.name)
                if self.store.history.metadata(task['id'],'文档')['trashed_at'] is not None:
                    continue
                document=task['document']
                recipe=task['configuration']
                parts=completed_pages(task)
                binding=recipe_binding(recipe)
                changed=task['workbench']['pages']
                cached=selection['cache'].setdefault(binding,{})
                for page in document['pages']:
                    number=page['id']
                    if number not in changed:
                        continue
                    originals.setdefault(number,page['text'])
                    group=parts.get(number,[])
                    cached.setdefault(number,(page,group))
                    displayed.setdefault((recipe['direction'],recipe['model'],number),
                        dict(direction=recipe['direction'],model=recipe['model'],page=number,
                             source=page['text'],target=target_text(group)))
            except (OSError,CoreError,KeyError,TypeError):
                continue
        return originals,list(displayed.values())

    def read_plan(self, request):
        name=request.get('plan')
        if not isinstance(name,str) or len(name)!=41 or not name.endswith('.r13.json'):
            raise CoreError('任务输入文件名无效。','invalid_input')
        try:
            if uuid.UUID(hex=name[:32]).hex != name[:32]:
                raise ValueError()
        except ValueError as exc:
            raise CoreError('任务输入文件名无效。','invalid_input') from exc
        path=self.store.root/'incoming'/name
        try:
            raw=safe_file(path,8*1024*1024)
            if (len(raw),sha(raw))!=(request.get('plan_bytes'),request.get('plan_sha256')):
                raise CoreError('提交原文的内容发生变化，请重新提交。','invalid_input')
            result=strict_json(raw)
            if not isinstance(result,dict) or result.get('mode') not in MODES:
                raise CoreError('任务来源类型无效。','invalid_input')
            direction_pair(result.get('direction'))
            return result
        finally:
            path.unlink(missing_ok=True)

    def _page_source(self, selection, mode, number, direction, cancel):
        path, fingerprint=selection['path'],selection['fingerprint']
        if selection['suffix']=='.txt':
            return self.tasks._txt(path).replace('\r\n','\n').replace('\r','\n')
        adapter=self.tasks.image_adapter if mode=='image' else self.tasks.ocr_adapter
        if adapter is None:
            raise CoreError('Apple Vision 当前不可用。你可以手动录入原文。','ocr_adapter_missing')
        extra={'source_id':selection['token']} if mode=='image' else {}
        result=adapter.recognize(path,fingerprint,number,direction,cancel,**extra)
        selection['ocr'][number]=(direction,result)
        return result['text']

    def _pages(self, plan, cancel, report):
        mode=plan['mode']
        selection=self.selections.get(mode)
        if not selection or plan.get('token')!=selection['token']:
            raise CoreError('文档或图片来源已改变，请重新选择。','selection_mismatch')
        range_=page_range(plan.get('first'),plan.get('last'),selection['total'])
        originals=plan.get('originals',{})
        if not isinstance(originals,dict):
            raise CoreError('有效原文字段无效。','invalid_input')
        pages={}
        for number in range(range_['first'],range_['last']+1):
            if cancel.is_set():
                raise Cancelled()
            value=originals.get(str(number))
            if value is not None and not isinstance(value,str):
                raise CoreError('有效原文字段无效。','invalid_input')
            if value is None or mode=='image' and not trim(value):
                report(dict(message='正在读取 TXT 正文…' if selection['suffix']=='.txt' else f'Apple Vision 正在提取原文 · 第 {number} 页…'))
                value=self._page_source(selection,mode,number,plan['direction'],cancel)
                report(dict(page=number,original=value))
            pages[number]=value
        if sum(len(value.encode()) for value in pages.values())>MAX_INPUT_BYTES:
            raise CoreError('所选范围原文总量超过 1 MiB，请缩小页面范围。','resource_limit')
        return selection,range_,pages

    def _segments(self, text, recipe, engine, cancel):
        parts=split(text,self.tasks.boundaries,cancel)
        for part in parts:
            if cancel.is_set():
                raise Cancelled()
            part.update(state='completed',result=engine.translate(trim(part['source']),recipe))
        return parts

    def execute(self, plan, extract, op, resolve, report, commit):
        mode,direction=plan['mode'],plan['direction']
        cancel=op['run_cancel']
        if mode=='text':
            if extract:
                raise CoreError('文字原文可直接输入。','invalid_input')
            source=plan.get('source')
            if not isinstance(source,str) or not trim(source) or len(source.encode())>MAX_INPUT_BYTES:
                raise CoreError('请输入非空原文，单次提交上限 1 MiB。','invalid_input')
            selection=None
            pages={1:source}
            range_=None
        else:
            selection,range_,pages=self._pages(plan,cancel,report)
            if extract:
                return 'completed',dict(message='原文已提取，可以编辑。' if any(map(trim,pages.values()))
                                        else '系统 OCR 未识别到文字。可手动输入原文，或更换文件。')
        if not any(map(trim,pages.values())) and not (selection and mode=='document' and selection['cache']):
            raise CoreError('没有可翻译的原文。可在原文框手动输入后提交。','zero_translatable_text')
        report(dict(message='正在核验本机 Ollama、模型与翻译方向…'))
        recipe,version,engine,execution=resolve(plan.get('model'),direction,op)
        binding=recipe_binding(recipe)
        cached=selection['cache'].get(binding,{}) if mode=='document' else {}
        changed=[n for n,value in pages.items() if n not in cached or cached[n][0]['text']!=value]
        if not changed:
            return 'completed',dict(message='所选范围原文无变化，已沿用译文。')
        if not any(map(trim,pages.values())) and not any(n in cached for n in changed):
            raise CoreError('没有可翻译的原文。可在原文框手动输入后提交。','zero_translatable_text')
        completed={}
        try:
            if any(trim(pages[n]) for n in changed):
                verify_direction(engine,recipe,version,self.evidence)
            for number in changed:
                if cancel.is_set():
                    raise Cancelled()
                value=pages[number]
                if not trim(value) and number not in cached:
                    continue
                report(dict(message='正在翻译本次提交的原文…' if mode!='document'
                            else f'正在翻译第 {number} 页…'))
                parts=self._segments(value,recipe,engine,cancel) if trim(value) else []
                completed[number]=parts
                if mode=='document':
                    report(dict(page=number,preview=target_text(parts),source=value))
        except Cancelled:
            if not op.get('stop') or op['cancel'].is_set():
                raise
        if op['cancel'].is_set():
            raise Cancelled()
        stopped=bool(op.get('stop'))
        if not completed:
            return ('stopped' if stopped else 'completed'),dict(message='已停止。本次没有已完成页。' if stopped
                else '未获得译文，可补录原文后重试。')
        # Recheck the actual digest before publication. A stopped transport
        # is intentionally closed, so perform the check with the hard-cancel event.
        from .ollama import Ollama
        verification=Ollama(engine.address,cancel=op['cancel'],require_mac_version=True)
        verification.verify(recipe)
        saved_pages={}
        if selection:
            task=self._file_task(selection,mode,range_,pages,cached,completed,recipe,stopped)
            asset=selection['path']
            content=task.get('document') or task['image']
            saved_pages={p['id']:p for p in content.get('pages',content.get('units',[]))}
        else:
            task=self._text_task(source,completed[1],recipe)
            asset=source.encode()
        task['execution']=execution
        self._publish(task,asset,op['cancel'],commit)
        if mode=='document':
            next_cache=dict(cached)
            for n,parts in completed.items():
                next_cache[n]=(saved_pages[n],parts)
            selection['cache'][binding]=next_cache
        results=[dict(page=n,source=pages[n],target=target_text(parts)) for n,parts in completed.items()]
        message=('已停止，保留第 '+ '、'.join(map(str,completed))+' 页。' if stopped else
                 '已翻译第 '+ '、'.join(map(str,completed))+' 页。' if mode=='document' else '翻译完成。')
        if not stopped and mode=='document':
            omitted=[n for n in changed if n not in completed]
            if omitted:
                message+=' 第 '+ '、'.join(map(str,omitted))+' 页无文字，可补录后提交。'
        return ('stopped' if stopped else 'completed'),dict(message=message,results=results,task_id=task['id'],
                     direction=direction,model=recipe['model'])

    def _new_task(self, mode, title, recipe):
        now=time.time()-FOUNDATION_EPOCH
        return dict(id=identity(),kind=MODES[mode],name=title,revision=1,created=now,updated=now,
                    input='',configuration=recipe,drafts={})

    def _text_task(self, source, parts, recipe):
        task=self._new_task('text',trim(source)[:42],recipe)
        digest=sha(source.encode())
        task.update(input=source,asset=dict(originalPath='b02://typed-input',filename=f'source-{digest}.txt',
                                           byteCount=len(source.encode()),fingerprint=digest))
        task['text']=self._text_job(task,source,parts,False)
        return task

    def _text_job(self, task, source, parts, stopped):
        recipe=task['configuration']
        return dict(id=task['id'],source=source,model=recipe['model'],direction=recipe['direction'],
                    phase='stopped' if stopped else 'completed',segments=parts)

    def _file_task(self, selected, mode, range_, pages, cache, completed, recipe, stopped):
        is_image=mode=='image'
        pdf=selected['suffix']=='.pdf'
        ocr=is_image or pdf
        adapter=self.tasks.image_adapter if is_image else self.tasks.ocr_adapter if pdf else None
        task=self._new_task(mode,selected['file'],recipe)
        digest=selected['fingerprint']
        task['asset']=dict(originalPath=selected['originalPath'],filename=f"source-{digest}{selected['suffix']}",
                           byteCount=selected['byteCount'],fingerprint=digest)
        content=dict(id=task['id'],model=recipe['model'],direction=recipe['direction'],file=selected['file'],
            fingerprint=digest,mode='ocr' if ocr else 'text',translationStarted=True,
            phase='已停止并保留已完成页' if stopped else '翻译已提交',
            extractionProfile=adapter.identity if adapter else 'utf8-bom-v1',sources=[],segments=[])
        if ocr:
            content['ocrBackend']=adapter.profile(recipe['direction']) if adapter else {}
            task['formatVersion']=3 if is_image else 2
        container='units' if is_image else 'pages'
        field='unit' if is_image else 'page'
        if is_image:
            content.update(mediaKind='image',source=selected['source'])
        else:
            content.update(range=range_,totalPages=selected['total'],isPDF=pdf,originalPath=selected['originalPath'])
        task['image' if is_image else 'document']=content
        content[container]=[]
        parts=[]
        for number,value in pages.items():
            if number in completed:
                group=completed[number]
                page=self._make_page(selected,content,number,value,recipe['direction'])
            elif number in cache:
                page, group=cache[number]
                page=deepcopy(page)
            else:
                group=[]
                page=self._make_page(selected,content,number,'',recipe['direction'],obtained=False)
            content[container].append(page)
            for paragraph,part in enumerate(group,1):
                index=len(parts)
                part=dict(part,id=index)
                content['sources'].append(dict(segmentID=index,**{field:number},paragraph=paragraph,
                    utf8Start=part['utf8Start'],utf8End=part['utf8End'],revision=page['revision']))
                parts.append(part)
        content['segments']=[{**p,'state':'pending'} for p in parts]
        for part in content['segments']:
            part.pop('result',None)
        task['text']=self._text_job(task,source_key(content),parts,stopped)
        task['workbench']=dict(version=1,pages=list(completed))
        return task

    def _make_page(self, selected, content, number, value, direction, obtained=True):
        mode=content['mode']
        page=dict(id=number,text=value,state=('extracted' if trim(value) else 'noText') if obtained else 'pending',
                  mode=mode,revision=identity(),edited=False,reviewed=False)
        if mode=='ocr':
            page.update(reviewRevision=None,effectiveSource=dict(kind='unobtained',rawRevision=None))
            ocr_direction,result=selected['ocr'].get(number,(None,None))
            if obtained and result and ocr_direction==direction:
                install_raw(page,result,'workbench')
            if obtained and (not page.get('ocr') or page['ocr']['rawText']!=value):
                page['edited']=True
                raw_revision=(page.get('ocr') or {}).get('rawRevision')
                kind=('manual-correction' if raw_revision else 'manual-input') if trim(value) else 'manual-clear'
                page['effectiveSource']=dict(kind=kind,rawRevision=raw_revision)
        return page

    def _publish(self, task, asset, cancel, commit):
        payload=encode_snapshot(task)
        with tempfile.TemporaryDirectory(prefix='.submission-',dir=self.store.root) as path:
            staged=Path(path)
            name=self.store.asset_name(task)
            if isinstance(asset,bytes):
                atomic_write(asset,staged/name)
            else:
                meta=task['asset']
                atomic_copy(asset,staged/name,meta['byteCount'],meta['fingerprint'],cancel)
            atomic_write(payload,staged/'current.json')
            if task['kind']=='文档':
                header=dict(fingerprint=task['asset']['fingerprint'],updated=task['updated'])
                from .resources import atomic_json
                atomic_json(staged/'workbench.json',header)

            def publish():
                with self.store.mutex:
                    target=self.store.directory(task['id'])
                    if target.exists():
                        raise CoreError('提交任务身份已存在。','task_exists')
                    directory = os.open(self.store.root,os.O_RDONLY)
                    try:
                        os.rename(staged,target)
                        try:
                            os.fsync(directory)
                        except OSError:
                            os.rename(target,staged)
                            raise
                    finally:
                        os.close(directory)
                    self.store.high_water[task['id']]=task['revision']
                    self.store.expected_snapshots[task['id']]=sha(payload)
                    self.store.history_epoch+=1
            commit(publish)
