"""Signed App entry point for the managed interpreter's isolated mode."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from streaming_translator.__main__ import main

try:
    raise SystemExit(main())
except (ValueError, OSError) as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(1)
