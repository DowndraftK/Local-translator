"""Launch the actual owned interpreter, retaining only its controlled venv.

Windows' venv python.exe is a redirector with a second, unowned child PID.
CPython's own __PYVENV_LAUNCHER__ contract lets the pinned base executable
enter that same venv directly, including with -I, without changing sys.path.
"""
import os
import sys
from pathlib import Path


def owned_python():
    if sys.platform == "win32" and sys.prefix != sys.base_prefix:
        executable = Path(sys._base_executable).resolve(strict=True)
        if not executable.is_relative_to(Path(sys.base_prefix).resolve()):
            raise RuntimeError("The owned interpreter must belong to the controlled base runtime")
        return str(executable)
    return sys.executable


def owned_environment():
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.startswith(("PYTHON", "CONDA", "DYLD", "QT_", "QML")) or key in (
                "VIRTUAL_ENV", "LD_LIBRARY_PATH", "__PYVENV_LAUNCHER__"):
            environment.pop(key, None)
    if sys.platform == "win32" and sys.prefix != sys.base_prefix:
        environment["__PYVENV_LAUNCHER__"] = sys.executable
    return environment
