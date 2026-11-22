"""Related existing Mac/core/protocol checks, excluding paused platform work."""
import json
import sys
import time
import unittest
from pathlib import Path

if sys.platform != 'darwin':
    raise SystemExit('This scope runner records Mac evidence only.')
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
# Do not even schedule native/simulated Windows-specific checks in this package.
EXCLUDED={
    'test_owned_process.OwnedWindowsProcessTests.test_job_close_reaps_child_even_when_parent_is_stopped',
    'test_text_core.StoreCase.test_windows_junction_cannot_import_or_replace_external_task',
    'test_text_core.StoreCase.test_qt_unicode_packing_keeps_graphemes_complete_and_source_exact',
    'test_text_core.StoreCase.test_new_adapter_identity_never_rewrites_legacy_plan_or_recipe',
    'test_text_core.StoreCase.test_asset_path_traversal_and_windows_aliases_are_rejected',
    'test_text_core.OllamaCase.test_stream_connection_abort_retains_cancel_or_service_failure_identity',
}
def leaves(suite):
    for item in suite:
        if isinstance(item,unittest.TestSuite):yield from leaves(item)
        else:yield item
all_tests=list(leaves(unittest.defaultTestLoader.discover(str(HERE),pattern='test_*.py')))
excluded=[test.id() for test in all_tests if test.id() in EXCLUDED or test.__class__.__module__=='test_owned_process']
selected=[test for test in all_tests if test.id() not in excluded]
start=time.monotonic()
result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))
print(json.dumps(dict(platform='Mac',run=result.testsRun,failed=len(result.failures),errors=len(result.errors),
    skipped=len(result.skipped),seconds=round(time.monotonic()-start,3),
    excluded_without_execution=excluded,windows='用户暂停，本轮未实施/未验证'),ensure_ascii=False))
raise SystemExit(0 if result.wasSuccessful() else 1)
