"""Run a real local checkpoint through WLK AudioProcessor and SimulStreaming.

Uses the same PCM/ASR/finalization pipeline as /asr, without a network server.
Records every frontend update plus device evidence. No microphone or translation.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import platform
import sys
import subprocess
import time
import traceback


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", choices=["cpu", "mps"], default="mps")
    parser.add_argument("--dtype", choices=["float32", "float16"], default="float32")
    parser.add_argument("--paced", action="store_true")
    parser.add_argument("--chunk-seconds", type=float, default=0.5)
    parser.add_argument("--timeout", type=float, default=180)
    return parser.parse_args()


async def run(args, result):
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
    import torch
    import mlx.core as mx
    from whisperlivekit.config import WhisperLiveKitConfig
    from whisperlivekit.core import TranscriptionEngine
    from whisperlivekit.audio_processor import AudioProcessor
    from whisperlivekit.whisper import _ALIGNMENT_HEADS

    loop = asyncio.get_running_loop()
    # MLX model creation and queued inference share one owned worker.
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1, thread_name_prefix="wlk-gpu"))
    audio, sample_rate = sf.read(args.audio, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    divisor = math.gcd(sample_rate, 16000)
    audio = resample_poly(audio, 16000 // divisor, sample_rate // divisor).astype(np.float32)
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    warmup = args.output.parent / "warmup.wav"
    sf.write(warmup, audio[:48000], 16000, subtype="PCM_16")
    result.update({"torch": torch.__version__, "mlx": mx.__version__,
                   "mps_available": torch.backends.mps.is_available(),
                   "audio_seconds": len(audio)/16000, "input_samples": len(audio),
                   "mode": "paced" if args.paced else "burst", "translation": False})
    if args.device == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError("This process cannot access MPS; no CPU fallback is permitted")
    config = WhisperLiveKitConfig(
        backend="mlx-whisper", backend_policy="simulstreaming", model_size="large-v3-turbo",
        encoder_model_path=str(args.model.resolve()), decoder_model_path=str(args.model.resolve()),
        custom_alignment_heads=_ALIGNMENT_HEADS["large-v3-turbo"].decode("ascii"),
        decoder_device=args.device, decoder_dtype=args.dtype,
        lan="en", pcm_input=True, diarization=False, target_language="",
        warmup_file=str(warmup.resolve()), min_chunk_size=args.chunk_seconds,
        asr_coalesce_min_s=args.chunk_seconds, retention_seconds=0,
        max_buffered_audio=30, backpressure_timeout=60,
    )
    result["configuration"] = asdict(config)
    started = time.perf_counter()
    engine = await asyncio.to_thread(TranscriptionEngine, config=config)
    result["load_and_warmup_seconds"] = time.perf_counter()-started
    processor = AudioProcessor(transcription_engine=engine, mode="full", language="en")
    decoder = processor.transcription.model
    parameters = list(decoder.model.decoder.parameters())
    result["device_evidence"] = {
        "encoder_backend": engine.asr.encoder_backend,
        "decoder_parameter_devices": sorted({str(p.device) for p in parameters}),
        "decoder_parameter_dtypes": sorted({str(p.dtype) for p in parameters}),
        "alignatt_device": str(decoder.device),
        "uses_full_mlx_decoder": engine.asr.use_full_mlx,
        "encoder_parameter_dtype": str(engine.asr.mlx_encoder.encoder.conv1.weight.dtype),
    }
    assert all(p.device.type == args.device for p in parameters)
    assert decoder.device.type == args.device
    assert not engine.asr.use_full_mlx and engine.asr.mlx_encoder is not None
    original_encode = decoder._encode

    def traced_encode(samples):
        features, end = original_encode(samples)
        result["device_evidence"]["mlx_default_device"] = str(mx.default_device())
        result["device_evidence"]["encoded_feature_device"] = str(features.device)
        result["device_evidence"]["encoded_feature_dtype"] = str(features.dtype)
        assert features.device.type == args.device
        return features, end

    decoder._encode = traced_encode
    updates = []
    clock_start = time.perf_counter()
    log_path = args.output.with_suffix(".updates.jsonl")
    sink = log_path.open("w")

    async def collect(generator):
        async for front in generator:
            record = {"elapsed": time.perf_counter()-clock_start, **front.to_dict()}
            updates.append(record)
            sink.write(json.dumps(record, ensure_ascii=False)+"\n")
            sink.flush()
            if record.get("error"):
                raise RuntimeError(record["error"])

    consumer = None
    try:
        generator = await processor.create_tasks()
        clock_start = time.perf_counter()
        consumer = asyncio.create_task(collect(generator))
        nbytes = int(args.chunk_seconds*16000)*2
        for offset in range(0, len(pcm), nbytes):
            stop = min(offset+nbytes, len(pcm))
            if args.paced:
                await asyncio.sleep(max(0, stop/32000 - (time.perf_counter()-clock_start)))
            if consumer.done():
                await consumer
                raise RuntimeError("Result stream stopped before all input was sent")
            await processor.process_audio(pcm[offset:stop])
        result["input_finished_seconds"] = time.perf_counter()-clock_start
        await processor.process_audio(b"")
        await asyncio.wait_for(consumer, args.timeout)
        result["elapsed_seconds"] = time.perf_counter()-clock_start
        result["tail_finalize_seconds"] = result["elapsed_seconds"]-result["input_finished_seconds"]
        result["first_any_text_seconds"] = next((r["elapsed"] for r in updates if
            r.get("buffer_transcription") or any(x.get("text") for x in r.get("lines", []))), None)
        result["first_committed_text_seconds"] = next((r["elapsed"] for r in updates if
            any(x.get("text") for x in r.get("lines", []))), None)
        final = updates[-1] if updates else {}
        result["final"] = final
        result["text"] = " ".join(x["text"] for x in final.get("lines", []) if x.get("text"))
        result["received_pcm_samples"] = processor.total_pcm_samples
        result["queue_peak_samples"] = processor.transcription_queue.peak_samples
        result["processing_error"] = processor.processing_error
        result["overload_error"] = processor.overload_error
        result["metrics"] = {
            "total_asr_call_seconds": processor.metrics.total_processing_time_s,
            "asr_call_time_per_audio_second": processor.metrics.total_processing_time_s/(len(audio)/16000),
            "n_transcription_calls": processor.metrics.n_transcription_calls,
            "transcription_durations": list(processor.metrics.transcription_durations),
            "n_tokens_produced": processor.metrics.n_tokens_produced,
        }
        if processor.processing_error or processor.overload_error:
            raise RuntimeError(processor.processing_error or processor.overload_error)
        if not result["text"].strip() or final.get("buffer_transcription"):
            raise RuntimeError("No committed text or unflushed transcription tail")
        if processor.total_pcm_samples != len(audio):
            raise RuntimeError("Submitted PCM sample count was not preserved")
        result["status"] = "completed"
    finally:
        if consumer is not None and not consumer.done():
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        await processor.cleanup()
        sink.close()


def main():
    args = arguments()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "0"
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(args.source.resolve()))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    result = {"status": "started", "python": platform.python_version(),
              "audio_path": str(args.audio.resolve()),
              "audio_sha256": hashlib.sha256(args.audio.read_bytes()).hexdigest(),
              "mps_fallback": "0", "transport": "in-process AudioProcessor PCM"}
    result["power_before"] = subprocess.run(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True).stdout
    try:
        asyncio.run(run(args, result))
    except Exception as exc:
        result.update(status="failed", error=str(exc), traceback=traceback.format_exc())
    result["power_after"] = subprocess.run(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True).stdout
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str)+"\n")
    print(json.dumps({k:v for k,v in result.items() if k not in ("configuration", "final", "traceback")}, indent=2, ensure_ascii=False, default=str))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
