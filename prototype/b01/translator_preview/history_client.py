"""Bounded history transport. Query state never owns a translation operation."""
import uuid
from copy import deepcopy
from PySide6.QtCore import QObject, Signal
from translator_core.history import QUERY_VERSION, METADATA_VERSION, MAX_PAGE_BYTES, encoded

class HistoryClient(QObject):
    changed=Signal(str)
    result=Signal(str,dict)
    invalidated=Signal(str)
    updated=Signal(dict)
    update_failed=Signal(str)
    def __init__(self,client):
        super().__init__(client);self.client=client;self.available=False;self.root_id=None
        self.views={};self.inflight={};self.cancels={};self.pending={};self.update_request=None
        self.records={};self.rejected=0;self.max_inflight=0;self.closed=False
        self.epoch=0
    def configure(self,diagnostics):
        desc=diagnostics.get('history',{})
        self.available=(diagnostics.get('capabilities',{}).get('history')==QUERY_VERSION and
            desc.get('contract')==QUERY_VERSION and desc.get('metadata')==METADATA_VERSION and bool(desc.get('root_id')))
        self.root_id=desc.get('root_id');self.closed=False
        for scope in self.views:self.changed.emit(scope)
        if self.available:self.refresh_all()
    def disconnect(self):
        self.available=False;self.root_id=None;self.pending.clear();self.inflight.clear();self.cancels.clear()
        self.update_request=None;self.records.clear();self.closed=False
        self.epoch=0
        for scope,view in self.views.items():
            view['owner']=None;view['result']=None;view['state']='disconnected';self.invalidated.emit(scope);self.changed.emit(scope)
    def register(self,scope):
        if scope not in self.views:
            self.views[scope]=dict(revision=0,owner=None,result=None,state='idle',message='',
                filters=dict(text='',kind=None,status=None,favorite=False,trash=False,limit=20))
    def observe_epoch(self,epoch):
        if type(epoch) is not int or epoch<=self.epoch:return
        self.epoch=epoch
        for scope,view in tuple(self.views.items()):
            result=view.get('result')
            if result and result.get('epoch',0)<epoch:
                self.invalidate(scope,state='stale')
                view['message']='保存内容已更新，请刷新搜索。'
                self.changed.emit(scope)
    def cancel_owned(self,owner):
        if not owner:return
        qid=owner['query_id']
        if qid in self.cancels.values():return
        if any(value==owner for value in self.inflight.values()):
            req=self.client.send('history.cancel',query_id=qid)
            if req:self.cancels[req]=qid
    def invalidate(self,scope,*,state='idle'):
        self.register(scope);view=self.views[scope];self.cancel_owned(view['owner']);self.pending.pop(scope,None)
        view['revision']+=1;view['owner']=None;view['result']=None;view['state']=state;view['message']=''
        self.invalidated.emit(scope);self.changed.emit(scope)
    def submit(self,scope,filters=None,cursor=None):
        self.register(scope);view=self.views[scope]
        if filters is not None:view['filters']=deepcopy(filters)
        self.invalidate(scope)
        if not self.available or self.closed or self.client.state not in ('ready','running','cancelling'):
            view['state']='unavailable';self.changed.emit(scope);return None
        owner=dict(query_id=str(uuid.uuid4()),session_id=self.client.session_id,root_id=self.root_id,
                   scope=scope,view_revision=view['revision'])
        view['owner']=owner;view['state']='querying'
        self.pending[scope]=dict(version=QUERY_VERSION,owner=owner,**view['filters'],cursor=cursor)
        self.changed.emit(scope);self.pump();return owner['query_id']
    def pump(self):
        while self.pending and len(self.inflight)<2 and not self.closed:
            scope=next(iter(self.pending));query=self.pending.pop(scope)
            req=self.client.send('history.query',query=query)
            if req:self.inflight[req]=query['owner'];self.max_inflight=max(self.max_inflight,len(self.inflight))
            else:
                self.views[scope]['state']='failed';self.views[scope]['message']='查询未发送，请检查工作进程。';self.changed.emit(scope)
    def refresh_all(self):
        if self.closed:return
        for scope in tuple(self.views):self.submit(scope)
    def cancel(self,scope):self.invalidate(scope,state='cancelled')
    def manage(self,row,changes):
        if not self.available or self.update_request or self.client.state!='ready' or self.client.task_request_pending():return False
        for scope in tuple(self.views):self.invalidate(scope,state='saving')
        self.update_request=self.client.send('history.update',task_id=row['id'],root_id=self.root_id,
            writer_session=self.client.session_id,task_revision=row['revision'],metadata_revision=row['metadata_revision'],changes=changes)
        return bool(self.update_request)
    def receive(self,event,request_kind):
        req=event['request_id'];kind=event['type']
        if request_kind=='history.query':
            owner=self.inflight.pop(req,None)
            scope=owner['scope'] if owner else None;view=self.views.get(scope)
            current=bool(view and view['owner']==owner and owner['root_id']==self.root_id and owner['session_id']==self.client.session_id)
            if not current:self.rejected+=1
            elif kind=='history.result':
                result=event.get('result',{})
                if result.get('version')!=QUERY_VERSION or result.get('owner')!=owner or len(encoded(result))>MAX_PAGE_BYTES:
                    self.rejected+=1;view['state']='failed';view['message']='历史响应身份或容量无效。';self.changed.emit(scope)
                elif result['status']=='cancelled':
                    view['state']='cancelled';self.changed.emit(scope)
                elif type(result.get('epoch')) is not int or result['epoch']<self.epoch:
                    self.rejected+=1;view['state']='stale';view['message']='保存内容已更新，请刷新搜索。';self.changed.emit(scope)
                elif any(row['metadata_revision']<self.records.get(row['id'],{}).get('metadata_revision',0) for row in result['items']):
                    self.rejected+=1;view['state']='failed';view['message']='记录已更新，请刷新。';self.changed.emit(scope)
                else:
                    self.epoch=max(self.epoch,result['epoch'])
                    view['result']=result;view['state']=result['status']
                    for row in result['items']:self.records[row['id']]=row
                    # Bound this presentation cache; authority stays on disk.
                    while len(self.records)>512:self.records.pop(next(iter(self.records)))
                    self.result.emit(scope,result);self.changed.emit(scope)
            elif kind=='history.error':
                view['state']='failed';view['message']=event['error']['message'];self.changed.emit(scope)
        elif request_kind=='history.cancel':self.cancels.pop(req,None)
        elif request_kind=='history.update':
            if req!=self.update_request:self.rejected+=1
            else:
                self.update_request=None
                if kind=='history.updated':
                    result=event['result'];meta=result['metadata']
                    self.observe_epoch(result['epoch'])
                    row=self.records.get(result['task_id'])
                    if row:
                        row.update(favorite=meta['favorite'],renamed_title=meta['display_title'],
                            trashed_at=meta['trashed_at'],metadata_revision=meta['revision'])
                        row['name']=meta['display_title'] or row.get('original_name',row['name'])
                    self.updated.emit(result)
                else:self.update_failed.emit(event.get('error',{}).get('message','历史修改尚未保存。'))
                self.refresh_all()
        self.pump()
    def close(self):
        if self.closed:return
        for scope in tuple(self.views):self.invalidate(scope,state='closing')
        self.pending.clear();self.closed=True
