"""Execution-only adapters over the unchanged, pinned streaming recognizer."""
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
import wave

_CONTEXT = {}


def configure(request, stop, emit, store=None):
    if _CONTEXT:
        _CONTEXT['request'].clear()
        _CONTEXT['request'].update(request)
        _CONTEXT.update(stop=stop, store=store)
        return
    request = dict(request)
    _CONTEXT.update(request=request, stop=stop, store=store)
    import sys
    sys.path.insert(0, request['config']['source'])
    from whisperlivekit.core import TranscriptionEngine
    from whisperlivekit.audio_processor import AudioProcessor
    import streaming_translator.capture as capture

    original_init = TranscriptionEngine.__init__
    original_call = AudioProcessor._run_counted_transcription_call
    original_tasks = AudioProcessor.create_tasks
    original_stream_event = AudioProcessor._emit_stream_event_after_snapshot

    def initialize(engine, *args, **kwargs):
        config = kwargs.get('config')
        warmup = request['config'].get('live_warmup_file')
        if config is not None and warmup:
            config.warmup_file = warmup
        reused = TranscriptionEngine._initialized
        preparing = time.time()
        with Path(request['gpu_lock']).open('rb') as lock:
            deadline = time.monotonic() + 180
            while True:
                if _CONTEXT['stop'].is_set():
                    raise RuntimeError('模型加载已取消。')
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise TimeoutError('GPU模型加载执行权等待超时。')
                    time.sleep(.03)
            original_init(engine, *args, **kwargs)
        emit('model_ready', reused=reused, startedWall=preparing, readyWall=time.time(),
            warmupFile=warmup)

    async def counted(processor, *args):
        queued = time.time()
        with Path(request['gpu_lock']).open('rb') as lock:
            deadline = time.monotonic() + 180
            while True:
                if _CONTEXT['stop'].is_set():
                    raise RuntimeError('识别已取消。')
                # Give an already queued stable-cue translation one bounded
                # turn before the recognizer reacquires the same OS lock.
                if Path(request['translation_pending']).exists():
                    if time.monotonic() > deadline:
                        raise TimeoutError('等待当前字幕翻译超过执行时限。')
                    await asyncio.sleep(.03)
                    continue
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise TimeoutError('GPU识别执行权等待超时。')
                    await asyncio.sleep(.03)
            started = time.time()
            result = await asyncio.wait_for(original_call(processor, *args), 120)
            emit('inference_timing', queuedWall=queued, startedWall=started, endedWall=time.time(),
                mediaEnd=result[1], confirmedTokens=len(result[0]))
            return result

    TranscriptionEngine.__init__ = initialize
    AudioProcessor._run_counted_transcription_call = counted
    async def observed_tasks(processor, *args, **kwargs):
        generator = await original_tasks(processor, *args, **kwargs)
        async def fronts():
            previous = None
            async for front in generator:
                text = front.to_dict().get('buffer_transcription', '') or ''
                if text != previous:
                    raw = text.encode()
                    emit('hypothesis', text=raw[:2048].decode('utf-8', errors='ignore'),
                         truncated=len(raw) > 2048, confirmed=False, observedWall=time.time())
                    previous = text
                yield front
        return fronts()
    AudioProcessor.create_tasks = observed_tasks
    async def confirmed_pause(processor, kind, timestamp):
        if kind=='silence_transcription_ready' and _CONTEXT['store'] is not None:
            _CONTEXT['store'].flush_confirmed_pause(timestamp)
        await original_stream_event(processor, kind, timestamp)
    AudioProcessor._emit_stream_event_after_snapshot = confirmed_pause

    def warm_streaming_path():
        import numpy as np
        from whisperlivekit.simul_whisper.backend import SimulStreamingOnlineProcessor
        with wave.open(request['config']['live_warmup_file'], 'rb') as media:
            samples = np.frombuffer(media.readframes(media.getnframes()),dtype='<i2').astype(np.float32)/32768
        with Path(request['gpu_lock']).open('rb') as lock:
            deadline = time.monotonic()+180
            while True:
                if _CONTEXT['stop'].is_set():
                    raise RuntimeError('识别预热已取消。')
                try:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic()>deadline:
                        raise TimeoutError('识别预热等待超时。')
                    time.sleep(.03)
            probe = SimulStreamingOnlineProcessor(TranscriptionEngine._instance.asr)
            calls = 0
            started = time.time()
            for offset in range(0,len(samples),8000):
                if _CONTEXT['stop'].is_set():
                    raise RuntimeError('识别预热已取消。')
                part = samples[offset:offset+8000]
                probe.insert_audio_chunk(part,(offset+len(part))/16000)
                probe.process_iter()
                calls += 1
            probe.start_silence()
            return dict(startedWall=started,readyWall=time.time(),calls=calls+1,
                seconds=len(samples)/16000,profile='fixed-calibration-fresh-online-processor')

    class DurableFeed(capture.PCMArchive):
        async def receive(self, stream=None):
            feed_path = Path(request['source'])
            index = 0
            seen = []
            event_ids = set()
            warmed = False
            try:
                with self.path.open('wb') as raw, wave.open(raw, 'wb') as media:
                    media.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                    media.writeframes(b'')
                    raw.flush()
                    os.fsync(raw.fileno())
                    while not _CONTEXT['stop'].is_set():
                        data = feed_path.read_bytes()
                        if len(data) > 2 * 1024 * 1024:
                            raise ValueError('采集镜像清单超限。')
                        feed = json.loads(data)
                        if (feed.get('contract') != 'audio-capture-feed-v1' or
                                feed.get('session') != request['capture_session'] or feed.get('task') != request['task_id'] or
                                len(feed['chunks']) > 3600 or feed['chunks'][:index] != seen):
                            raise ValueError('已确认录音清单身份或前缀被替换。')
                        if feed.get('warmup_requested') and not warmed:
                            evidence = await asyncio.wait_for(asyncio.to_thread(warm_streaming_path),180)
                            warmed = True
                            emit('inference_ready',capture_session=request['capture_session'],evidence=evidence)
                        for chunk in feed['chunks'][index:]:
                            if (chunk['file'] != f'capture/{index:06d}.pcm' or chunk['sequence'] != index or
                                    chunk['offset'] != self.samples or not 0 < chunk['samples'] <= 8000):
                                raise ValueError('录音清单顺序或范围不符。')
                            path = feed_path.parent / chunk['file']
                            if any(p.is_symlink() for p in (path, *path.parents)):
                                raise ValueError('录音分块路径含链接。')
                            pcm = path.read_bytes()
                            if (len(pcm) != chunk['samples'] * 2 or len(pcm) != chunk['bytes'] or
                                    hashlib.sha256(pcm).hexdigest() != chunk['sha256']):
                                raise ValueError('已确认录音分块hash不符。')
                            media.writeframes(pcm)
                            raw.flush()
                            os.fsync(raw.fileno())
                            self.samples += chunk['samples']
                            seen.append(chunk)
                            index += 1
                            self.store.update(durable_audio_samples=self.samples, audio_seconds=self.samples / 16000,
                                              received_audio_seconds=self.samples / 16000)
                        for event in feed['events']:
                            if event['id'] in event_ids:
                                continue
                            if event['sample'] > self.samples:
                                raise ValueError('暂停边界超过已确认录音。')
                            with (self.store.directory / 'capture-events.jsonl').open('ab') as log:
                                log.write(json.dumps(event).encode() + b'\n')
                                log.flush()
                                os.fsync(log.fileno())
                            event_ids.add(event['id'])
                        if feed['samples'] != self.samples:
                            raise ValueError('录音镜像水位不符。')
                        if feed['closed']:
                            break
                        await asyncio.sleep(.03)
            except BaseException as exc:
                self.error = exc
                raise
            finally:
                self.done.set()

    capture.PCMArchive = DurableFeed
