"""Exercise the real CPU VAD dependency and interpreter teardown, without GPU."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_onnx_vad_session_exits_after_disabling_telemetry():
    root = Path(__file__).resolve().parents[2]
    model = root / ('artifacts/whisperlivekit-speech-repair-20261001-final/source/'
                    'whisperlivekit/silero_vad_models/silero_vad.onnx')
    if not model.is_file():
        pytest.skip('Requires the existing local pinned VAD model; never download it')
    code = '''
import sys
from streaming_translator.asr import prepare_vad_runtime
prepare_vad_runtime()
import onnxruntime as ort
import numpy as np
options = ort.SessionOptions()
options.intra_op_num_threads = options.inter_op_num_threads = 1
session = ort.InferenceSession(sys.argv[1], sess_options=options,
                              providers=['CPUExecutionProvider'])
state = np.zeros((2, 1, 128), dtype=np.float32)
for _ in range(20):
    result, state = session.run(None, {
        'input': np.zeros((1, 576), dtype=np.float32),
        'state': state, 'sr': np.array(16000, dtype=np.int64)})
    assert np.isfinite(result).all() and np.isfinite(state).all()
del session
print('VAD session completed')
'''
    child = subprocess.run([sys.executable, '-c', code, str(model)],
                           env={**os.environ, 'PYTHONPATH': str(root / 'runtime'),
                                'PYTHONDONTWRITEBYTECODE': '1'},
                           capture_output=True, text=True, timeout=20)
    assert child.returncode == 0, child.stderr
    assert 'VAD session completed' in child.stdout
