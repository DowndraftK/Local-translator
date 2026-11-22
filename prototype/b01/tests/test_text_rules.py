"""Shared T03-A corpus and byte/legacy boundaries; no model or Widgets needed."""
import json
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT/'shared'), str(ROOT/'prototype/b01')]
from translator_core import CoreError
from translator_core.contracts import MAX_INPUT_BYTES, foundation_json, recipe_binding
from translator_core.planning import platform_boundaries, split
from translator_core.store import TaskStore
from translator_core.tasks import TextTasks
from translator_core.text_rules import (EXPLANATION, TRANSLATION, _backend, analyze_text,
    assess_new_text, prepare_short_explanation, rule_identity)
from test_text_core import fake_result, simple_boundaries

CORPUS = json.loads((ROOT/'fixtures/t03a/text-rules-v1.json').read_bytes())['cases']


class TextRulesChecks(unittest.TestCase):
    def test_shared_literal_corpus_and_capability_gate(self):
        for case in CORPUS:
            with self.subTest(case=case['id']):
                source = case['source']; raw = source.encode()
                assessment = assess_new_text(source, case['direction'])
                actual = asdict(assessment.analysis); actual.pop('identity'); actual.pop('direction')
                self.assertEqual(actual, case['expected'])
                self.assertEqual(assessment.execution_kind, 'none' if case['expected']['is_empty'] else TRANSLATION)
                self.assertFalse(assessment.explanation_available)
                self.assertEqual(source.encode(), raw)

    def test_unknown_backend_is_explicit_failure_without_fallback(self):
        _backend.cache_clear()
        try:
            with patch('PySide6.QtCore.qVersion', return_value='6.99.0'):
                with self.assertRaises(CoreError) as failure: analyze_text('hello', 'en-zh')
                self.assertEqual(failure.exception.code, 'text_counting_incompatible')
        finally:
            _backend.cache_clear()
        self.assertEqual(rule_identity().qt, '6.10.2')
        with self.assertRaises(CoreError): analyze_text('hello', 'auto')

    def test_single_unit_preparation_binds_raw_input_direction_rules_and_fixed_bytes(self):
        source = ' \r\ne\u0301 👨‍👩‍👧‍👦 𠀀\r\n '
        # Controlled opaque bytes, not an approved/persisted explanation recipe.
        config = b'fixture-only-configuration-v1'
        p = prepare_short_explanation(source, 'en-zh', fixed_configuration=config)
        self.assertEqual(p.source_utf8, source.encode())
        self.assertEqual((p.utf8_start, p.utf8_end), (0, len(source.encode())))
        self.assertEqual(p.analysis.target_kind, EXPLANATION)
        self.assertEqual(p.binding, prepare_short_explanation(source, 'en-zh', fixed_configuration=config).binding)
        for other in (prepare_short_explanation(source.replace('\r\n','\n'),'en-zh',fixed_configuration=config),
                      prepare_short_explanation(source,'zh-en',fixed_configuration=config),
                      prepare_short_explanation(source,'en-zh',fixed_configuration=config+b'2')):
            self.assertNotEqual(p.binding, other.binding)
        from dataclasses import replace
        original = analyze_text(source, 'en-zh')
        with patch('translator_core.text_rules.analyze_text', return_value=replace(original, identity=replace(original.identity,rule='future-rule'))):
            self.assertNotEqual(p.binding,prepare_short_explanation(source,'en-zh',fixed_configuration=config).binding)
        for text in ('', 'a '*21):
            with self.assertRaises(CoreError) as failure:
                prepare_short_explanation(text, 'en-zh', fixed_configuration=config)
            self.assertEqual(failure.exception.code, 'short_explanation_ineligible')

    def test_few_graphemes_do_not_bypass_input_or_single_unit_capacity(self):
        source = 'e'+'\u0301'*1500
        self.assertEqual(analyze_text(source,'en-zh').graphemes,1)
        with self.assertRaises(CoreError) as failure:
            prepare_short_explanation(source,'en-zh',fixed_configuration=b'fixture')
        self.assertEqual(failure.exception.code,'short_unit_capacity')
        with tempfile.TemporaryDirectory() as tmp:
            with closing(TaskStore(Path(tmp)/'tasks')) as store:
                core = TextTasks(store,platform_boundaries(ROOT/'artifacts/b02-20261004/bin/text-boundaries'))
                core.create(source,'en-zh')
                self.assertEqual(core.run('g',threading.Event(),lambda _:None),'failed')
                self.assertEqual(core.task['text']['segments'],[])
                self.assertIn(source,core.export())
                huge = 'e'+'\u0301'*(MAX_INPUT_BYTES//2)
                self.assertEqual(analyze_text(huge,'en-zh').graphemes,1)
                with self.assertRaises(CoreError): core.create(huge,'en-zh')
                self.assertEqual(core.task['input'],source)

    def test_complete_input_decision_never_reapplied_to_small_packed_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            with closing(TaskStore(Path(tmp)/'tasks')) as store:
                calls=[]
                class Engine:
                    def __init__(self,**_): pass
                    def translate(self,source,recipe):
                        calls.append((source,foundation_json(recipe)))
                        return fake_result(source,recipe)
                core=TextTasks(store,simple_boundaries,Engine)
                core.create('hello '*50,'en-zh')
                recipe=foundation_json(core.task['configuration'])
                self.assertEqual(core.last_text_creation['assessment'].analysis.target_kind,TRANSLATION)
                core._plan=lambda cancel: split(core.task['input'],simple_boundaries,cancel,80)
                self.assertEqual(core.run('g',threading.Event(),lambda _:None),'completed')
                self.assertGreater(len(calls),1)
                for source,used in calls:
                    self.assertEqual(analyze_text(source,'en-zh').target_kind,EXPLANATION)
                    self.assertEqual(used,recipe)
                self.assertEqual(foundation_json(core.task['configuration']),recipe)

    def test_legacy_complete_planned_and_unplanned_tasks_keep_saved_identity(self):
        old=ROOT/'fixtures/b02/shared-task/11111111-2222-4333-8444-555555555555'
        before={p.name:p.read_bytes() for p in old.iterdir()}
        with tempfile.TemporaryDirectory() as tmp:
            with closing(TaskStore(Path(tmp)/'tasks')) as store:
                calls=[]
                class NoReplan:
                    splitter_version='controlled-other-adapter'
                    def __call__(self,*_): raise AssertionError('Must not replan')
                class Engine:
                    def __init__(self,**_): pass
                    def translate(self,source,recipe):
                        calls.append(source);return fake_result(source,recipe)
                core=TextTasks(store,NoReplan(),Engine)
                task=core.import_copy(old)['task']; recipe=foundation_json(task['configuration'])
                prefix=foundation_json(task['text']['segments'][0])
                plan=[(s['source'],s['utf8Start'],s['utf8End']) for s in task['text']['segments']]
                self.assertIsNone(core.last_text_creation)
                self.assertEqual(core.run('g',threading.Event(),lambda _:None),'completed')
                self.assertEqual(foundation_json(core.task['text']['segments'][0]),prefix)
                self.assertEqual(foundation_json(core.task['configuration']),recipe)
                self.assertEqual([(s['source'],s['utf8Start'],s['utf8End']) for s in core.task['text']['segments']],plan)
                saved=(store.directory(task['id'])/'current.json').read_bytes(); count=len(calls)
                core.open(task['id']);core.export()
                core.run('already-complete',threading.Event(),lambda _:None)
                self.assertEqual(len(calls),count)
                self.assertEqual(recipe_binding(core.task['configuration']),recipe_binding(task['configuration']))
                # Already-complete explicit run can save status; read-only open/export do not.
                core.open(task['id']); on_disk=(store.directory(task['id'])/'current.json').read_bytes()
                core.export();self.assertEqual((store.directory(task['id'])/'current.json').read_bytes(),on_disk)
                self.assertTrue(saved)
                core.boundaries=simple_boundaries;core.create('Old short draft.','en-zh')
                core.boundaries=NoReplan();sealed=foundation_json(core.task)
                with self.assertRaises(CoreError) as failure: core.run('no-plan',threading.Event(),lambda _:None)
                self.assertEqual(failure.exception.code,'boundary_adapter_mismatch')
                self.assertEqual(foundation_json(core.task),sealed)
        self.assertEqual(before,{p.name:p.read_bytes() for p in old.iterdir()})


if __name__=='__main__': unittest.main(verbosity=2)
