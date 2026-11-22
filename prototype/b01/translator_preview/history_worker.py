"""One owned history reader, with one replaceable pending query per view."""
import threading
from collections import OrderedDict
from translator_core import CoreError, Cancelled
from translator_core.history import QUERY_VERSION

class HistoryWorker:
    def __init__(self, worker):
        self.worker=worker; self.repository=worker.tasks.store.history if worker.tasks else None
        if self.repository:self.repository.session_id=worker.session_id
        self.condition=threading.Condition();self.pending=OrderedDict();self.active=None;self.closed=False
        self.thread=threading.Thread(target=self.run,name='history-reader',daemon=False);self.thread.start()

    def description(self):
        return self.repository.description() if self.repository else dict(contract=QUERY_VERSION,root_id=None)

    def handle(self, req):
        try:
            if not self.repository:raise CoreError('请指定独立任务根。','preview_root_required')
            kind=req['type']
            if kind=='history.query':
                query=req.get('query',{});owner=query.get('owner',{});scope=owner.get('scope')
                if scope not in ('history','recent'):raise CoreError('历史入口无效。','history_owner')
                with self.condition:
                    if self.closed:raise CoreError('历史查询正在关闭。','closing')
                    if self.active and self.active[1].get('owner',{}).get('scope')==scope:self.active[2].set()
                    previous=self.pending.pop(scope,None)
                    if previous:self.terminal_cancel(previous[0],previous[1])
                    self.pending[scope]=(req,query,threading.Event());self.condition.notify()
            elif kind=='history.cancel':
                with self.condition:
                    identity=req.get('query_id')
                    if self.active and self.active[1].get('owner',{}).get('query_id')==identity:self.active[2].set()
                    for scope,item in tuple(self.pending.items()):
                        if item[1].get('owner',{}).get('query_id')==identity:
                            self.pending.pop(scope);self.terminal_cancel(item[0],item[1])
                self.worker.emit('history.cancelled',req,query_id=identity)
            elif kind=='history.update':
                tasks=self.worker.tasks
                with self.worker.operation_lock:
                    if self.worker.operation:raise CoreError('当前操作运行或正在停止，请等待安全结束后管理记录。','busy')
                    with tasks.lock:
                        snapshot=tasks.snapshot();task=snapshot['task']
                        if task and (snapshot['save_blocked'] or snapshot['durable_revision']!=task['revision'] or task.get('drafts')):
                            raise CoreError('当前任务或文档草稿尚未安全保存，请先完成保存保护。','save_blocked')
                        result=self.repository.update(req.get('task_id'),root_id=req.get('root_id'),
                            session_id=req.get('writer_session'),task_revision=req.get('task_revision'),
                            metadata_revision=req.get('metadata_revision'),changes=req.get('changes'))
                self.worker.emit('history.updated',req,result=result)
            else:raise CoreError('不支持此历史操作。','unsupported_operation')
        except (CoreError,OSError,TypeError,ValueError) as exc:
            self.worker.emit('history.error',req,error=dict(code=getattr(exc,'code','history_io'),message=str(exc)))

    def terminal_cancel(self,req,query):
        self.worker.emit('history.result',req,result=dict(version=QUERY_VERSION,owner=query.get('owner'),status='cancelled',items=[]))

    def run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda:self.closed or self.pending)
                if self.closed:return
                _,item=self.pending.popitem(last=False);self.active=item
            req,query,cancel=item
            try:
                result=self.repository.query(query,cancel)
                with self.repository.store.mutex:
                    if cancel.is_set():raise Cancelled()
                    if result['epoch']!=self.repository.store.history_epoch:raise CoreError('记录已更新，请重新查询。','history_changed')
                    self.worker.emit('history.result',req,result=result)
            except Cancelled:self.terminal_cancel(req,query)
            except (CoreError,OSError,ValueError,TypeError,KeyError) as exc:
                self.worker.emit('history.error',req,error=dict(code=getattr(exc,'code','history_io'),message=str(exc)))
            finally:
                with self.condition:self.active=None

    def close(self):
        with self.condition:
            if self.closed:return
            self.closed=True
            if self.active:self.active[2].set()
            for item in self.pending.values():self.terminal_cancel(item[0],item[1])
            self.pending.clear();self.condition.notify()
        self.thread.join()
