"""Validate and summarize the saved September 15 real-model trials; no GPU needed."""
import argparse
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
RUNS = [
    "mps-fp32-paced", "mps-fp16-paced", "cpu-fp32-paced",
    "mps-fp32-paced-repeat", "mps-fp32-pause-repeat",
]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def normalized_words(text):
    # A narrowly scoped fixture comparison, not a general ASR accuracy metric.
    return ["3" if word == "three" else word for word in re.findall(r"\w+", text.lower())]


def relative(path):
    return str(path.resolve().relative_to(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=ROOT / "artifacts/whisperlivekit-mps-20260915")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("mps-trial-results.json"))
    args = parser.parse_args()
    base = args.artifacts.resolve()
    patched = json.loads((base / "patched-source.json").read_text())
    for name, sha in patched["patched_files"].items():
        assert digest(base / "source" / name) == sha, f"Modified source: {name}"
    manifest = json.loads(Path(__file__).with_name("mps-model-manifest.json").read_text())
    reference_path = ROOT / "fixtures/translation-en.txt"
    reference = normalized_words(reference_path.read_text())
    runs = []
    for name in RUNS:
        raw_path = base / f"{name}.json"
        raw = json.loads(raw_path.read_text())
        updates_path = base / f"{name}.updates.jsonl"
        updates = [json.loads(line) for line in updates_path.read_text().splitlines()]
        first = next(r for r in updates if any(line.get("text") for line in r.get("lines", [])))
        cfg, device = raw["configuration"], raw["device_evidence"]
        repetitions = 2 if name.endswith("pause-repeat") else 1
        checks = {
            "completed": raw["status"] == "completed",
            "all_pcm_samples_received": raw["input_samples"] == raw["received_pcm_samples"],
            "no_pipeline_error": not raw["processing_error"] and not raw["overload_error"],
            "empty_transcription_tail": raw["final"]["buffer_transcription"] == "",
            "committed_text_present": bool(raw["text"].strip()),
            "first_commit_matches_update_log": raw["first_committed_text_seconds"] == first["elapsed"],
            "automatic_mps_fallback_disabled": raw["mps_fallback"] == "0",
            "mlx_encoder_gpu": device["mlx_default_device"] == "Device(gpu, 0)",
            "pytorch_decoder_selected": not device["uses_full_mlx_decoder"],
            "decoder_device_matches": device["decoder_parameter_devices"] == ["mps:0" if cfg["decoder_device"] == "mps" else "cpu"],
            "decoder_dtype_matches": device["decoder_parameter_dtypes"] == ["torch." + cfg["decoder_dtype"]],
            "feature_device_matches": device["encoded_feature_device"] == device["alignatt_device"] == device["decoder_parameter_devices"][0],
            "feature_dtype_matches": device["encoded_feature_dtype"] == "torch." + cfg["decoder_dtype"],
        }
        phrase_counts = {p: raw["text"].count(p) for p in [
            "Students must submit", "at least 14 days", "causal relationship",
        ]}
        checks["expected_phrase_repetition_counts"] = all(n == repetitions for n in phrase_counts.values())
        assert all(checks.values()), (name, checks)
        expected, actual = reference * repetitions, normalized_words(raw["text"])
        differences = [
            {"operation": tag, "reference": expected[a:b], "recognized": actual[c:d]}
            for tag, a, b, c, d in SequenceMatcher(None, expected, actual, autojunk=False).get_opcodes()
            if tag != "equal"
        ]
        metrics = raw["metrics"]
        # The first run predates the explicit metrics serialization; keep raw evidence intact.
        total_asr = metrics.get("total_asr_call_seconds", metrics.get("total_processing_time_s"))
        runs.append({
            "name": name, "raw_path": relative(raw_path), "raw_sha256": digest(raw_path),
            "updates_path": relative(updates_path), "updates_sha256": digest(updates_path),
            "audio_path": relative(Path(raw["audio_path"])), "audio_sha256": raw["audio_sha256"],
            **{k: raw[k] for k in ["audio_seconds", "input_samples", "received_pcm_samples",
                "load_and_warmup_seconds", "first_committed_text_seconds", "elapsed_seconds",
                "tail_finalize_seconds", "device_evidence", "text"]},
            "versions": {k: raw[k] for k in ["python", "torch", "mlx"]},
            "decoder_device": cfg["decoder_device"], "decoder_dtype": cfg["decoder_dtype"],
            "first_committed_text": " ".join(line["text"] for line in first["lines"] if line.get("text")).strip(),
            "total_asr_call_seconds": total_asr, "asr_call_seconds_per_audio_second": total_asr / raw["audio_seconds"],
            "n_transcription_calls": metrics["n_transcription_calls"],
            "queue_peak_seconds": raw["queue_peak_samples"] / 16000,
            "checks": checks, "phrase_counts": phrase_counts,
            "normalized_reference_match": not differences, "normalized_word_differences": differences,
            "final_lines": raw["final"]["lines"],
            "power_before": raw.get("power_before"), "power_after": raw.get("power_after"),
        })
        assert digest(Path(raw["audio_path"])) == raw["audio_sha256"], name
        assert cfg["encoder_model_path"] == cfg["decoder_model_path"]
    model = Path(raw["configuration"]["encoder_model_path"])
    for filename, info in manifest["files"].items():
        path = model / filename
        assert path.stat().st_size == info["size"] and digest(path) == info["sha256"], filename
    result = {
        "experiment_date": "2026-09-15", "report_date": "2026-09-16",
        "scope": "Real large-v3-turbo, paced in-process WLK AudioProcessor PCM; no WebSocket, microphone, translation, or Swift app integration",
        "hardware": "Apple M5 Pro, 20 GPU cores (prior system_profiler record)",
        "upstream_commit": patched["upstream_commit"], "patched_files": patched["patched_files"],
        "patch_sha256": digest(base / "mps.patch"), "model_manifest": manifest,
        "fixture_reference": {"path": relative(reference_path), "sha256": digest(reference_path), "words": len(reference)},
        "comparison_normalization": "Lowercase, punctuation ignored via word tokenization, three -> 3; differences are fixture checks, not a corpus WER benchmark",
        "measurement_notes": [
            "The audio clock starts after model load and a 3-second warmup; 0.5-second PCM packets are paced in real time.",
            "First committed text is the single word Students, not a complete English or bilingual sentence.",
            "Accumulated ASR call duration includes encoding, feature conversion, decoding, alignment, and executor dispatch; it is not isolated decoder GPU time.",
            "Input coalescing varies with runtime; run order, caches, and kernel compilation were not controlled.",
            "MPS FP32 repeat approaches the CPU decoder baseline but shows no speed improvement in this short sample.",
            "The initial FP32 run has no recorded power state. Later trials record AC power; no power consumption or memory profiling was performed.",
            "The pause trial has a 0.22-second overlap between estimated silence end and second speech start; timestamp accuracy is not established.",
        ],
        "configuration_tests": {"passed": 6, "junit_path": relative(base / "config-tests.xml"), "junit_sha256": digest(base / "config-tests.xml")},
        "runs": runs,
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"Validated {len(runs)} trials, model hashes, source hashes, and PCM/EOF/device evidence -> {args.output}")


if __name__ == "__main__":
    main()
