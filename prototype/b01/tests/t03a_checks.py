"""T03-A actual Qt + owned worker checks; controlled service, no real model.

Run separately from QCoreApplication-only tests, like T01/T02 checks.
"""
import json
import platform
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
from t01_checks import APP, ROOT, wait, T01Checks
from t02_checks import T02Checks
from test_text_core import LocalAPI
from test_text_rules import CORPUS
from PySide6.QtGui import QTextCursor
from shiboken6 import delete
from translator_core.contracts import decode_snapshot, foundation_json, recipe_binding, sha
from translator_core.store import atomic_write
from translator_core.text_rules import analyze_text
from translator_preview.text_draft import TextDraftStore
from translator_preview.ui import PreviewWindow


class T03AChecks(unittest.TestCase):
    setUp=T01Checks.setUp
    cleanup=T01Checks.cleanup
    connect=T02Checks.connect

    def diagnose(self):
        client=self.window.client
        req=client.send('diagnose')
        wait(lambda:req not in client.requests)
        return client.diagnostic

    def test_same_corpus_direct_real_page_and_actual_worker_create(self):
        p,f=self.connect();w=self.window
        for case in CORPUS:
            with self.subTest(case=case['id']):
                p.direction.setCurrentIndex(0 if case['direction']=='en-zh' else 1)
                p.source.setPlainText(case['source'])
                actual=asdict(p.analysis);actual.pop('direction');actual.pop('identity')
                self.assertEqual(actual,case['expected'])
                if case['expected']['is_empty']:
                    self.assertFalse(p.translate_button.isEnabled());continue
                raw=case['source'].encode();name=uuid.uuid4().hex+'.txt'
                atomic_write(raw,self.path/'tasks/incoming'/name)
                req=w.client.send('task.create',input_file=name,byte_count=len(raw),sha256=sha(raw),direction=case['direction'],
                                  # Untrusted UI claims are ignored by the production path.
                                  target_kind='made-up',graphemes=99999)
                wait(lambda:req not in w.client.requests)
                task=w.client.current_task
                self.assertEqual(task['input'],case['source'])
                self.assertNotIn('text',task)
                info=self.diagnose();creation=info['last_text_creation']
                self.assertEqual(creation['task_id'],task['id'])
                self.assertEqual(creation['assessment']['analysis'],asdict(analyze_text(case['source'],case['direction'])))
                self.assertEqual(creation['assessment']['execution_kind'],'translation')
                self.assertFalse(creation['assessment']['explanation_available'])
                asset=self.path/'tasks'/task['id']/task['asset']['filename']
                self.assertEqual(asset.read_bytes(),raw)
                on_disk=decode_snapshot((asset.parent/'current.json').read_bytes())
                self.assertEqual(on_disk['input'].encode(),raw)
        self.assertEqual(self.api.calls,[])
        self.assertFalse(w.evidence['inference'])

    def test_edits_direction_threshold_theme_read_copy_export_never_auto_generate(self):
        p,f=self.connect();w=self.window
        for source in ('hello','a'*160,'a'*161,'a '*20,'a '*21,'e\u0301 👨‍👩‍👧‍👦 𠀀'):
            p.source.setPlainText(source)
            for direction in (1,0): p.direction.setCurrentIndex(direction)
            w.theme.set_preference('dark');w.navigate('settings');w.navigate('history');w.navigate('translate')
            w.select_tab(1);w.select_tab(2);w.select_tab(0)
        self.assertEqual(self.api.calls,[])
        self.assertIsNone(f.saved)
        p.source.setPlainText('Please keep 12 notes.')
        self.assertTrue(f.translate())
        wait(lambda:f.saved and not f.busy)
        task=f.saved['task'];recipe=foundation_json(task['configuration'])
        self.assertEqual(task['text']['phase'],'completed')
        self.assertEqual(len([c for c in self.api.calls if c[0]=='/api/chat']),1)
        result=task['text']['segments'][0]['result']
        self.assertEqual(result['configurationBinding'],recipe_binding(task['configuration']))
        self.assertEqual(task['configuration']['promptProfile'],'hy-mt2-faithful-v2')
        self.assertIsNone(p.presentation.result.explanation)
        calls=list(self.api.calls);raw=(self.path/'tasks'/task['id']/'current.json').read_bytes()
        p.copy();f.export(target=self.path/'complete.txt');wait(lambda:not f.export_request)
        self.assertTrue(f.reopen());wait(lambda:not w.bridge.open_request)
        w.navigate('settings');w.theme.set_preference('light');w.navigate('translate')
        self.assertTrue(f.return_draft());w.open_task(task['id']);wait(lambda:not w.bridge.open_request)
        self.assertEqual(self.api.calls,calls)
        self.assertEqual((self.path/'tasks'/task['id']/'current.json').read_bytes(),raw)
        self.assertEqual(foundation_json(f.saved['task']['configuration']),recipe)

    def test_unicode_utf16_draft_and_history_selection_across_pages_and_reopen(self):
        p,f=self.connect();w=self.window
        text='A e\u0301 👨‍👩‍👧‍👦 👍🏽 🇨🇳 𠀀 Z'
        p.source.setPlainText(text);p.direction.setCurrentIndex(1)
        def select():
            cursor=p.source.textCursor();cursor.setPosition(5);cursor.setPosition(29,QTextCursor.KeepAnchor);p.source.setTextCursor(cursor)
        select();original=f.capture_editor();self.assertEqual((original['position'],original['anchor']),(29,5))
        self.assertEqual(p.analysis.graphemes,13)
        w.select_tab(1);w.select_tab(0);w.navigate('settings');w.theme.set_preference('dark');w.navigate('translate')
        self.assertEqual(f.capture_editor(),original)
        old=ROOT/'fixtures/b02/shared-task/11111111-2222-4333-8444-555555555555'
        self.assertTrue(f.import_copy(old));wait(lambda:f.saved and not f.busy)
        identity=f.saved['task']['id'];current=self.path/'tasks'/identity/'current.json';raw=current.read_bytes()
        w.close();wait(lambda:not w.isVisible());delete(w);APP.processEvents()
        store=TextDraftStore(self.path/'tasks');data=store.load()
        store.save(dict(data['draft'],mode=1),dict(data['context'],mode=1,suppressed=True));store.close()
        self.window=w=PreviewWindow(task_root=self.path/'tasks',preferences_path=self.path/'prefs.json')
        w.show();wait(lambda:w.text_flow.saved and not w.bridge.open_request)
        p,f=w.text_workspace,w.text_flow
        self.assertEqual(current.read_bytes(),raw);self.assertTrue(f.return_draft())
        self.assertEqual(f.capture_editor(),original)
        self.assertEqual(p.source.textCursor().selectedText(),'👨‍👩‍👧‍👦 👍🏽 🇨🇳 𠀀')
        self.assertTrue(f.create(False));wait(lambda:f.saved and not f.busy)
        select();f.dirty=True;self.assertTrue(f.flush_draft())
        identity=f.saved['task']['id'];current=self.path/'tasks'/identity/'current.json';raw=current.read_bytes()
        w.close();wait(lambda:not w.isVisible());delete(w);APP.processEvents()
        self.window=w=PreviewWindow(task_root=self.path/'tasks',preferences_path=self.path/'prefs.json')
        w.show();wait(lambda:w.text_flow.saved and not w.bridge.open_request)
        p=w.text_workspace
        self.assertEqual((p.source.textCursor().position(),p.source.textCursor().anchor()),(29,5))
        self.assertEqual(current.read_bytes(),raw);self.assertFalse(w.evidence['inference'])
        self.assertEqual(self.api.calls,[])

    def test_crlf_source_display_export_and_binding_remain_distinct(self):
        p,f=self.connect();w=self.window
        source='A e\u0301 👨‍👩‍👧‍👦 𠀀\r\nSecond line.\r\n'
        raw=source.encode();name=uuid.uuid4().hex+'.txt';atomic_write(raw,self.path/'tasks/incoming'/name)
        req=w.client.send('task.create',input_file=name,byte_count=len(raw),sha256=sha(raw),direction='en-zh')
        wait(lambda:req not in w.client.requests);task=w.client.current_task;identity=task['id']
        current=self.path/'tasks'/identity/'current.json';sealed=current.read_bytes();recipe=foundation_json(task['configuration'])
        w.open_task(identity);wait(lambda:f.saved and not w.bridge.open_request)
        self.assertEqual(f.view_source,source.replace('\r\n','\n'));self.assertTrue(f.bound)
        self.assertEqual(p.analysis,analyze_text(source,'en-zh'))
        self.assertTrue(f.export(full=True,target=self.path/'raw.txt'));wait(lambda:not f.export_request)
        self.assertIn(raw,(self.path/'raw.txt').read_bytes());self.assertEqual(current.read_bytes(),sealed)
        self.assertTrue(f.translate());wait(lambda:not f.busy)
        task=f.saved['task'];parts=task['text']['segments']
        self.assertEqual(''.join(s['source'] for s in parts).encode(),raw)
        self.assertEqual(foundation_json(task['configuration']),recipe)
        for s in parts:self.assertEqual(s['result']['configurationBinding'],recipe_binding(task['configuration']))
        self.assertEqual((current.parent/task['asset']['filename']).read_bytes(),raw)
        self.assertEqual(len([c for c in self.api.calls if c[0]=='/api/chat']),len(parts))

    def test_platform_title_common_fonts_and_active_qt_prototype_use_same_count(self):
        w=self.window
        self.assertIn(platform.system(),w.windowTitle());self.assertIn('t03a-preview.1',w.windowTitle())
        self.assertNotIn('windows',w.evidence)
        p=w.text_workspace
        w.theme.set_preference('light')
        self.assertEqual(p.source.font().families(),p.output.font().families())
        self.assertIn('Microsoft YaHei UI',p.source.font().families())
        sys.path.insert(0,str(ROOT/'prototype/ui-redesign'))
        from design_preview import TranslationDesign
        design=TranslationDesign();self.addCleanup(lambda:delete(design))
        design.source.setPlainText('e\u0301 👨‍👩‍👧‍👦 𠀀')
        self.assertEqual(design.char_count.text(),'5 字符 · 2 词')


if __name__=='__main__':unittest.main(verbosity=2)
