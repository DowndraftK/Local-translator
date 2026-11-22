"""T01 risk checks, isolated stores and offscreen Qt; never model generation.

Run separately from the QCoreApplication-only legacy suite.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT/'prototype/b01'), str(ROOT/'shared')]
from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QPixmap, QTextCursor
from PySide6.QtWidgets import QApplication, QLabel
from shiboken6 import delete
from translator_core.contracts import decode_snapshot
from translator_preview.ui import PreviewWindow
from translator_preview.text_page import TextPage
from translator_preview.theme import ThemeController
from translator_preview.workspace import (Alternative, Example, Explanation, ResultOwner, TaskState,
                                          TranslationResult)

APP = QApplication([])
APP.setStyle('Fusion')


def wait(condition, seconds=8):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        APP.processEvents()
        if condition(): return
        time.sleep(.005)
    raise AssertionError('Qt condition timed out')


class T01Checks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='translator-t01-')
        self.path = Path(self.temp.name)
        self.window = PreviewWindow(task_root=self.path/'tasks', preferences_path=self.path/'prefs.json', connect_worker=False)
        self.window.show(); APP.processEvents()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        w = self.window
        w.close()
        wait(lambda: not w.isVisible())
        self.assertFalse(w.client.failure)
        delete(w); APP.processEvents(); self.temp.cleanup()

    def test_appearance_persistence_corruption_and_native_event_mapping(self):
        w = self.window
        self.assertEqual(w.theme.preference, 'system')
        w.text_workspace.source.setPlainText('Keep this draft')
        source_id = id(w.text_workspace.source)
        w.navigate('settings'); w.theme.set_preference('dark')
        w.theme.system_changed(Qt.ColorScheme.Light)
        self.assertEqual(w.theme.effective, 'dark')
        other = ThemeController(self.path/'prefs.json')
        self.assertEqual(other.preference, 'dark'); delete(other)
        w.theme.set_preference('system'); w.theme.system_changed(Qt.ColorScheme.Light)
        self.assertEqual(w.theme.effective, 'light')
        w.theme.system_changed(Qt.ColorScheme.Dark); self.assertEqual(w.theme.effective, 'dark')
        self.assertEqual(id(w.text_workspace.source), source_id)
        self.assertEqual(w.text_workspace.source.toPlainText(), 'Keep this draft')
        self.assertEqual(w.current_section, 'settings')
        (self.path/'bad.json').write_text('{broken')
        other = ThemeController(self.path/'bad.json')
        self.assertEqual(other.preference, 'system'); self.assertTrue(other.warning); delete(other)
        self.assertEqual((self.path/'bad.json').read_text(), '{broken')
        (self.path/'large.json').write_text('x'*4097)
        other = ThemeController(self.path/'large.json'); self.assertTrue(other.warning); delete(other)

    def test_unified_text_has_no_mode_controls_and_preserves_cursor(self):
        p = self.window.text_workspace
        p.source.setPlainText('Controlled source')
        cursor = p.source.textCursor(); cursor.setPosition(3); cursor.setPosition(9,QTextCursor.KeepAnchor); p.source.setTextCursor(cursor)
        owner = p.owner(); self.assertTrue(p.present_result(TranslationResult(owner,'Controlled result')))
        self.assertEqual(p.source.toPlainText(), 'Controlled source')
        self.assertEqual((p.source.textCursor().position(), p.source.textCursor().anchor()), (9,3))
        self.window.navigate('settings'); self.window.navigate('translate')
        self.assertEqual(p.output.toPlainText(),'Controlled result')
        self.assertFalse(hasattr(p,'mode') or hasattr(self.window.settings_page,'mode'))
        for widget in self.window.findChildren(QLabel):
            self.assertNotIn(widget.text(),('翻译模式','文字翻译模式','简洁翻译','详解翻译'))
        p.source.insertPlainText('changed')
        self.assertIsNone(p.presentation.result)
        self.assertFalse(p.present_result(TranslationResult(owner,'Stale result')))

    def test_detail_reuse_identity_generation_config_and_source_invalidation(self):
        p = self.window.text_workspace
        p.source.setPlainText('Controlled source')
        owner = p.owner(task_id='task-identity',task_revision=3,request_id='request',generation='generation-a')
        self.assertTrue(p.expect_result(owner))
        detail = Explanation((Alternative('Alternative','Context','Explanation'),),('Usage',),(Example('Source','Target'),))
        result = TranslationResult(owner,'Main translation',detail)
        self.assertTrue(p.present_result(result))
        self.assertEqual(p.output_stack.currentIndex(),2)
        self.window.navigate('settings'); self.window.navigate('translate')
        self.assertIs(p.presentation.result,result)
        new_owner = p.owner(task_id='task-identity',task_revision=3,request_id='new-request',generation='generation-b')
        p.expect_result(new_owner); self.assertFalse(p.present_result(result))
        self.assertTrue(p.present_result(TranslationResult(new_owner,'New translation',detail)))
        p.source.insertPlainText('changed'); self.assertIsNone(p.presentation.result)
        self.assertFalse(p.present_result(result))
        p.source.setPlainText('Controlled source'); p.expect_result(owner); p.present_result(result)
        p.set_configuration(dict(p.configuration,model='controlled-different-config'),'revision-b')
        self.assertIsNone(p.presentation.result); self.assertFalse(p.expect_result(owner))
        with self.assertRaises(ValueError): TranslationResult(owner,'Main',{'raw':'json'})
        with self.assertRaises(ValueError): TranslationResult(owner,'Main',version=99)

    def test_navigation_small_window_image_geometry_and_menu_theme(self):
        w = self.window; w.resize(1060,720); APP.processEvents()
        p = w.text_workspace; p.source.setPlainText('Persistent draft')
        for section in ('audio','history','settings','translate'):
            w.nav_buttons[section].setChecked(True); APP.processEvents()
            self.assertEqual(w.page_stack.currentWidget(), w.pages[section])
        for index in (1,2,0):
            w.tab_buttons[index].setChecked(True); APP.processEvents()
            self.assertEqual(w.translation_stack.currentIndex(),index)
        self.assertEqual(p.source.toPlainText(),'Persistent draft')
        self.assertEqual(w.size().width(),1060)
        for theme in ('light','dark'):
            w.theme.set_preference(theme); w.select_tab(2); APP.processEvents()
            for dimensions in ((1800,180),(180,1800)):
                pix = QPixmap(*dimensions); pix.fill(Qt.red)
                w.image_page.display_image(pix,'Controlled aspect-ratio image','controlled')
                APP.processEvents()
                image, toolbar = w.image_page.preview, w.image_page.toolbar
                bottom = image.mapTo(w.image_page,image.rect().bottomLeft()).y()
                top = toolbar.mapTo(w.image_page,toolbar.rect().topLeft()).y()
                self.assertLess(bottom,top)
                selection_top = w.image_page.selection.mapTo(w.image_page,QPoint(0,0)).y()
                self.assertLess(toolbar.mapTo(w.image_page,toolbar.rect().bottomLeft()).y(),selection_top)
            w.select_tab(0); p.direction.showPopup(); APP.processEvents()
            self.assertTrue(p.direction.popup.isVisible()); self.assertGreater(p.direction.popup.width(),100)
            p.direction.hidePopup()
        self.assertFalse(w.client.process)  # no worker, service or generation invoked by navigation

    def test_state_mapping_worker_ready_is_not_completion(self):
        fixture = ROOT/'fixtures/b02/shared-task/11111111-2222-4333-8444-555555555555/current.json'
        task = decode_snapshot(fixture.read_bytes())
        snapshot = dict(task=task,durable_revision=task['revision'],save_blocked=False)
        state = TaskState.map(snapshot,'ready')
        self.assertEqual(state.phase,'stopped'); self.assertEqual((state.completed,state.total),(1,2))
        self.assertTrue(state.can_start); self.assertFalse(state.can_cancel)
        blocked = TaskState.map(dict(snapshot,save_blocked=True,warning='controlled write failure'),'ready')
        self.assertEqual(blocked.save_status,'blocked'); self.assertFalse(blocked.can_start or blocked.can_save)
        self.assertTrue(blocked.can_export); self.assertEqual(blocked.error,'controlled write failure')
        running = TaskState.map(snapshot,'running','task.start')
        self.assertTrue(running.can_cancel); self.assertFalse(running.can_start)

    def test_compatibility_switch_and_reconnect_do_not_reuse_stale_result(self):
        w = self.window
        legacy = TextPage(w.client, self.path/'tasks', w, auto_refresh=False)
        w.client.connect_worker(); wait(lambda: w.client.state == 'ready' and not w.bridge.refresh_request)
        fixture = ROOT/'fixtures/b02/shared-task/11111111-2222-4333-8444-555555555555'
        legacy.send('task.import',source_directory=str(fixture))
        wait(lambda: legacy.saved is not None and not legacy.pending)
        identity = legacy.saved['task']['id']
        self.assertTrue(legacy.output.toPlainText())
        before = (self.path/'tasks'/identity/'current.json').read_bytes()
        legacy.receive_snapshot({'task':{'kind':'文档'}})
        self.assertEqual(legacy.input.toPlainText(),'')
        self.assertEqual(legacy.output.toPlainText(),'')
        legacy.input.setPlainText('A different unsubmitted draft')
        cursor = legacy.input.textCursor(); cursor.setPosition(4); legacy.input.setTextCursor(cursor)
        legacy.receive_snapshot({'task':{'kind':'文档'}})
        self.assertEqual(legacy.input.toPlainText(),'A different unsubmitted draft')
        self.assertEqual(legacy.input.textCursor().position(),4)
        w.open_task(identity); wait(lambda: w.bridge.open_request is None and legacy.saved is not None)
        legacy.restore_history_draft()
        self.assertEqual(legacy.input.toPlainText(),'A different unsubmitted draft')
        self.assertEqual(legacy.input.textCursor().position(),4)
        w.open_task(identity); wait(lambda: w.bridge.open_request is None and legacy.saved is not None)
        w.client.shutdown(); wait(lambda: w.client.state == 'disconnected')
        w.client.connect_worker(); wait(lambda: w.client.state == 'ready' and not w.bridge.refresh_request)
        self.assertIsNone(w.bridge.state.task_id)
        self.assertFalse(legacy.start.isEnabled() or legacy.save.isEnabled() or legacy.export_button.isEnabled())
        w.open_task(identity); wait(lambda: w.bridge.open_request is None and legacy.start.isEnabled())
        self.assertEqual((self.path/'tasks'/identity/'current.json').read_bytes(),before)
        self.assertFalse(w.evidence['inference'])

    def test_real_worker_shared_history_compatibility_and_no_inference(self):
        w = self.window; events = []; w.client.event.connect(events.append)
        self.assertTrue(w.client.connect_worker()); wait(lambda: w.client.state=='ready' and w.bridge.refresh_request is None)
        p = w.text_workspace; p.source.setPlainText('T01 controlled draft')
        w.text_flow.create(False); wait(lambda: w.text_flow.saved is not None and not w.text_flow.pending)
        wait(lambda: len(w.bridge.read_summaries())==1)
        identity = w.text_flow.saved['task']['id']
        before = (self.path/'tasks'/identity/'current.json').read_bytes()
        w.toggle_history_drawer(); wait(lambda: w.bridge.refresh_request is None)
        self.assertIs(w.history_page.bridge,w.drawer_view.bridge)
        summary = w.bridge.read_summaries()[0]
        self.assertEqual(summary.task_id,identity); self.assertEqual(summary.revision,1)
        self.assertIsNone(summary.favorite); self.assertTrue(summary.source_ref['fingerprint'])
        self.assertTrue(w.bridge.open_task(identity)); wait(lambda: w.bridge.open_request is None)
        self.assertEqual(w.text_container.currentIndex(),0)
        self.assertEqual((self.path/'tasks'/identity/'current.json').read_bytes(),before)
        self.assertEqual(p.source.toPlainText(),'T01 controlled draft')
        w.theme.set_preference('dark'); w.navigate('history'); w.navigate('translate')
        self.assertEqual((self.path/'tasks'/identity/'current.json').read_bytes(),before)
        self.assertFalse(any(e['type']=='operation.started' and e.get('result_kind')=='task.start' for e in events))
        # Real TXT selection/create/extract through unchanged worker and compatibility page.
        source = self.path/'controlled.txt'; source.write_text('A controlled document.\nSecond line.')
        w.select_tab(1); w.select_document(str(source))
        wait(lambda: w.document_page.selection is not None and w.client.state=='ready')
        w.document_page.create_task(); wait(lambda: w.document_page.saved is not None and not w.document_page.pending)
        w.document_page.operate('document.extract'); wait(lambda: w.client.state=='ready' and w.document_page.operation_request is None)
        self.assertIn('A controlled document.',w.document_page.input.toPlainText())
        wait(lambda: len(w.bridge.read_summaries())==2)
        self.assertFalse(w.evidence['inference'])
        w.client.start_probe(steps=40,interval_ms=100,lease_ms=20000)
        wait(lambda: any(e['type']=='progress' for e in events))
        task_id = w.client.current_task['id']; w.navigate('settings'); w.theme.set_preference('light')
        self.assertEqual(w.client.current_task['id'],task_id); self.assertEqual(w.client.state,'running')
        self.assertTrue(w.client.cancel()); wait(lambda: w.client.state=='ready')
        self.assertEqual(w.client.terminal['status'],'cancelled')
        self.assertTrue(w.client.terminal['tail_drained'])


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        delete(APP)
