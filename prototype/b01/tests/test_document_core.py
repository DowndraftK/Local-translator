"""Shared B03 invariants with controlled engines; no claim of real PDF/OCR here."""
import base64
import json
import shutil
import sys
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
import uuid

P = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(P / "shared"), str(P / "prototype/b01")]
from translator_core import CoreError
from translator_core.contracts import decode_snapshot, encode_snapshot, foundation_json, sha
from translator_core.document_tasks import DocumentTasks
from translator_core.documents import page_range, validate_document_task
from translator_core.store import TaskStore, atomic_write, atomic_copy, file_identity
from test_text_core import simple_boundaries, fake_result


class Boundaries:
    splitter_version = "natural-language-utf8-v1"
    def __call__(self, source, cancel): return simple_boundaries(source, cancel)


class Facts:
    identity = "controlled-pdf-facts-v1"
    def __init__(self, total=220): self.total = total; self.calls = []; self.text = {}; self.wrong = False
    def metadata(self, source, fingerprint, cancel): return {"totalPages":self.total}
    def page(self, source, fingerprint, page, cancel):
        self.calls.append(page)
        return dict(page=page+1 if self.wrong else page, label=f"print-{page}",
                    text=self.text.get(page, f"Physical page {page}. Repeat. Repeat.\nEND-{page}\n"))
    def render(self, source, fingerprint, page, cancel):
        return dict(page=page, fingerprint=fingerprint, no_inference=True)


class DocumentCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="b03-controlled-"); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name); self.store = TaskStore(self.base / "tasks"); self.addCleanup(self.store.close)
        self.facts = Facts(); self.calls = []
        owner = self
        class Engine:
            def __init__(self, cancel): pass
            def cancel(self): pass
            def translate(self, source, recipe): owner.calls.append(source); return fake_result(source, recipe)
        self.core = DocumentTasks(self.store, Boundaries(), self.facts, engine_factory=Engine)
        self.source = self.base / "controlled.pdf"; self.source.write_bytes(b"controlled opaque source; simulated PDF facts, not real extraction")

    def document(self, first=1, last=12):
        selection = self.core.select_document(str(self.source), "select", threading.Event())
        self.core.create_document(selection["token"], first, last, "en-zh")
        return self.core.task

    def extract(self, changed=lambda _:None, cancel=None):
        return self.core.extract("extract", cancel or threading.Event(), changed)

    def test_continuous_physical_range_accepts_201_to_220(self):
        task = self.document(201,220); self.assertEqual(self.extract(),"completed")
        self.assertEqual(self.facts.calls,list(range(201,221)))
        self.assertEqual([p["id"] for p in task["document"]["pages"]],list(range(201,221)))
        self.assertIn("END-220",self.core.export()); self.assertIn("201–220",self.core.export())

    def test_ranges_reject_reverse_outside_and_over_200_without_clipping(self):
        for first,last,total in [(5,3,220),(0,2,220),(219,221,220),(1,201,220),(1,220,220),(True,1,2)]:
            with self.subTest(first=first,last=last),self.assertRaises(CoreError):page_range(first,last,total)
        self.assertEqual(page_range(3,5,12),{"first":3,"last":5})

    def test_manifest_and_export_include_all_12_and_last_unique_marker(self):
        task=self.document();self.extract()
        self.assertEqual(self.facts.calls,list(range(1,13)))
        out=self.core.export()
        for p in task["document"]["pages"]:
            self.assertIn(p["text"],out);self.assertIn("PDF物理页 "+str(p["id"]),out)
        self.assertEqual(out.count("END-12"),1)
        self.assertEqual(task["document"]["pages"][0]["text"].count("Repeat."),2)

    def test_no_text_pages_keep_identity_and_zero_text_cannot_translate(self):
        self.facts.text={1:" \n",2:""};task=self.document(1,2);self.extract()
        self.assertEqual([p["state"] for p in task["document"]["pages"]],["noText","noText"])
        with self.assertRaises(CoreError) as error:self.core.run("translate",threading.Event(),lambda _:None)
        self.assertEqual(error.exception.code,"zero_translatable_text");self.assertFalse(self.calls)
        self.assertIn("未获得可翻译原文",self.core.export())

    def test_cancelled_extraction_preserves_completed_pages_and_resumes_only_missing(self):
        task=self.document();cancel=threading.Event()
        def changed(snapshot):
            if snapshot["task"]["document"]["pages"][2]["state"]=="extracted":cancel.set()
        self.assertEqual(self.extract(changed,cancel),"cancelled")
        prefix=foundation_json(task["document"]["pages"][:3]);self.assertEqual(self.facts.calls,[1,2,3])
        raw=(self.store.directory(task["id"])/"current.json").read_bytes()
        self.core.open(task["id"]);self.assertEqual(raw,(self.store.directory(task["id"])/"current.json").read_bytes())
        self.assertEqual(self.extract(),"completed");self.assertEqual(self.facts.calls,list(range(1,13)))
        self.assertEqual(prefix,foundation_json(self.core.task["document"]["pages"][:3]))

    def test_wrong_page_response_is_failure_not_text_or_success(self):
        self.facts.wrong=True;task=self.document(3,5);self.assertEqual(self.extract(),"partial")
        self.assertEqual([p["state"] for p in task["document"]["pages"]],["failed"]*3)
        self.assertTrue(all(p["text"]=="" for p in task["document"]["pages"]))

    def test_backend_mismatch_keeps_read_export_but_refuses_missing_extraction(self):
        task=self.document(3,5);self.facts.identity="different-backend"
        with self.assertRaises(CoreError) as error:self.extract()
        self.assertEqual(error.exception.code,"extraction_adapter_mismatch")
        self.assertIn("PDF物理页 5",self.core.export());self.assertFalse(self.facts.calls)

    def test_source_move_does_not_break_sealed_task_or_original_page_reading(self):
        selection=self.core.select_document(str(self.source),"select",threading.Event())
        self.source.rename(self.base/"moved.pdf")
        task=self.core.create_document(selection["token"],3,5,"en-zh")["task"]
        self.extract();current=self.store.directory(task["id"])/"current.json";before=current.read_bytes()
        self.assertEqual(self.core.render(5,threading.Event())["page"],5)
        self.assertEqual(before,current.read_bytes())

    def test_missing_or_tampered_source_never_falls_back_to_original_path(self):
        for missing in (False,True):
            with self.subTest(missing=missing):
                task=self.document(1,2);self.extract();source=self.store.directory(task["id"])/task["asset"]["filename"]
                source.unlink() if missing else source.write_bytes(b"tampered")
                opened=self.core.open(task["id"]);self.assertTrue(opened["save_blocked"])
                self.assertIn("END-2",self.core.export())
                with self.assertRaises(CoreError):self.extract()
                with self.assertRaises(CoreError):self.core.render(1,threading.Event())

    def test_utf8_bom_unicode_whitespace_and_duplicate_text_are_exact(self):
        source=self.base/"bom.txt";text=" \t中文 e\u0301 👨‍👩‍👧‍👦\r\nRepeat. Repeat.\nEND\n "
        raw=b"\xef\xbb\xbf"+text.encode();source.write_bytes(raw)
        s=self.core.select_document(str(source),"select",threading.Event());task=self.core.create_document(s["token"],1,1,"zh-en")["task"]
        self.extract();task=self.core.task;self.assertEqual(task["document"]["pages"][0]["text"],text)
        self.assertEqual((self.store.directory(task["id"])/task["asset"]["filename"]).read_bytes(),raw)
        out=self.core.export();self.assertIn("TXT全文",out);self.assertNotIn("PDF物理页 1",out);self.assertIn(text,out)

    def test_non_utf8_and_utf16_without_bom_fail_explicitly(self):
        for raw in (b"\xff\xfeA\0",b"\x80",b"A\0B\0"):
            with self.subTest(raw=raw),self.assertRaises(CoreError):
                p=self.base/"bad.txt";p.write_bytes(raw);self.core.select_document(str(p),"select",threading.Event())

    def test_accumulated_text_limit_retains_prior_page_and_all_unprocessed_pages(self):
        self.facts.text={1:"A"*600000,2:"B"*600000};task=self.document(1,3)
        self.assertEqual(self.extract(),"failed")
        self.assertEqual([p["state"] for p in task["document"]["pages"]],["extracted","failed","stopped"])
        self.assertIn("A"*600000,self.core.export());self.assertNotIn("B"*600000,self.core.export())

    def test_translation_plan_is_page_local_and_success_prefix_unchanged_on_resume(self):
        task=self.document(3,5);self.extract();cancel=threading.Event()
        def changed(snapshot):
            parts=(snapshot["task"].get("text") or {}).get("segments",[])
            if parts and parts[0]["state"]=="completed":cancel.set()
        self.assertEqual(self.core.run("run1",cancel,changed),"cancelled")
        before=foundation_json(task["text"]["segments"][0]);plan=foundation_json(task["document"]["segments"])
        self.core.open(task["id"]);self.calls.clear()
        self.assertEqual(self.core.run("run2",threading.Event(),lambda _:None),"completed")
        self.assertEqual(len(self.calls),2);self.assertEqual(before,foundation_json(self.core.task["text"]["segments"][0]))
        self.assertEqual(plan,foundation_json(self.core.task["document"]["segments"]))
        for m in task["document"]["sources"]:self.assertEqual(m["utf8Start"],0)

    def test_completed_plan_cannot_be_attached_to_changed_page_revision(self):
        task=self.document(3,5);self.extract();self.core.run("run",threading.Event(),lambda _:None)
        corrupt=deepcopy(task);corrupt["document"]["pages"][0]["revision"]=str(uuid.uuid4()).upper()
        with self.assertRaises(CoreError):encode_snapshot(corrupt)

    def test_atomic_save_failure_stops_next_page_and_keeps_durable_current(self):
        task=self.document(1,3);directory=self.store.directory(task["id"]);before=(directory/"current.json").read_bytes()
        def fail(data,path):
            if path.name=="current.json":raise OSError("controlled no-space")
            atomic_write(data,path)
        self.store.write=fail;self.assertEqual(self.extract(),"failed")
        self.assertEqual((directory/"current.json").read_bytes(),before);self.assertFalse(self.facts.calls)
        self.assertTrue(self.core.save_blocked);self.assertIn("PDF物理页 3",self.core.export())

    def test_future_schema_does_not_fall_back_and_previous_corruption_is_read_only(self):
        task=self.document(1,2);self.extract();directory=self.store.directory(task["id"])
        good=(directory/"current.json").read_bytes();(directory/"current.json").write_bytes(b"broken")
        self.assertTrue(self.core.open(task["id"])["save_blocked"])
        self.assertEqual((directory/"current.json").read_bytes(),b"broken")
        envelope=json.loads(good);envelope["schema"]=99;(directory/"current.json").write_text(json.dumps(envelope))
        with self.assertRaises(CoreError) as error:self.core.open(task["id"])
        self.assertEqual(error.exception.code,"future_schema")

    def test_cancelled_late_page_not_committed_and_no_next_page_started(self):
        task=self.document(1,3);entered=threading.Event();release=threading.Event();cancel=threading.Event()
        def late(*args):entered.set();release.wait(5);return {"page":1,"text":"LATE-UNSAFE"}
        self.facts.page=late
        thread=threading.Thread(target=lambda:self.core.extract("old",cancel,lambda _:None));thread.start()
        self.assertTrue(entered.wait(2));self.core.cancel("old")
        with self.assertRaises(CoreError):self.core.create("replacement","en-zh")
        release.set();thread.join(2);self.assertFalse(thread.is_alive())
        self.assertTrue(all(p["text"]=="" for p in task["document"]["pages"]));self.assertNotIn("LATE-UNSAFE",self.core.export())

    def test_two_writers_cannot_acquire_document_task_root(self):
        with self.assertRaises(CoreError) as error:TaskStore(self.store.root)
        self.assertEqual(error.exception.code,"writer_busy")

    def test_stream_copy_checks_hash_before_publishing_and_keeps_existing_target(self):
        target=self.base/"target.pdf";target.write_bytes(b"keep")
        with self.assertRaises(CoreError):atomic_copy(self.source,target,self.source.stat().st_size,"0"*64)
        self.assertEqual(target.read_bytes(),b"keep");self.assertFalse(list(self.base.glob('.copy-*')))

    def test_production_swift_legacy_document_and_ocr_plans_are_read_only_then_resume_exactly(self):
        source=json.loads((P/"fixtures/b03/source-bytes.json").read_bytes());raw=base64.b64decode(source["base64"]);self.assertEqual(sha(raw),source["sha256"])
        for frozen in sorted((P/"fixtures/b03/legacy").iterdir()):
            imported=self.base/frozen.name;shutil.copytree(frozen,imported);(imported/"source.pdf").write_bytes(raw)
            originals={p.name:sha(p.read_bytes()) for p in imported.iterdir()}
            self.core.import_copy(imported);task=self.core.task
            prefix=foundation_json(task["text"]["segments"][0]);recipe=foundation_json(task["configuration"]);plan=foundation_json(task["document"]["segments"])
            self.assertNotIn("contextVersion",task["configuration"])
            if task["drafts"]:
                with self.assertRaises(CoreError):self.core.run("draft-block",threading.Event(),lambda _:None)
                # This is a controlled fixture copy: explicit discard for the continuation check.
                task["drafts"]={}
            self.assertEqual(self.core.run("legacy",threading.Event(),lambda _:None),"completed")
            self.assertEqual(prefix,foundation_json(task["text"]["segments"][0]));self.assertEqual(recipe,foundation_json(task["configuration"]));self.assertEqual(plan,foundation_json(task["document"]["segments"]))
            self.assertEqual(originals,{p.name:sha(p.read_bytes()) for p in imported.iterdir()})
