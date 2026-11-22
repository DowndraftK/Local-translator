"""T02 UI integration risks. Controlled loopback results are never real-model evidence."""
import json
from pathlib import Path
import sys
from copy import deepcopy
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
import t01_checks
from t01_checks import APP, ROOT, wait
sys.path.insert(0,str(ROOT/'prototype/b01/tests'))
from test_text_core import LocalAPI
from PySide6.QtGui import QTextCursor
from translator_preview.ui import PreviewWindow
from translator_preview.text_draft import TextDraftStore
from translator_core.contracts import foundation_json, sha


class T02Checks(unittest.TestCase):
    setUp = t01_checks.T01Checks.setUp
    cleanup = t01_checks.T01Checks.cleanup

    def connect(self, api=True):
        self.api = LocalAPI() if api else None
        if self.api:
            self.addCleanup(self.api.close)
            self.window.client.endpoint = self.api.address
        w = self.window; w.client.connect_worker()
        wait(lambda:w.client.state=='ready' and not w.bridge.refresh_request)
        return w.text_workspace, w.text_flow

    def test_save_ack_single_click_repeat_gate_main_translation_and_consistent_export(self):
        p,f = self.connect(); w = self.window
        p.source.setPlainText('The task contains 12 local notes.')
        p.translate_button.click(); p.translate_button.click()
        self.assertTrue(f.pending); self.assertTrue(p.source.isReadOnly())
        wait(lambda:f.saved and (f.saved['task'].get('text') or {}).get('phase')=='completed' and not f.busy)
        self.assertEqual(len(w.bridge.read_summaries()),1)
        self.assertEqual(len([c for c in self.api.calls if c[0]=='/api/chat']),1)
        self.assertEqual(p.output.toPlainText(),'已确认译文。')
        self.assertNotIn('双语',p.output.toPlainText())
        self.assertTrue(p.copy_button.isEnabled() and p.export_button.isEnabled())
        p.copy(); self.assertEqual(APP.clipboard().text(),p.output.toPlainText())
        target = self.path/'full.txt'; self.assertTrue(f.export(target=target)); wait(lambda:not f.export_request)
        self.assertIn('The task contains 12 local notes.',target.read_text())
        self.assertIn(p.output.toPlainText(),target.read_text())
        before = list(self.api.calls); task = f.saved['task']; current = self.path/'tasks'/task['id']/'current.json'; raw = current.read_bytes()
        w.navigate('settings'); w.navigate('translate'); p.copy(); w.bridge.refresh()
        wait(lambda:not w.bridge.refresh_request)
        self.assertEqual(p.output.toPlainText(),'已确认译文。')
        self.assertEqual(self.api.calls,before); self.assertEqual(current.read_bytes(),raw)
        self.assertTrue(f.reopen()); wait(lambda:not w.bridge.open_request)
        self.assertEqual(p.output.toPlainText(),'已确认译文。')

    def test_long_cancel_preserves_success_plan_config_and_active_input_gates(self):
        p,f = self.connect(); w = self.window
        self.api.mode = 'slow_body'
        source = 'The task has 12 notes. Keep each note on this computer.\n'*90
        p.source.setPlainText(source); cancel_sent = []
        def cancel_after_prefix(snapshot):
            parts = ((snapshot.get('task') or {}).get('text') or {}).get('segments',[])
            if len(parts)>1 and parts[0]['state']=='completed' and parts[1]['state']=='processing' and not cancel_sent:
                cancel_sent.append(f.cancel())
        w.client.snapshot.connect(cancel_after_prefix)
        self.assertTrue(f.translate()); self.assertFalse(f.translate())
        self.assertFalse(w.bridge.open_task('11111111-2222-4333-8444-555555555555'))
        w.navigate('settings'); w.theme.set_preference('dark'); w.navigate('translate')
        wait(lambda:bool(cancel_sent) and not f.busy)
        task = f.saved['task']; parts = task['text']['segments']
        self.assertTrue(cancel_sent[0]); self.assertEqual(task['text']['phase'],'stopped')
        self.assertEqual(parts[0]['state'],'completed'); self.assertNotEqual(parts[1]['state'],'completed')
        prefix = foundation_json(parts[0]); recipe = foundation_json(task['configuration'])
        plan = [(s['source'],s['utf8Start'],s['utf8End']) for s in parts]
        self.assertEqual(p.source.toPlainText(),source)
        target = self.path/'partial.txt'; f.export(target=target); wait(lambda:not f.export_request)
        value = target.read_text()
        for s in parts: self.assertIn(s['source'],value)
        self.assertIn('【',value); self.assertIn('1/',p.notice.text())
        w.client.snapshot.disconnect(cancel_after_prefix)
        raw = (self.path/'tasks'/task['id']/'current.json').read_bytes()
        w.open_task(task['id']); wait(lambda:not w.bridge.open_request)
        self.assertEqual((self.path/'tasks'/task['id']/'current.json').read_bytes(),raw)
        self.assertTrue(f.translate()); wait(lambda:not f.busy)
        task = f.saved['task']; self.assertEqual(task['text']['phase'],'completed')
        self.assertEqual(foundation_json(task['text']['segments'][0]),prefix)
        self.assertEqual(foundation_json(task['configuration']),recipe)
        self.assertEqual([(s['source'],s['utf8Start'],s['utf8End']) for s in task['text']['segments']],plan)

    def test_draft_history_return_cursor_and_cross_exit_restore(self):
        p,f = self.connect(False); w = self.window
        p.source.setPlainText('Unsubmitted original draft 原文'); p.direction.setCurrentIndex(1)
        cursor = p.source.textCursor(); cursor.setPosition(3); cursor.setPosition(11,QTextCursor.KeepAnchor); p.source.setTextCursor(cursor)
        fixture = ROOT/'fixtures/b02/shared-task/11111111-2222-4333-8444-555555555555'
        self.assertTrue(f.import_copy(fixture)); wait(lambda:f.saved and not f.pending)
        task_id = f.saved['task']['id']; original = f.draft.copy()
        p.source.setTextCursor(QTextCursor(p.source.document())); w.theme.set_preference('dark')
        w.navigate('history'); w.navigate('translate')
        self.assertTrue(f.return_draft())
        self.assertEqual(f.capture_editor(),original)
        w.open_task(task_id); wait(lambda:not w.bridge.open_request)
        before = (self.path/'tasks'/task_id/'current.json').read_bytes()
        w.close(); wait(lambda:not w.isVisible())
        # Old detailed drafts and suppressed reading contexts must not restore
        # an inaccessible mode after the selector is removed.
        store = TextDraftStore(self.path/'tasks'); data = store.load()
        store.save(dict(data['draft'],mode=1),dict(data['context'],mode=1,suppressed=True)); store.close()
        from shiboken6 import delete
        delete(w); APP.processEvents()
        self.window = w = PreviewWindow(task_root=self.path/'tasks',preferences_path=self.path/'prefs.json',connect_worker=True)
        w.show(); wait(lambda:w.text_flow.saved is not None and not w.bridge.open_request)
        self.assertEqual((self.path/'tasks'/task_id/'current.json').read_bytes(),before)
        self.assertEqual(w.text_flow.draft,original)
        self.assertTrue(w.text_workspace.output.toPlainText())
        self.assertTrue(w.text_flow.return_draft()); self.assertEqual(w.text_flow.capture_editor(),original)
        self.assertFalse(w.evidence['inference'])

    def test_draft_atomic_failure_keeps_window_and_corrupt_file_has_backup(self):
        p = self.window.text_workspace; f = self.window.text_flow
        p.source.setPlainText('Unsaved must remain here')
        writer = f.store.save
        f.store.save = lambda *a: (_ for _ in ()).throw(OSError('controlled disk failure'))
        self.window.close(); APP.processEvents()
        self.assertTrue(self.window.isVisible()); self.assertTrue(f.dirty)
        self.assertIn('controlled disk failure',p.notice.text())
        f.store.save = writer; self.assertTrue(f.flush_draft())
        self.assertEqual(json.loads(f.store.path.read_bytes())['draft']['text'],p.source.toPlainText())
        self.assertIn('草稿已保存',p.notice.text())
        self.assertNotIn('草稿待保存',p.notice.text()); self.assertNotIn('controlled disk failure',p.notice.text())
        from translator_preview.text_draft import TextDraftStore
        other = TextDraftStore(self.path/'corrupt-tasks'); other.path.write_text('{broken')
        self.assertIsNone(other.load()); self.assertIn('保留',other.warning)
        other.save(f.draft,None); other.close()
        backups = list(self.path.glob('corrupt-tasks-text-draft.json.unreadable-*'))
        self.assertEqual(len(backups),1); self.assertEqual(backups[0].read_text(),'{broken')

    def test_draft_single_writer_and_late_writer_cannot_replace_newer_input(self):
        from translator_preview.text_draft import TextDraftStore
        a = TextDraftStore(self.path/'other'); b = TextDraftStore(self.path/'other')
        a.load(); b.load(); d = dict(a=1)
        draft = dict(text='First',direction='en-zh',mode=0,position=0,anchor=0)
        try:
            a.save(draft,None)
            with self.assertRaises(ValueError): b.save(dict(draft,text='Late'),None)
            a.close()
            with self.assertRaises(ValueError): b.save(dict(draft,text='Late'),None)
            self.assertEqual(json.loads(a.path.read_bytes())['draft']['text'],'First')
        finally: a.close(); b.close()

    def test_missing_service_preserves_saved_input_and_explicit_retry(self):
        p,f = self.connect(False); w = self.window
        # A refused loopback port exercises real worker failure, without altering the shared service.
        import socket
        sock = socket.socket(); sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]; sock.close()
        w.client.shutdown(); wait(lambda:w.client.state=='disconnected')
        w.client.endpoint = 'http://127.0.0.1:'+str(port); w.client.connect_worker(); wait(lambda:w.client.state=='ready')
        p.source.setPlainText('A saved source survives service failure.')
        f.translate(); wait(lambda:f.saved and not f.busy)
        self.assertEqual(f.saved['task']['text']['phase'],'failed')
        self.assertEqual(p.output.toPlainText(),''); self.assertFalse(p.copy_button.isEnabled())
        self.assertIn('服务',p.notice.text()); self.assertTrue(p.translate_button.isEnabled())
        target = self.path/'failed.txt'; self.assertTrue(f.export(full=True,target=target)); wait(lambda:not f.export_request)
        self.assertIn(p.source.toPlainText(),target.read_text())
        self.assertIn('【',target.read_text())

    def test_reconnect_requires_open_and_rejects_late_request_generation(self):
        p,f = self.connect(); w = self.window
        p.source.setPlainText('A current input.'); f.translate(); wait(lambda:f.saved and not f.busy)
        saved = deepcopy(f.saved); task_id = saved['task']['id']; expected = p.output.toPlainText()
        w.client.snapshot_context = dict(session=w.client.session_id,request='old-request',kind='task.start',generation='old-generation')
        stale = deepcopy(saved); stale['task']['text']['segments'][0]['result']['translation']='WRONG LATE RESULT'
        w.client.snapshot.emit(stale)
        self.assertEqual(p.output.toPlainText(),expected)
        w.client.shutdown(); wait(lambda:w.client.state=='disconnected')
        w.client.connect_worker(); wait(lambda:w.client.state=='ready' and not w.bridge.refresh_request)
        self.assertFalse(p.translate_button.isEnabled() or p.export_button.isEnabled())
        self.assertFalse(f.translate()); self.assertTrue(f.reopen()); wait(lambda:not w.bridge.open_request)
        self.assertTrue(p.export_button.isEnabled()); self.assertEqual(f.saved['task']['id'],task_id)

    def test_incomplete_output_does_not_copy_or_complete_and_retries_remain_explicit(self):
        p,f = self.connect(); self.api.mode = 'truncated'
        p.source.setPlainText('An incomplete response must not become a result.')
        f.translate(); wait(lambda:f.saved and not f.busy)
        self.assertEqual(f.saved['task']['text']['phase'],'partial')
        self.assertEqual(p.output.toPlainText(),''); self.assertFalse(p.copy_button.isEnabled() or p.export_button.isEnabled())
        self.assertEqual(len([c for c in self.api.calls if c[0]=='/api/chat']),1)
        for _ in range(2):
            self.assertTrue(f.translate()); wait(lambda:not f.busy)
        self.assertEqual(len([c for c in self.api.calls if c[0]=='/api/chat']),3)
        self.assertFalse(p.translate_button.isEnabled())
        self.assertIn('上限',p.translate_button.text())

    def test_legacy_crlf_source_keeps_original_bytes_and_remains_bound_in_new_view(self):
        p,f = self.connect(False)
        fixture = ROOT/'fixtures/b01/text-task/11111111-2222-4333-8444-555555555555'
        self.assertTrue(f.import_copy(fixture)); wait(lambda:f.saved and not f.pending)
        task = f.saved['task']; self.assertIn('\r\n',task['input'])
        self.assertNotEqual(p.source.toPlainText(),task['input'])
        self.assertTrue(f.bound); self.assertTrue(p.translate_button.isEnabled())
        cursor = p.source.textCursor(); cursor.setPosition(5); p.source.setTextCursor(cursor)
        self.assertTrue(f.export(target=self.path/'legacy.txt')); wait(lambda:not f.export_request)
        for part in task['text']['segments']:
            self.assertIn(part['source'].encode(),(self.path/'legacy.txt').read_bytes())
        self.assertEqual(p.source.textCursor().position(),5)
        original = deepcopy(f.saved)
        p.set_configuration(p.configuration,'changed-effective-revision')
        self.assertFalse(f.bound); self.assertEqual(p.output.toPlainText(),'')
        self.window.client.snapshot_context = dict(session=self.window.client.session_id,request='late-save',kind='task.save',generation=None)
        self.window.client.snapshot.emit(original)
        self.assertEqual(p.output.toPlainText(),'')
        self.assertTrue(f.reopen()); wait(lambda:not self.window.bridge.open_request)

    def test_saved_task_edit_uses_new_draft_without_late_refill_or_history_rewrite(self):
        p,f = self.connect(); w = self.window
        p.source.setPlainText('Original saved source.'); f.translate(); wait(lambda:f.saved and not f.busy)
        saved = deepcopy(f.saved); identity = saved['task']['id']
        current = self.path/'tasks'/identity/'current.json'; before = current.read_bytes()
        self.assertTrue(f.edit_copy()); self.assertFalse(p.source.isReadOnly())
        p.source.setPlainText('A different draft'); p.direction.setCurrentIndex(1)
        w.client.snapshot_context = dict(session=w.client.session_id,request='old-save',kind='task.save',generation=None)
        w.client.snapshot.emit(saved)
        self.assertEqual(p.source.toPlainText(),'A different draft'); self.assertEqual(p.output.toPlainText(),'')
        self.assertEqual(current.read_bytes(),before)
        self.assertFalse(p.copy_button.isEnabled() or p.export_button.isEnabled())

    def test_save_blocked_close_gate_and_memory_export_remain_available(self):
        p,f = self.connect(); w = self.window
        p.source.setPlainText('Keep confirmed memory results.'); f.translate(); wait(lambda:f.saved and not f.busy)
        clean = deepcopy(f.saved); blocked = deepcopy(clean); blocked['save_blocked']=True
        blocked['warning']='controlled task save failure'; blocked['durable_revision']-=1
        w.client.snapshot_context = dict(session=w.client.session_id,request='save-failure',kind='task.save',generation=None)
        w.client.snapshot.emit(blocked)
        self.assertFalse(p.translate_button.isEnabled()); self.assertTrue(p.export_button.isEnabled())
        self.assertFalse(w.client.can_change_task())
        self.assertFalse(f.prepare_close(lambda:None)); self.assertTrue(w.isVisible())
        self.assertTrue(f.export(target=self.path/'memory.txt')); wait(lambda:not f.export_request)
        self.assertEqual(f.exported_revision,f.saved['task']['revision'])
        w.client.snapshot.emit(clean)  # controlled failure removed before normal cleanup

    def test_close_during_initial_save_cancels_chained_generation(self):
        p,f = self.connect(); w = self.window
        p.source.setPlainText('Save this input while closing.')
        f.translate(); self.assertTrue(f.pending); w.close()
        wait(lambda:not w.isVisible())
        self.assertEqual([c for c in self.api.calls if c[0]=='/api/chat'],[])
        self.assertTrue(w.client.shutdown_confirmed)
        self.assertFalse(w.evidence['inference'])


if __name__=='__main__':
    unittest.main(verbosity=2)
