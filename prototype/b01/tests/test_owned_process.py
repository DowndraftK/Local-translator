import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1])]
from translator_preview.owned_process import OwnedWindowsProcess


@unittest.skipUnless(sys.platform == "win32", "Windows Job Object needs native Windows")
class OwnershipTests(unittest.TestCase):
    def test_job_close_reaps_child_even_when_parent_is_stopped(self):
        import ctypes
        from ctypes import wintypes as W
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]; k.OpenProcess.restype = W.HANDLE
        k.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]; k.WaitForSingleObject.restype = W.DWORD
        k.CloseHandle.argtypes = [W.HANDLE]
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); marker = root / "child-pid.txt"
            source = "import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(120)"
            with (root / "out").open("wb") as out, (root / "err").open("wb") as err:
                process = OwnedWindowsProcess([sys.executable, "-I", "-B", "-c", source, str(marker)],
                                              env=dict(os.environ), stdout=out, stderr=err)
            handle = None
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline: time.sleep(.05)
                self.assertTrue(marker.exists(), (root / "err").read_text())
                handle = k.OpenProcess(0x100000, False, int(marker.read_text()))
                self.assertTrue(handle)
                process.close()
                self.assertEqual(k.WaitForSingleObject(handle, 5000), 0)
            finally:
                process.close()
                if handle: k.CloseHandle(handle)
