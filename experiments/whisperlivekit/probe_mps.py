"""Probe WLK decoder operators on Apple GPU using tiny random weights only.

No model download, microphone, network, or application edits. This is an
operator compatibility probe, not a real ASR benchmark or integration patch.
"""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    # Must be set before importing PyTorch. Unsupported ops should be visible.
    os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "0"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))

    import torch

    report = {
        "scope": "random-weight operator probe; no trained model or ASR pipeline",
        "python": platform.python_version(),
        "torch": torch.__version__,
        "mps_built": torch.backends.mps.is_built(),
        "mps_available": torch.backends.mps.is_available(),
        "fallback": os.environ["PYTORCH_ENABLE_MPS_FALLBACK"],
        "model_source_sha256": hashlib.sha256(
            (source / "whisperlivekit/whisper/model.py").read_bytes()
        ).hexdigest(),
        "checks": {},
    }

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)

    if not report["mps_available"]:
        report["status"] = "mps_unavailable_in_this_process"
        save()
        return 2

    from whisperlivekit.whisper.model import ModelDimensions, Whisper
    from whisperlivekit.whisper.timing import median_filter

    torch.manual_seed(7)
    dims = ModelDimensions(n_mels=80, n_audio_ctx=16, n_audio_state=64,
                           n_audio_head=4, n_audio_layer=2, n_vocab=128,
                           n_text_ctx=16, n_text_state=64, n_text_head=4,
                           n_text_layer=2)
    cpu = Whisper(dims, decoder_only=True).eval()
    with torch.no_grad():
        for param in cpu.parameters():
            param.normal_(0, 0.02)
        for module in cpu.modules():
            if isinstance(module, torch.nn.LayerNorm):
                module.weight.fill_(1)
                module.bias.zero_()

    naive = copy.deepcopy(cpu)
    try:
        naive.to("mps")
        torch.mps.synchronize()
        report["checks"]["unmodified_model_to_mps"] = {"status": "passed"}
    except Exception as exc:
        report["checks"]["unmodified_model_to_mps"] = {
            "status": "failed", "error_type": type(exc).__name__,
            "error": str(exc).splitlines()[0][:1200],
        }
    del naive

    token_ids = torch.tensor([[1, 8, 12, 19, 23]], dtype=torch.long)
    features = torch.randn(1, 16, 64)
    with torch.inference_mode():
        reference, reference_attn = cpu.logits(token_ids, features, return_cross_attn=True)

    for label, dtype, atol in [("float32", torch.float32, 0.0002),
                               ("float16", torch.float16, 0.002)]:
        try:
            gpu = copy.deepcopy(cpu)
            gpu.to(device="mps", dtype=dtype)
            # Exercise the original sparse buffer too; do not assume older
            # PyTorch MPS sparse limitations still apply to this installed build.
            head_pairs = [(layer.item(), head.item())
                          for layer, head in gpu.alignment_heads.indices().T]
            expected_pairs = [(layer.item(), head.item())
                              for layer, head in cpu.alignment_heads.indices().T]
            assert head_pairs == expected_pairs
            x = token_ids.to("mps")
            xa = features.to(device="mps", dtype=dtype)
            cache = {}
            with torch.inference_mode():
                actual, attention = gpu.logits(x, xa, return_cross_attn=True)
                gpu.logits(x[:, :3], xa, kv_cache=cache, return_cross_attn=True)
                continuations = [gpu.logits(x[:, i:i+1], xa, kv_cache=cache)
                                 for i in range(3, 5)]
                joined = torch.cat(continuations, dim=1)
            torch.mps.synchronize()
            assert all(p.device.type == "mps" for p in gpu.decoder.parameters())
            assert all(v.device.type == "mps" for v in cache.values())
            assert actual.device.type == "mps"
            assert len(attention) == len(reference_attn) == 2
            assert all(a.device.type == "mps" for a in attention)
            torch.testing.assert_close(actual.cpu(), reference, atol=atol, rtol=0.01)
            torch.testing.assert_close(joined.cpu(), actual[:, 3:].cpu(), atol=atol, rtol=0.01)
            for a, b in zip(attention, reference_attn):
                torch.testing.assert_close(a.cpu(), b, atol=atol, rtol=0.01)
            report["checks"][f"decoder_{label}"] = {
                "status": "passed", "parameter_device": str(next(gpu.decoder.parameters()).device),
                "alignment_metadata_device": str(gpu.alignment_heads.device),
                "alignment_index_iteration": "passed",
                "max_logit_abs_diff_from_cpu_float32": float((actual.cpu() - reference).abs().max()),
                "cached_continuation": "passed", "cross_attention": "passed",
                "cache_tensor_count": len(cache),
            }
            del gpu
        except Exception as exc:
            report["checks"][f"decoder_{label}"] = {
                "status": "failed", "error_type": type(exc).__name__, "error": str(exc)[:1200],
            }

    try:
        values = torch.randn(1, 2, 8, 16)
        actual = median_filter(values.to("mps"), 7)
        torch.mps.synchronize()
        torch.testing.assert_close(actual.cpu(), median_filter(values, 7))
        report["checks"]["alignment_median_filter"] = {"status": "passed", "device": str(actual.device)}
    except Exception as exc:
        report["checks"]["alignment_median_filter"] = {
            "status": "failed", "error_type": type(exc).__name__, "error": str(exc)[:1200],
        }

    required = ["decoder_float32", "decoder_float16", "alignment_median_filter"]
    passed = all(report["checks"][key]["status"] == "passed" for key in required)
    report["status"] = "operator_probe_passed" if passed else "operator_probe_failed"
    save()
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
