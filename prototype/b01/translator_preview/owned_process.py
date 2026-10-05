"""Windows-only ownership for a test service and every child it creates.

Create suspended, assign to a private KILL_ON_JOB_CLOSE job, then resume. No
process-name kill, pre-existing PID adoption, or shared-service unload occurs.
"""
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes as W


class OwnedWindowsProcess:
    def __init__(self, args, *, env, stdout, stderr):
        if sys.platform != "win32":
            raise RuntimeError("Requires native Windows")
        import msvcrt
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k = k
        ptr = ctypes.c_void_p
        signatures = {
            "CreateJobObjectW": ([ptr, W.LPCWSTR], W.HANDLE),
            "SetInformationJobObject": ([W.HANDLE, ctypes.c_int, ptr, W.DWORD], W.BOOL),
            "AssignProcessToJobObject": ([W.HANDLE, W.HANDLE], W.BOOL),
            "CloseHandle": ([W.HANDLE], W.BOOL),
            "ResumeThread": ([W.HANDLE], W.DWORD),
            "TerminateProcess": ([W.HANDLE, W.UINT], W.BOOL),
            "GetCurrentProcess": ([], W.HANDLE),
            "DuplicateHandle": ([W.HANDLE, W.HANDLE, W.HANDLE, ctypes.POINTER(W.HANDLE), W.DWORD, W.BOOL, W.DWORD], W.BOOL),
            "WaitForSingleObject": ([W.HANDLE, W.DWORD], W.DWORD),
            "GetExitCodeProcess": ([W.HANDLE, ctypes.POINTER(W.DWORD)], W.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            fn = getattr(k, name); fn.argtypes = arguments; fn.restype = result

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", W.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", W.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", W.DWORD), ("SchedulingClass", W.DWORD)]
        class Limits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        class Startup(ctypes.Structure):
            _fields_ = [("cb", W.DWORD), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR), ("lpTitle", W.LPWSTR),
                        *[(n, W.DWORD) for n in ("dwX", "dwY", "dwXSize", "dwYSize", "dwXCountChars", "dwYCountChars", "dwFillAttribute", "dwFlags")],
                        ("wShowWindow", W.WORD), ("cbReserved2", W.WORD), ("lpReserved2", ptr),
                        ("hStdInput", W.HANDLE), ("hStdOutput", W.HANDLE), ("hStdError", W.HANDLE)]
        class Info(ctypes.Structure):
            _fields_ = [("hProcess", W.HANDLE), ("hThread", W.HANDLE), ("dwProcessId", W.DWORD), ("dwThreadId", W.DWORD)]
        k.CreateProcessW.argtypes = [W.LPCWSTR, W.LPWSTR, ptr, ptr, W.BOOL, W.DWORD, ptr, W.LPCWSTR, ctypes.POINTER(Startup), ctypes.POINTER(Info)]
        k.CreateProcessW.restype = W.BOOL

        def check(value):
            if not value: raise ctypes.WinError(ctypes.get_last_error())
            return value
        self.job = check(k.CreateJobObjectW(None, None)); self.process = None
        limits = Limits(); limits.BasicLimitInformation.LimitFlags = 0x2000
        inherited = []
        try:
            check(k.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            def duplicate(file):
                handle = W.HANDLE()
                check(k.DuplicateHandle(k.GetCurrentProcess(), msvcrt.get_osfhandle(file.fileno()),
                                        k.GetCurrentProcess(), ctypes.byref(handle), 0, True, 2))
                inherited.append(handle); return handle
            with open(os.devnull, "rb") as null:
                startup = Startup(); startup.cb = ctypes.sizeof(startup); startup.dwFlags = 0x100
                startup.hStdInput = duplicate(null); startup.hStdOutput = duplicate(stdout); startup.hStdError = duplicate(stderr)
                info = Info(); command = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(map(str, args))))
                environment = ctypes.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in sorted(env.items(), key=lambda p: p[0].upper())) + "\0\0")
                check(k.CreateProcessW(str(args[0]), command, None, None, True, 0x4 | 0x400 | 0x08000000,
                                       environment, None, ctypes.byref(startup), ctypes.byref(info)))
            self.process = info.hProcess; self.pid = info.dwProcessId
            try:
                check(k.AssignProcessToJobObject(self.job, self.process))
                if k.ResumeThread(info.hThread) == 0xffffffff: raise ctypes.WinError(ctypes.get_last_error())
            except BaseException:
                k.TerminateProcess(self.process, 1)
                raise
            finally:
                k.CloseHandle(info.hThread)
        except BaseException:
            self.close(); raise
        finally:
            for handle in inherited: k.CloseHandle(handle)

    def poll(self):
        if self.k.WaitForSingleObject(self.process, 0) == 0x102: return None
        code = W.DWORD()
        if not self.k.GetExitCodeProcess(self.process, ctypes.byref(code)): raise ctypes.WinError(ctypes.get_last_error())
        return code.value

    def close(self):
        # Closing the only job handle kills all assigned descendants even after
        # Ollama's parent has exited. Assignment precedes its first instruction.
        if getattr(self, "job", None): self.k.CloseHandle(self.job); self.job = None
        if getattr(self, "process", None):
            if self.k.WaitForSingleObject(self.process, 10000) == 0x102:
                raise RuntimeError("Owned service tree did not exit within 10 seconds")
            self.k.CloseHandle(self.process); self.process = None
