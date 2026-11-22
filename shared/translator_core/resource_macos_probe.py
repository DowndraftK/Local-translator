"""Reviewed disposable helper: imports and device queries, never constructors."""
import importlib
import importlib.metadata
import json
import os
import platform
import resource
import sys
from pathlib import Path


def main():
    request = json.loads(Path(sys.argv[1]).read_bytes())
    root = Path(request["runtime"])
    assert sys.flags.isolated and sys.dont_write_bytecode and sys.flags.no_user_site
    assert platform.machine() == "arm64" and platform.python_version() == "3.12.14"
    assert Path(sys.prefix).resolve() == root / "Python"
    protected = tuple(str(Path(p)) + "/" for p in request["model_roots"])

    def deny_inference_network(event, args):
        if event in ("socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.bind"):
            raise RuntimeError("No network permitted in resource probe")
        if event == "open" and isinstance(args[0], (str, bytes)):
            path = os.fsdecode(args[0])
            if path.startswith(protected):
                raise RuntimeError("Model/asset data may not be opened by a no-model probe")
    sys.addaudithook(deny_inference_network)
    sys.path.insert(0, str(root / "Code/source"))
    expected = request["dependencies"]
    versions = {}
    for name, version in expected.items():
        actual = importlib.metadata.version(name)
        if actual != version: raise RuntimeError("Dependency version mismatch: " + name)
        versions[name] = actual
    origins, errors = {}, {}
    names = ("onnxruntime", "torch", "mlx.core", "av", "soundfile", "tiktoken", "mlx_whisper", "whisperlivekit.core")
    for name in names:
        try:
            module = importlib.import_module(name)
            origin = Path(module.__file__).resolve(strict=True)
            if not origin.is_relative_to(root): raise RuntimeError("External import origin")
            origins[name] = str(origin)
            if name == "onnxruntime": module.disable_telemetry_events()
        except Exception as exc:
            errors[name] = type(exc).__name__ + ": " + str(exc)[:512]
    torch, mx = sys.modules.get("torch"), sys.modules.get("mlx.core")
    mps = bool(torch and torch.backends.mps.is_available())
    mlx_gpu = bool(mx and mx.default_device() == mx.gpu)
    core = sys.modules.get("whisperlivekit.core")
    if core:
        assert core.TranscriptionEngine._instance is None
    transcribe = sys.modules.get("mlx_whisper.transcribe")
    if transcribe:
        assert transcribe.ModelHolder.model is None
    print(json.dumps(dict(version="mac-speech-import-probe-v1", executable=sys.executable,
        prefix=sys.prefix, python=platform.python_version(), machine=platform.machine(),
        os_release=platform.release(), isolated=True, bytecode_disabled=True,
        versions=versions, origins=origins, errors=errors, mps=mps, mlx_gpu=mlx_gpu,
        models_loaded=False, inference=False, audio_opened=False,
        network="OS-denied-and-audit-denied", peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
        ensure_ascii=False))


if __name__ == "__main__": main()
