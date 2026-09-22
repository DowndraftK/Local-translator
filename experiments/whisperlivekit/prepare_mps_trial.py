"""Create an isolated, guarded patch of the reviewed WLK source snapshot."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import shutil


def prepare(source, destination):
    source, destination = source.resolve(), destination.resolve()
    if destination.exists():
        raise SystemExit(f"Destination already exists; choose a new directory: {destination}")
    metadata = json.loads(Path(__file__).with_name("review-metadata.json").read_text())
    for name, digest in metadata["source_sha256"].items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise SystemExit(f"Source differs from reviewed snapshot: {name}")
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    changes = {}

    def replace(path, old, new):
        file = destination / path
        text = file.read_text()
        if text.count(old) != 1:
            raise RuntimeError(f"Expected exactly one replacement in {path}")
        changes.setdefault(path, text)
        file.write_text(text.replace(old, new))

    replace("whisperlivekit/config.py", "    def __post_init__(self):\n",
        '    decoder_device: str = "auto"\n    decoder_dtype: str = "float32"\n\n'
        '    def __post_init__(self):\n'
        '        if self.decoder_device not in ("auto", "cpu", "cuda", "mps"):\n'
        '            raise ValueError("Invalid decoder_device")\n'
        '        if self.decoder_dtype not in ("float32", "float16"):\n'
        '            raise ValueError("Invalid decoder_dtype")\n')
    replace("whisperlivekit/parse_args.py", '    parser.add_argument(\n        "--model",',
        '    parser.add_argument("--decoder-device", choices=["auto", "cpu", "cuda", "mps"], default="auto")\n'
        '    parser.add_argument("--decoder-dtype", choices=["float32", "float16"], default="float32")\n'
        '    parser.add_argument(\n        "--model",')
    replace("whisperlivekit/core.py", '                simulstreaming_params = {\n',
        '                simulstreaming_params = {\n'
        '                    "decoder_device": config.decoder_device,\n'
        '                    "decoder_dtype": config.decoder_dtype,\n')
    replace("whisperlivekit/simul_whisper/backend.py", '        whisper_model = load_model(\n',
        '        device = getattr(self, "decoder_device", "auto")\n'
        '        if device == "auto":\n'
        '            device = "cuda" if torch.cuda.is_available() else "cpu"\n'
        '        if device == "mps" and not torch.backends.mps.is_available():\n'
        '            raise RuntimeError("MPS requested but unavailable; refusing CPU fallback")\n'
        '        dtype = getattr(torch, getattr(self, "decoder_dtype", "float32"))\n'
        '        whisper_model = load_model(\n'
        '            device=device,\n')
    replace("whisperlivekit/simul_whisper/backend.py", '        warmup_audio = load_file(self.warmup_file)\n        if warmup_audio is not None:\n            warmup_audio = torch.from_numpy',
        '        whisper_model = whisper_model.to(dtype=dtype).eval()\n'
        '        logger.info("Decoder execution device=%s dtype=%s", whisper_model.device, dtype)\n'
        '        warmup_audio = load_file(self.warmup_file)\n        if warmup_audio is not None:\n            warmup_audio = torch.from_numpy')
    replace("whisperlivekit/simul_whisper/simul_whisper.py", "        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'\n",
        '        self.device = loaded_model.device\n')
    replace("whisperlivekit/simul_whisper/simul_whisper.py", '        return encoder_feature, content_mel_len\n',
        '        encoder_feature = encoder_feature.to(\n'
        '            device=self.model.device, dtype=next(self.model.parameters()).dtype,\n'
        '        )\n'
        '        return encoder_feature, content_mel_len\n')
    # Session failures must reach AudioProcessor instead of becoming empty text.
    replace("whisperlivekit/simul_whisper/backend.py",
        '            logger.exception(f"SimulStreaming processing error: {e}")\n            return [], self.end\n',
        '            logger.exception(f"SimulStreaming processing error: {e}")\n            raise\n')
    patch = "".join("".join(difflib.unified_diff(
        old.splitlines(keepends=True), (destination/path).read_text().splitlines(keepends=True),
        fromfile="a/"+path, tofile="b/"+path)) for path, old in changes.items())
    (destination.parent / "mps.patch").write_text(patch)
    (destination.parent / "patched-source.json").write_text(json.dumps({
        "upstream_commit": metadata["commit"],
        "patched_files": {p: hashlib.sha256((destination/p).read_bytes()).hexdigest() for p in changes},
    }, indent=2)+"\n")
    print(f"Prepared {destination}; patched {len(changes)} files")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source, args.destination)
