"""Adapter for the pinned, patched WLK AudioProcessor; all tokens stay local."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import logging
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


async def recognize(store, config, input_path, paced, stop, max_audio_seconds=None, stdin_pcm=False, resume=False):
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
    from .vad import LookbackVAD
    from .capture import PCMArchive, resource_digest, PrefixHasher

    device = config.get('device', 'mps')
    dtype = config.get('dtype', 'float32')
    if device == 'mps' and not torch.backends.mps.is_available():
        raise ValueError('当前进程无法访问 MPS GPU；请检查运行环境。不会自动改用 CPU。')
    if device not in ('mps', 'cpu') or dtype not in ('float32', 'float16'):
        raise ValueError('不支持的解码设备或精度。')
    audio_path = store.directory / 'audio.wav'
    offset = store.get('resume_sample', 0) if resume else 0
    if stdin_pcm:
        input_samples = None
    elif resume:
        with wave.open(str(audio_path), 'rb') as media:
            input_samples = media.getnframes()
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
        vac=config.get('vad', True),
        min_chunk_size=.5, asr_coalesce_min_s=config.get('coalesce_seconds', .5), retention_seconds=60,
        max_context_tokens=config.get('max_context_tokens', 128),
        max_buffered_audio=30, backpressure_timeout=60)
    store.update(state='loading', asr_config=asdict(cfg), source_commit=fingerprints['upstream_commit'],
                 model_revision=manifest['revision'])
    store.publish()
    loaded_at = time.monotonic()
    engine = await asyncio.to_thread(TranscriptionEngine, config=cfg)
    load_seconds = time.monotonic()-loaded_at
    sequence = store.next_sequence()
    origin = offset
    resource_fingerprint = resource_digest(config)
    prefix_hasher = PrefixHasher(audio_path)
    clock = time.monotonic()
    first_token = None

    class DurableProcessor(AudioProcessor):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.vad_gate = LookbackVAD(preroll=int(config.get('vad_preroll_seconds', 1)*16000))
            self.gated_active_samples = 0
            self.gated_silence_samples = 0

        async def _process_pcm_array(self, pcm_array):
            if not self.args.vac:
                return await super()._process_pcm_array(pcm_array)
            events = self.vac(pcm_array) or []
            for event in events:
                logging.info('VAD_EVENT %s received_sample=%s', event,
                             self.total_pcm_samples + len(pcm_array))
            await self._send_spans(self.vad_gate.feed(pcm_array, events))
            self.total_pcm_samples += len(pcm_array)

        async def _send_spans(self, spans):
            for start, end, active, pcm in spans:
                if active:
                    await self._end_silence(at_sample=start, speech_resumed=True)
                    await self._enqueue_active_audio(pcm)
                    self.gated_active_samples += end-start
                else:
                    await self._begin_silence(at_sample=start)
                    self.gated_silence_samples += end-start

        async def _finish_input(self):
            if self.args.vac:
                await self._send_spans(self.vad_gate.feed(np.empty(0, dtype=np.float32), final=True))
            await super()._finish_input()

        async def _emit_stream_event_after_snapshot(self, kind, timestamp):
            if kind == 'silence_transcription_ready':
                sample = origin + round(timestamp*16000)
                previous = store.latest_checkpoint()
                # Only a fully flushed silence boundary is restartable. No KV state
                # or guessed token timestamp is treated as a complete checkpoint.
                if sample >= (previous['sample'] if previous else 0)+160000:
                    end = max((t['end'] for t in store.get('pending_tokens', [])), default=0)
                    end = max(end, store.db.execute('SELECT COALESCE(MAX(end),0) FROM segments').fetchone()[0])
                    if end <= sample/16000 and not self.state.buffer_transcription.text.strip():
                        store.checkpoint(sample, prefix_hasher.digest(sample), resource_fingerprint)
            await super()._emit_stream_event_after_snapshot(kind, timestamp)

        async def _queue_tokens_for_translation(self, tokens):
            # The pinned upstream calls this once for every committed token batch,
            # including EOF recovery. Translation runs in our own durable queue.
            nonlocal sequence, first_token
            if tokens:
                if first_token is None:
                    first_token = time.monotonic()-clock
                values = [{'text': t.text or '', 'start': origin/16000 + max(0, float(t.start or 0)),
                           'end': origin/16000 + max(0, float(t.end or t.start or 0))} for t in tokens]
                store.ingest(sequence, values)
                sequence += 1

    processor = DurableProcessor(transcription_engine=engine, mode='diff', language='en')
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
                 resources_digest=resource_fingerprint,
                 audio_path=str(audio_path.resolve()))
    store.publish()
    print(json.dumps({'event': 'ready', 'session': str(store.directory)}, ensure_ascii=False), flush=True)
    archive = PCMArchive(store) if stdin_pcm else None
    capture_task = None
    consumer = None
    sent_samples = offset
    last_front = {}
    metrics = {'calls': 0, 'call_seconds': 0, 'queue_peak': 0, 'active': 0, 'silence': 0, 'late': 0}
    processed_pauses = set()

    def capture_events():
        path = store.directory/'capture-events.jsonl'
        if not path.exists():
            return []
        # A recording has few explicit lifecycle events; tolerate only an unfinished
        # final line while the native serial capture queue is appending it.
        events = []
        for line in path.read_text().splitlines(keepends=True):
            if line.endswith('\n'):
                events.append(json.loads(line))
        return events

    async def collect(generator):
        nonlocal last_front
        last_update = 0
        async for front in generator:
            last_front = front.to_dict()
            if last_front.get('error'):
                raise RuntimeError(last_front['error'])
            received = archive.samples if archive else sent_samples
            processed = origin + round(processor.state.end_transcription_processed*16000)
            if time.monotonic()-last_update >= .5:
                store.update(processed_audio_seconds=processed/16000,
                    asr_backlog_seconds=max(0, (received-processed)/16000),
                    asr_queue_seconds=processor.transcription_queue.queued_samples/16000)
                last_update = time.monotonic()

    async def start_processor():
        nonlocal consumer, last_front
        last_front = {}
        generator = await processor.create_tasks()
        consumer = asyncio.create_task(collect(generator))

    async def finish_processor():
        await processor.process_audio(b'')
        await asyncio.wait_for(consumer, 180)
        if processor.processing_error or processor.overload_error:
            raise RuntimeError(processor.processing_error or processor.overload_error)
        if processor.total_pcm_samples != sent_samples-origin:
            raise RuntimeError('ASR 接收到的样本数与输入不符。')
        if last_front.get('buffer_transcription'):
            raise RuntimeError('ASR 结束后仍有未处理暂存文本。')
        metrics['calls'] += processor.metrics.n_transcription_calls
        metrics['call_seconds'] += processor.metrics.total_processing_time_s
        metrics['queue_peak'] = max(metrics['queue_peak'], processor.transcription_queue.peak_samples/16000)
        metrics['active'] += processor.gated_active_samples
        metrics['silence'] += processor.gated_silence_samples
        metrics['late'] += processor.vad_gate.late_events
        await processor.cleanup()

    try:
        await start_processor()
        clock = time.monotonic()
        store.update(playback_started_at=time.time(), playback_sample_offset=offset,
                     durable_audio_samples=input_samples or 0)
        if archive:
            capture_task = asyncio.create_task(archive.receive())
            while archive.samples == 0 and not archive.done.is_set():
                await asyncio.sleep(.02)
            if archive.done.is_set():
                await capture_task
            # The writer creates a canonical 44-byte WAV header; ASR tails only
            # fsync-confirmed PCM, independently of pipe ingestion.
            source = audio_path.open('rb')
            source.seek(44)
        else:
            source = wave.open(str(audio_path), 'rb')
            source.setpos(offset)
        try:
            while not stop.is_set():
                events = capture_events()
                boundary = next((e for e in events if e['kind'] in ('paused', 'interrupted')
                    and e['id'] not in processed_pauses and e['sample'] >= sent_samples), None)
                if boundary and boundary['sample'] == sent_samples:
                    await finish_processor()
                    store.ingest(sequence, [], final=True, complete=False)
                    sequence += 1
                    store.checkpoint(sent_samples, prefix_hasher.digest(sent_samples), resource_fingerprint)
                    processed_pauses.add(boundary['id'])
                    origin = sent_samples
                    processor = DurableProcessor(transcription_engine=engine, mode='diff', language='en')
                    await start_processor()
                    store.update(capture_state=boundary['kind'])
                if archive:
                    available = archive.samples-sent_samples
                    if boundary and boundary['sample'] > sent_samples:
                        available = min(available, boundary['sample']-sent_samples)
                    if available <= 0:
                        if archive.done.is_set():
                            await capture_task
                            break
                        await asyncio.sleep(.02)
                        continue
                    data = source.read(min(8000, available)*2)
                else:
                    available = min(8000, boundary['sample']-sent_samples) if boundary and boundary['sample'] > sent_samples else 8000
                    data = source.readframes(available)
                if not data:
                    break
                if max_audio_seconds is not None:
                    remaining = max(0, int(max_audio_seconds*16000)-sent_samples)
                    data = data[:remaining*2]
                    if not data:
                        break
                sent_samples += len(data)//2
                if paced and not stdin_pcm:
                    wait = (sent_samples-offset)/16000-(time.monotonic()-clock)
                    if wait > 0:
                        try:
                            await asyncio.wait_for(stop.wait(), wait)
                        except asyncio.TimeoutError:
                            pass
                if consumer.done():
                    await consumer
                    raise RuntimeError('ASR 结果流提前停止。')
                await processor.process_audio(data)
                store.update(received_audio_seconds=(archive.samples if archive else sent_samples)/16000,
                    submitted_audio_samples=sent_samples)
        finally:
            source.close()
        input_finished = time.monotonic()
        store.update(state='finishing_asr')
        await finish_processor()
        store.ingest(sequence, [], final=True)
        complete_samples = archive.samples if archive else input_samples
        store.update(asr_complete=sent_samples == complete_samples and not stop.is_set(),
            stopped=stop.is_set(), input_samples=complete_samples, received_pcm_samples=sent_samples,
            processed_audio_seconds=sent_samples/16000, asr_backlog_seconds=max(0,(complete_samples-sent_samples)/16000),
            asr_elapsed_seconds=time.monotonic()-clock, input_finished_seconds=input_finished-clock,
            first_committed_token_seconds=first_token, asr_tail_seconds=time.monotonic()-input_finished,
            asr_call_seconds=metrics['call_seconds'], asr_calls=metrics['calls'],
            queue_peak_seconds=metrics['queue_peak'], vad_active_samples=metrics['active'],
            vad_silence_samples=metrics['silence'], vad_late_events=metrics['late'])
        if stdin_pcm:
            store.update(audio_seconds=complete_samples/16000, audio_sha256=await asyncio.to_thread(file_sha256, audio_path))
    finally:
        for task in (capture_task, consumer):
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await processor.cleanup()
