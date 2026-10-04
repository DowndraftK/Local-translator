"""Explicit bootstrap for an isolated, development-only B01 candidate."""
import sys
import os
from pathlib import Path

# -I ignores cwd/PYTHONPATH; only this reviewed package directory is added.
sys.path.insert(0, str(Path(__file__).resolve().parent))
for key in tuple(os.environ):
    if key.startswith(("PYTHON", "CONDA", "DYLD", "QT_", "QML")) or key in ("VIRTUAL_ENV", "LD_LIBRARY_PATH"):
        os.environ.pop(key, None)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        from translator_preview.worker import main
        main(sys.argv[2:])
    else:
        from translator_preview.ui import main
        main(sys.argv[1:])
