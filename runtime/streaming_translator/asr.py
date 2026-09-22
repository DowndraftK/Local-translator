"""Adapter for the pinned, patched WLK AudioProcessor; all tokens stay local."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import sys
import time
import wave

from .audio import prepare_audio
from .store import file_sha256


def validate_resources(config):
    source = Path(config['source']).resolve()
    fingerprints = json.loads(Path(config['source_manifest']).read_text())
    for name, expected in fingerprints['patched_files'].items():
        if file_sha256(source / name) != expected:
            raise ValueError(f'流式源码校验失败：{name}')
    model = Path(config['model']).resolve()
    manifest = json.loads(Path(config['model_manifest']).read_text())
    for name, expected in manifest['files'].items():
        path = model / name
        if path.stat().st_size != expected['size'] or file_sha256(path) != expected['sha256']:
            raise ValueError(f'语音模型校验失败：{name}')
    return fingerprints, manifest


async def recognize(store, config, input_path, paced, stop, max_audio_seconds=None, stdin_pcm=False):
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['PYTORCH_ENABLE_MPS_FALLBACK'] = '0'
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1, thread_name_prefix='speech-gpu'))
    fingerprints, manifest = await asyncio.to_thread(validate_resources, config)
    sys.path.insert(0, str(Path(config['source']).resolve()))
    import numpy as np
    import torch
    import mlx.core as mx
    from whisperlivekit.config import WhisperLiveKitConfig
    from whisperlivekit.core import TranscriptionEngine
    from whisperlivekit.audio_processor import AudioProcessor
    from whisperlivekit.whisper import _ALIGNMENT_HEADS

    device = config.get('device', 'mps')
    dtype = config.get('dtype', 'float32')
    if device == 'mps' and not torch.backends.mps.is_available():
        raise ValueError('当前进程无法访问 MPS GPU；请检查运行环境。不会自动改用 CPU。')
    if device not in ('mps', 'cpu') or dtype not in ('float32', 'float16'):
        raise ValueError('不支持的解码设备或精度。')
    audio_path = store.directory / 'audio.wav'
    if stdin_pcm:
        input_samples = None
    else:
        source_hash = await asyncio.to_thread(file_sha256, input_path)
        store.update(source_path=str(Path(input_path).resolve()), source_sha256=source_hash)
        input_samples = await asyncio.to_thread(prepare_audio, input_path, audio_path)
        store.update(source_path=str(Path(input_path).resolve()), source_sha256=source_hash,
                     audio_seconds=input_samples/16000, input_samples=input_samples,
                     audio_sha256=await asyncio.to_thread(file_sha256, audio_path))
    warmup = store.directory / 'warmup.wav'
    with wave.open(str(warmup), 'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
        if stdin_pcm:
            output.writeframes(np.zeros(48000, dtype='<i2').tobytes())
        else:
            with wave.open(str(audio_path), 'rb') as source:
                output.writeframes(source.readframes(48000))
    cfg = WhisperLiveKitConfig(backend='mlx-whisper', backend_policy='simulstreaming',
        model_size='large-v3-turbo', encoder_model_path=str(Path(config['model']).resolve()),
        decoder_model_path=str(Path(config['model']).resolve()),
        custom_alignment_heads=_ALIGNMENT_HEADS['large-v3-turbo'].decode('ascii'),
        decoder_device=device, decoder_dtype=dtype, lan='en', pcm_input=True,
        diarization=False, target_language='', warmup_file=str(warmup.resolve()),
        min_chunk_size=.5, asr_coalesce_min_s=config.get('coalesce_seconds', .5), retention_seconds=60,
        max_context_tokens=config.get('max_context_tokens', 128),
        max_buffered_audio=30, backpressure_timeout=60)
    store.update(state='loading', asr_config=asdict(cfg), source_commit=fingerprints['upstream_commit'],
                 model_revision=manifest['revision'])
    store.publish()
    loaded_at = time.monotonic()
    engine = await asyncio.to_thread(TranscriptionEngine, config=cfg)
    load_seconds = time.monotonic()-loaded_at
    sequence = 0
    clock = time.monotonic()
    first_token = None

    class DurableProcessor(AudioProcessor):
        async def _queue_tokens_for_translation(self, tokens):
            # The pinned upstream calls this once for every committed token batch,
            # including EOF recovery. Translation runs in our own durable queue.
            nonlocal sequence, first_token
            if tokens:
                if first_token is None:
                    first_token = time.monotonic()-clock
                values = [{'text': t.text or '', 'start': max(0, float(t.start or 0)),
                           'end': max(0, float(t.end or t.start or 0))} for t in tokens]
                store.ingest(sequence, values)
                sequence += 1

    processor = DurableProcessor(transcription_engine=engine, mode='full', language='en')
    alignatt = processor.transcription.model
    parameters = list(alignatt.model.decoder.parameters())
    evidence = {'decoder_devices': sorted({str(p.device) for p in parameters}),
                'decoder_dtypes': sorted({str(p.dtype) for p in parameters}),
                'alignatt_device': str(alignatt.device), 'mlx_default_device': str(mx.default_device()),
                'encoder_dtype': str(engine.asr.mlx_encoder.encoder.conv1.weight.dtype),
                'full_mlx_decoder': engine.asr.use_full_mlx, 'mps_fallback': '0',
                'torch': torch.__version__, 'mlx': mx.__version__}
    if any(p.device.type != device for p in parameters) or engine.asr.use_full_mlx:
        raise ValueError('实际 decoder 与配置不一致。')
    store.update(state='recognizing', device_evidence=evidence, load_seconds=load_seconds,
                 audio_path=str(audio_path.resolve()))
    store.publish()
    print(json.dumps({'event': 'ready', 'session': str(store.directory)}, ensure_ascii=False), flush=True)
    reader_transport = None
    raw_media = None
    consumer = None
    sent_samples = 0
    last_front = {}

    async def collect(generator):
        nonlocal last_front
        async for front in generator:
            last_front = front.to_dict()
            if last_front.get('error'):
                raise RuntimeError(last_front['error'])

    try:
        generator = await processor.create_tasks()
        consumer = asyncio.create_task(collect(generator))
        clock = time.monotonic()
        store.update(playback_started_at=time.time())
        if stdin_pcm:
            reader = asyncio.StreamReader(limit=65536)
            reader_transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
            raw_media = audio_path.open('wb')
            media = wave.open(raw_media, 'wb')
            media.setnchannels(1); media.setsampwidth(2); media.setframerate(16000)
        else:
            media = wave.open(str(audio_path), 'rb')
        try:
            while not stop.is_set():
                if stdin_pcm:
                    try:
                        data = await asyncio.wait_for(reader.readexactly(16000), 1)
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.IncompleteReadError as exc:
                        data = exc.partial
                    if len(data) % 2:
                        raise ValueError('PCM 输入以不完整的 16 位样本结束。')
                else:
                    data = media.readframes(8000)
                if not data:
                    break
                if max_audio_seconds is not None:
                    remaining = max(0, int(max_audio_seconds*16000)-sent_samples)
                    data = data[:remaining*2]
                    if not data:
                        break
                if stdin_pcm:
                    # Persist incoming PCM before handing it to ASR. A live WAV header
                    # is updated per packet so an abnormal exit leaves readable audio.
                    media.writeframes(data)
                    raw_media.flush()
                    os.fsync(raw_media.fileno())
                sent_samples += len(data)//2
                if paced and not stdin_pcm:
                    wait = sent_samples/16000-(time.monotonic()-clock)
                    if wait > 0:
                        try:
                            await asyncio.wait_for(stop.wait(), wait)
                        except asyncio.TimeoutError:
                            pass
                if consumer.done():
                    await consumer
                    raise RuntimeError('ASR 结果流提前停止。')
                await processor.process_audio(data)
                store.update(received_audio_seconds=sent_samples/16000,
                             asr_queue_seconds=processor.transcription_queue.queued_samples/16000
                             if hasattr(processor.transcription_queue, 'queued_samples') else None)
        finally:
            media.close()
            if raw_media:
                raw_media.close()
        input_finished = time.monotonic()
        store.update(state='finishing_asr')
        await processor.process_audio(b'')
        await asyncio.wait_for(consumer, 180)
        if processor.processing_error or processor.overload_error:
            raise RuntimeError(processor.processing_error or processor.overload_error)
        if processor.total_pcm_samples != sent_samples:
            raise RuntimeError('ASR 接收到的样本数与输入不符。')
        if last_front.get('buffer_transcription'):
            raise RuntimeError('ASR 结束后仍有未处理暂存文本。')
        store.ingest(sequence, [], final=True)
        duration = time.monotonic()-clock
        store.update(asr_complete=not stop.is_set(), stopped=stop.is_set(),
            input_samples=sent_samples, received_pcm_samples=processor.total_pcm_samples,
            processed_audio_seconds=sent_samples/16000, asr_elapsed_seconds=duration,
            input_finished_seconds=input_finished-clock,
            first_committed_token_seconds=first_token, asr_tail_seconds=time.monotonic()-input_finished,
            asr_call_seconds=processor.metrics.total_processing_time_s,
            asr_calls=processor.metrics.n_transcription_calls,
            queue_peak_seconds=processor.transcription_queue.peak_samples/16000)
        if stdin_pcm:
            store.update(audio_seconds=sent_samples/16000, audio_sha256=await asyncio.to_thread(file_sha256, audio_path))
    finally:
        if reader_transport:
            reader_transport.close()
        if consumer and not consumer.done():
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        await processor.cleanup()
