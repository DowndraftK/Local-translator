"""Document acquisition feeds the existing TextTasks scheduler, never a second one."""
import time
import uuid
from pathlib import Path
from copy import deepcopy
from . import Cancelled, CoreError
from .contracts import MAX_INPUT_BYTES, default_recipe, trim, validate_task
from .documents import page_range, source_key, prepare_document, coverage
from .store import MAX_DOCUMENT_BYTES, atomic_copy, file_identity, safe_file, is_link
from .tasks import TextTasks, FOUNDATION_EPOCH


class DocumentTasks(TextTasks):
    def __init__(self, store, boundaries, adapter, ocr_adapter=None, **kwargs):
        super().__init__(store, boundaries, **kwargs)
        self.adapter = adapter; self.selection = None
        self.ocr_adapter=ocr_adapter

    def select_document(self, source, generation, cancel):
        with self.lock:
            self.idle(); self.active = (generation, cancel)
        sealed = None; accepted = False
        try:
            path = Path(source).absolute()
            suffix = path.suffix.lower()
            if suffix not in (".pdf", ".txt"): raise CoreError("请选择PDF或UTF-8 TXT。")
            count, fingerprint = file_identity(path, cancel=cancel)
            if count == 0: raise CoreError("来源文件为空。", "empty_source")
            token = uuid.uuid4().hex
            sealed = self.store.root / "incoming" / (token + suffix)
            atomic_copy(path, sealed, count, fingerprint, cancel)
            if suffix == ".pdf":
                if self.adapter is None: raise CoreError("本地PDF事实适配器缺失；不会自动安装。", "pdf_adapter_missing")
                metadata = self.adapter.metadata(sealed, fingerprint, cancel)
                total = metadata["totalPages"]; backend = self.adapter.identity
            else:
                self._txt(sealed)
                total = 1; backend = "utf8-bom-v1"
            with self.lock:
                self._current(generation)
                previous = self.selection
                self.selection = dict(token=token, file=path.name, path=str(sealed), originalPath=str(path),
                                      byteCount=count, fingerprint=fingerprint, totalPages=total,
                                      isPDF=suffix == ".pdf", backend=backend)
                if previous:
                    old = Path(previous["path"])
                    if old.parent == sealed.parent and not is_link(old): old.unlink(missing_ok=True)
                accepted = True
                return deepcopy({k: v for k, v in self.selection.items() if k != "path"})
        finally:
            if sealed is not None and not accepted and not is_link(sealed): sealed.unlink(missing_ok=True)
            with self.lock: self.active = None

    @staticmethod
    def _txt(path):
        raw = safe_file(path, MAX_INPUT_BYTES + 3)
        try: text = raw.decode("utf-8-sig")
        except UnicodeError as exc: raise CoreError("TXT仅支持UTF-8及UTF-8 BOM；其他编码请显式转换后另选。", "txt_encoding") from exc
        if "\0" in text: raise CoreError("TXT含NUL，可能是UTF-16或二进制；仅支持UTF-8。", "txt_encoding")
        if len(text.encode()) > MAX_INPUT_BYTES: raise CoreError("TXT全文超出1MiB UTF-8文字保护。", "resource_limit")
        return text

    def create_document(self, token, first, last, direction, mode='text'):
        with self.lock:
            self.idle(); s = self.selection
            if not s or s["token"] != token: raise CoreError("所选文件身份已失效，请重新选择。", "selection_mismatch")
            r = page_range(first, last, s["totalPages"])
            if mode not in ('text','ocr') or mode=='ocr' and not s['isPDF']:raise CoreError('OCR仅支持明确选择的PDF整页范围。')
            if mode=='ocr' and self.ocr_adapter is None:raise CoreError('本地OCR不可用；不会自动补包。','ocr_adapter_missing')
            if not s["isPDF"] and r != {"first": 1, "last": 1}: raise CoreError("TXT只能选择全文。")
            count, fingerprint = file_identity(s["path"])
            if (count, fingerprint) != (s["byteCount"], s["fingerprint"]): raise CoreError("暂存来源字节已变化。", "source_mismatch")
            recipe = default_recipe(direction); recipe["splitterVersion"] = self.boundaries.splitter_version
            identity = str(uuid.uuid4()).upper(); now = time.time() - FOUNDATION_EPOCH
            doc = dict(id=identity, file=s["file"], fingerprint=fingerprint, originalPath=s["originalPath"],
                       extractionProfile=s["backend"], totalPages=s["totalPages"], isPDF=s["isPDF"], range=r,
                       model=recipe["model"], direction=direction, pages=[dict(id=n, text="", state="pending", mode="text",
                       revision=str(uuid.uuid4()).upper(), edited=False, reviewed=False) for n in range(first, last + 1)],
                       sources=[], segments=[], mode="text", translationStarted=False, phase="已封存来源，等待主动提取")
            task = dict(id=identity, revision=1, created=now, updated=now, name=s["file"], kind="文档", input="",
                        configuration=recipe, document=doc, drafts={}, asset=dict(originalPath=s["originalPath"],
                        filename=f"source-{fingerprint}{'.pdf' if s['isPDF'] else '.txt'}", byteCount=count, fingerprint=fingerprint))
            if mode=='ocr':
                task['formatVersion']=2;doc['mode']='ocr';doc['extractionProfile']=self.ocr_adapter.identity
                doc['ocrBackend']=self.ocr_adapter.profile(direction);doc['phase']='显式OCR任务已保存，等待主动识别'
                for p in doc['pages']:p.update(mode='ocr',effectiveSource=dict(kind='unobtained',rawRevision=None),reviewRevision=None)
            self.store.save(task, Path(s["path"]))
            self.task, self.warning, self.durable_revision, self.save_blocked = task, None, 1, False
            return self.snapshot()

    def _document(self):
        if not self.task or self.task["kind"] != "文档": raise CoreError("请先打开文档任务。", "task_kind_mismatch")
        return self.task["document"]

    def _source_key(self):
        return source_key(self.task["document"]) if self.task["kind"] == "文档" else super()._source_key()

    def _before_run(self):
        if self.task["kind"] != "文档": return
        d = self._document()
        if self.task["drafts"]: raise CoreError("尚有未应用草稿，请先保存校正或显式放弃草稿。", "unapplied_draft")
        if any(p["state"] in ("pending", "recognizing", "stopped") for p in d["pages"]):
            raise CoreError("尚有未处理页，请主动继续提取后再翻译。", "extraction_incomplete")
        if not any(trim(p["text"]) for p in d["pages"]): raise CoreError("所选范围没有可翻译文字。", "zero_translatable_text")

    def _plan(self, cancel):
        if self.task["kind"] != "文档": return super()._plan(cancel)
        with self.lock:
            d = self._document()
            plan = prepare_document(d, self.boundaries, cancel)
            d["translationStarted"] = True
            self.task["text"]["segments"] = deepcopy(plan)
            return deepcopy(plan)

    def extract(self, generation, cancel, changed, mode='text'):
        with self.lock:
            self.idle(); d = self._document()
            if self.save_blocked: raise CoreError("保存/源副本受阻；仍可阅读和导出。", "save_blocked")
            source = self.store.verify_asset(self.task, self.store.directory(self.task["id"]))
            missing = [p for p in d["pages"] if p["state"] in ("pending", "recognizing", "stopped", "failed") and not p["edited"]]
            if mode=='ocr':missing=[p for p in missing if p.get('ocr') is None]
            if not missing: return "completed"
            if d["translationStarted"] or d["segments"] or self.task.get("text"):
                raise CoreError("已有来源计划已冻结；补页或更改原文须显式另建任务。", "source_frozen")
            if d["isPDF"] and self.adapter is None: raise CoreError("本地PDF适配器不可用；可读已获得内容/导出。", "pdf_adapter_missing")
            if mode=='ocr':
                if self.task.get('formatVersion')!=2 or self.ocr_adapter is None:raise CoreError('旧OCR后端不兼容；请显式另建任务。','ocr_adapter_mismatch')
                if self.task['drafts']:raise CoreError('请先应用或明确放弃编辑草稿。','unapplied_draft')
                if d['ocrBackend']!=self.ocr_adapter.profile(d['direction']):raise CoreError('OCR资源/规则身份已改变，请另建任务。','ocr_adapter_mismatch')
                self.ocr_adapter.require(d['direction'])
            expected = self.ocr_adapter.identity if mode=='ocr' else self.adapter.identity if d["isPDF"] else "utf8-bom-v1"
            if d.get("extractionProfile") != expected or d["mode"] != mode:
                raise CoreError("提取后端身份不兼容；可阅读核对/导出已有内容或另建任务。", "extraction_adapter_mismatch")
            self.active = (generation, cancel); d["phase"] = "正在逐页整页OCR；可取消当前自有事实进程" if mode=='ocr' else "正在逐页提取文字层"
        try:
            for p in missing:
                with self.lock:
                    self._current(generation)
                    p["state"] = "recognizing"; p.pop("issue", None); self._persist(changed)
                try:
                    result = self.ocr_adapter.recognize(source,d['fingerprint'],p['id'],d['direction'],cancel) if mode=='ocr' else self.adapter.page(source, d["fingerprint"], p["id"], cancel) if d["isPDF"] else dict(text=self._txt(source), page=1)
                    with self.lock:
                        self._current(generation)
                        if result.get("page") != p["id"] or not isinstance(result.get("text"), str):
                            raise CoreError("提取响应页身份不匹配。", "page_identity_mismatch")
                        if sum(len(q["text"].encode()) for q in d["pages"] if q is not p) + len(result["text"].encode()) > MAX_INPUT_BYTES:
                            raise CoreError("累计原文超出1MiB UTF-8保护，当前页未接收；请另建较小范围任务。", "resource_limit")
                        candidate = deepcopy(p)
                        candidate["text"] = result["text"]; candidate["state"] = "extracted" if trim(result["text"]) else "noText"
                        if mode=='ocr':
                            from .ocr_contracts import install_raw
                            install_raw(candidate,result,generation)
                            candidate['issue']='识别响应已完整结束；请人工核对漏字、重复、数字、否定和顺序；置信度不是准确率。'
                        if result.get("label") is not None: candidate["label"] = result["label"]
                        if candidate["state"] == "noText": candidate["issue"] = "整页OCR响应完整，但没有识别文字；请核对原页。" if mode=='ocr' else "未获得文字层；请查看原页，可主动另建整页OCR任务。"
                        # Validate the whole bounded raw/effective manifest before accepting
                        # a page. Invalid or oversized raw facts never poison in-memory state.
                        proposed = deepcopy(self.task)
                        proposed['document']['pages'][d['pages'].index(p)] = candidate
                        validate_task(proposed)
                        self._current(generation)
                        p.clear(); p.update(candidate)
                        self._persist(changed)
                except CoreError as exc:
                    with self.lock:
                        self._current(generation); p["state"] = "failed"; p["issue"] = str(exc)
                        if not self.save_blocked: self._persist(changed)
                    if exc.code in ("resource_limit", "source_mismatch", "unsafe_path", "save_failed"): raise
            with self.lock:
                self._current(generation)
                complete = all(p["state"] in ("extracted", "noText") for p in d["pages"])
                d["phase"] = ("所选范围OCR响应完成，等待人工核对" if mode=='ocr' else "所选范围提取完成") if complete else "所选范围存在问题页"
                d.pop('issue',None)
                if not any(trim(p["text"]) for p in d["pages"]): d["issue"] = "所选范围没有可翻译文字；请核对原页。"
                self._persist(changed); return "completed" if complete else "partial"
        except (Cancelled, CoreError) as exc:
            with self.lock:
                for p in d["pages"]:
                    if p["state"] in ("pending", "recognizing"): p["state"] = "stopped"
                d["phase"] = "已停止页面处理，等待主动继续" if isinstance(exc, Cancelled) else "页面处理未完成"
                if not isinstance(exc, Cancelled): d["issue"] = str(exc)
                if not self.save_blocked: self._persist(changed)
            return "cancelled" if isinstance(exc, Cancelled) else "failed"
        finally:
            with self.lock: self.active = None

    def render(self, number, cancel):
        with self.lock:
            self.idle(); d = self._document()
            if not d["isPDF"]: raise CoreError("TXT来源为全文，没有PDF物理页。")
            if self.adapter is None: raise CoreError("本地PDF渲染适配器缺失。", "pdf_adapter_missing")
            if type(number) is not int or not 1 <= number <= d["totalPages"]: raise CoreError("物理页超出来源文件。")
            source = self.store.verify_asset(self.task, self.store.directory(self.task["id"]))
            fingerprint = d["fingerprint"]
        return self.adapter.render(source, fingerprint, number, cancel)

    def _editable_page(self,number,revision,frozen=True):
        self.idle();d=self._document()
        if self.save_blocked:raise CoreError('保存受阻，保留原任务，可阅读/导出。','save_blocked')
        if self.task.get('formatVersion')!=2:raise CoreError('旧schema1只读核对；编辑请明确另建核对任务。','ocr_schema_mismatch')
        if frozen and (d['translationStarted'] or d['segments'] or self.task.get('text')):
            raise CoreError('来源已冻结；请另建核对任务，保留旧译文。','source_frozen')
        p=next((p for p in d['pages'] if p['id']==number),None)
        if p is None or p['revision']!=revision:raise CoreError('请求不属于当前有效页修订。','page_revision_mismatch')
        return p

    def draft(self,number,revision,text):
        with self.lock:
            p=self._editable_page(number,revision)
            if not isinstance(text,str) or len(text.encode())>MAX_INPUT_BYTES:raise CoreError('草稿超出1MiB保护。','resource_limit')
            others=sum(len(v['text'].encode()) for k,v in self.task['drafts'].items() if k!=str(number))
            if others+len(text.encode())>MAX_INPUT_BYTES:raise CoreError('草稿累计超出1MiB保护。','resource_limit')
            self.task['drafts'][str(number)]=dict(id=str(uuid.uuid4()).upper(),baseRevision=p['revision'],text=text)
            return self.save()

    def apply_draft(self,number,revision,discard=False):
        with self.lock:
            p=self._editable_page(number,revision);key=str(number);draft=self.task['drafts'].get(key)
            if draft is None or draft['baseRevision']!=revision:raise CoreError('没有属于当前修订的草稿。','draft_revision_mismatch')
            if not discard:
                if sum(len(q['text'].encode()) for q in self._document()['pages'] if q is not p)+len(draft['text'].encode())>MAX_INPUT_BYTES:
                    raise CoreError('有效原文累计超出1MiB保护。','resource_limit')
                old=p['revision'];p['text']=draft['text'];p['revision']=str(uuid.uuid4()).upper();p['edited']=True
                p['state']='extracted' if trim(p['text']) else 'noText';p['reviewed']=False;p['reviewRevision']=None
                raw=p.get('ocr');kind='manual-clear' if not trim(p['text']) else 'manual-correction' if raw else 'manual-input'
                p['effectiveSource']=dict(kind=kind,rawRevision=raw['rawRevision'] if raw else None,previousRevision=old,draftID=draft['id'])
                p['issue']='人工有效原文修订已保存；原始OCR保留，请重新核对。'
            del self.task['drafts'][key]
            return self.save()

    def review(self,number,revision,value):
        with self.lock:
            p=self._editable_page(number,revision,frozen=False)
            if type(value) is not bool:raise CoreError('人工核对状态无效。')
            if str(number) in self.task['drafts']:raise CoreError('尚有未应用草稿，请先保存校正。','unapplied_draft')
            p['reviewed']=value;p['reviewRevision']=revision if value else None
            return self.save()

    def fork_document(self,fresh=False,direction=None):
        with self.lock:
            self.idle();old=self.task;d=self._document();source=self.store.verify_asset(old,self.store.directory(old['id']))
            if self.save_blocked:raise CoreError('此任务只读或保存受阻；请显式从完整来源另建任务。','save_blocked')
            if old['drafts']:raise CoreError('先应用或明确放弃草稿，再另建任务。','unapplied_draft')
            if type(fresh) is not bool:raise CoreError('新任务目的无效。')
            if self.ocr_adapter is None:raise CoreError('当前OCR适配器不可用。','ocr_adapter_missing')
            direction=direction or d['direction'];identity=str(uuid.uuid4()).upper();now=time.time()-FOUNDATION_EPOCH
            recipe=default_recipe(direction);recipe['splitterVersion']=self.boundaries.splitter_version
            new=deepcopy(old);new.pop('text',None);new['formatVersion']=2;new['id']=identity;new['revision']=1
            new.update(created=now,updated=now,configuration=recipe,drafts={},parent=dict(id=old['id'],revision=old['revision'],purpose='fresh-ocr' if fresh else 'review-copy'))
            nd=new['document'];nd.update(id=identity,mode='ocr',model=recipe['model'],direction=direction,segments=[],sources=[],translationStarted=False,extractionProfile=self.ocr_adapter.identity,ocrBackend=self.ocr_adapter.profile(direction),phase='明确另建的OCR/核对任务，旧任务与译文保留')
            for p in nd['pages']:
                if fresh:
                    p.pop('ocr',None);p.pop('issue',None);p.update(text='',state='pending',edited=False,reviewed=False,revision=str(uuid.uuid4()).upper())
                    p['effectiveSource']=dict(kind='unobtained',rawRevision=None)
                else:
                    raw=p.get('ocr')
                    if raw:
                        if old.get('formatVersion')!=2:
                            original=deepcopy(raw)
                            raw.update(rawRevision=str(uuid.uuid4()).upper(),rawText='\n'.join(o['text'] for o in raw['observations']),
                                       backend=d.get('extractionProfile','legacy-identity-missing'),operation='inherited:'+old['id'],
                                       recognitionComplete=True,scale=None,orientation='up',coordinateSystem='normalized-bottom-left',
                                       textAngle=None,evidenceOrigin='schema1-preserved',legacyEvidence=original)
                        p['effectiveSource']=dict(kind='inherited-ocr',rawRevision=raw['rawRevision'],parent=old['id'])
                    else:
                        p['effectiveSource']=dict(kind='manual-input' if trim(p['text']) else 'unobtained',rawRevision=None)
                        if trim(p['text']):p['edited']=True
                    p['reviewed']=False
                p['mode']='ocr';p['reviewRevision']=None
            self.store.save(new,source);self.task=new;self.save_blocked=False;self.warning=None;self.durable_revision=1
            return self.snapshot()
