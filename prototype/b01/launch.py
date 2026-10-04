"""Explicit bootstrap for an isolated, development-only B01 candidate."""
import sys
import os
from pathlib import Path

# -I ignores cwd/PYTHONPATH; only this reviewed package directory is added.
sys.path.insert(0, str(Path(__file__).resolve().parent))
# Built candidates carry the shared package beside this bootstrap. Development
# uses the same package in the repository, never arbitrary PYTHONPATH.
shared_path = Path(__file__).resolve().parent / "shared"
if not shared_path.is_dir():
    shared_path = Path(__file__).resolve().parents[2] / "shared"
sys.path.insert(0, str(shared_path))
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
