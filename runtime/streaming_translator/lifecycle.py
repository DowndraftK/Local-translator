"""Release a worker's speech state only after its inference/drain has ended.

This module never imports a GPU framework for a read/export/translation worker.
The caller queues release on the worker's one-thread speech executor, after
AudioProcessor cleanup. It must not be used while recognition is active.
"""
import gc
import sys
import time

from .telemetry import process_metrics


def install_encoder_lifetime_adapter(backend, mx):
    """The pinned fast-encoder route calls only model.encoder, never decoder.

    Its original loader constructs/evaluates a complete Whisper container,
    including a random FP32 MLX decoder. The real decoder is independently
    loaded in PyTorch/MPS. Drop only that unused MLX child before Torch loads;
    the original loader, encoder weights/dtype and decoder loader stay intact.
    """
    evidence = {}
    original = backend.load_mlx_encoder
    if getattr(original, '_worker_encoder_lifetime_adapter', False):
        return original._evidence

    def encoder_only(*args, **kwargs):
        model = original(*args, **kwargs)
        before = mx.get_active_memory()
        model.decoder = None
        gc.collect()
        mx.synchronize()
        mx.clear_cache()
        evidence.update(mlx_active_before_bytes=before,
                        mlx_active_after_bytes=mx.get_active_memory(),
                        unused_mlx_decoder_released=True)
        return model

    encoder_only._worker_encoder_lifetime_adapter = True
    encoder_only._evidence = evidence
    backend.load_mlx_encoder = encoder_only
    return evidence


def framework_metrics():
    values = {}
    torch = sys.modules.get('torch')
    if torch is not None and torch.backends.mps.is_available():
        values['mps_current_allocated_bytes'] = torch.mps.current_allocated_memory()
        values['mps_driver_allocated_bytes'] = torch.mps.driver_allocated_memory()
    mx = sys.modules.get('mlx.core')
    if mx is not None:
        for key, name in [('mlx_active_bytes', 'get_active_memory'),
                          ('mlx_cache_bytes', 'get_cache_memory'),
                          ('mlx_peak_bytes', 'get_peak_memory')]:
            method = getattr(mx, name, None)
            if method is not None:
                values[key] = method()
    return values


def release_speech_resources():
    started = time.monotonic()
    before = {**process_metrics(), **framework_metrics()}
    # The App uses one recognition invocation per worker process. After that
    # invocation and all its processor tasks end, the singleton has no client.
    core = sys.modules.get('whisperlivekit.core')
    if core is not None:
        core.TranscriptionEngine.reset()
    refinement = sys.modules.get('mlx_whisper.transcribe')
    if refinement is not None:
        refinement.ModelHolder.model = None
        refinement.ModelHolder.model_path = None
    for name in ('whisperlivekit.whisper.audio', 'whisperlivekit.whisper.tokenizer',
                 'mlx_whisper.audio', 'mlx_whisper.tokenizer'):
        module = sys.modules.get(name)
        if module is not None:
            for key in ('mel_filters', 'get_encoding', 'get_tokenizer'):
                clear = getattr(getattr(module, key, None), 'cache_clear', None)
                if clear is not None:
                    clear()
    gc.collect()
    torch = sys.modules.get('torch')
    if torch is not None and torch.backends.mps.is_available():
        torch.mps.synchronize()
        torch.mps.empty_cache()
    mx = sys.modules.get('mlx.core')
    if mx is not None:
        mx.synchronize()
        mx.clear_cache()
    gc.collect()
    return {'before': before, 'after': {**process_metrics(), **framework_metrics()},
            'release_seconds': time.monotonic()-started,
            'scope': 'worker-owned speech models after drained inference; shared Ollama untouched'}
