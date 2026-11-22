"""Run the pinned WLK qwen3-vllm-metal backend against verified local BF16."""
from contextlib import contextmanager, redirect_stdout
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time
import wave

RATE = 16000
FRAME = 8000
MAX_WINDOW = 16*RATE
TIMESTAMPS = 'pcm-window-word-estimate'


@contextmanager
def gpu_turn(request):
    if request['stage'] != 'live':
        yield
        return
    deadline = time.monotonic() + 180
    with open(request['gpu_lock'], 'rb') as lock:
        while True:
            if not Path(request['translation_pending']).exists():
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    pass
            if time.monotonic() >= deadline:
                raise TimeoutError('Qwen 等待当前字幕翻译超过执行时限。')
            time.sleep(.03)
        yield


def online_processor(module):
    """Use the WLK word timestamps and PCM buffer with two-pass prefix agreement."""
    class Online(module.Qwen3VLLMMetalOnlineProcessor):
        previous = ()

        def process_iter(self, is_last=False):
            if not is_last and self._samples_since_last_inference < self._min_new_samples:
                return [], self.end
            self._samples_since_last_inference = 0
            return self._commit_available(flush=is_last), self.end

        def _commit_available(self, flush=False):
            if len(self.audio_buffer) < 400:
                return []
            words = self._transcribe_words()
            keys = tuple(word.casefold().rstrip('.,!?;:') for word in words)
            if flush:
                end = len(words)
            else:
                end = 0
                for left, right in zip(self.previous, keys):
                    if left != right:
                        break
                    end += 1
                end = min(end, len(keys)-self.holdback_words)
            self.previous = keys
            if end <= self._n_committed_words:
                return []
            tokens = self._tokens_for_range(words, self._n_committed_words, end)
            self._n_committed_words = end
            return tokens

    return Online


class Recognition:
    def __init__(self, make_processor, request, emit, np, take_segments):
        self.processor = make_processor()
        self.make_processor, self.request, self.emit = make_processor, request, emit
        self.np, self.take_segments = np, take_segments
        self.total = self.origin = int(request.get('offset', 0))
        self.pending = list(request.get('pending_tokens', []))
        self.index, self.silence, self.last_start = 0, 0, self.origin/RATE
        self.active_samples = 0
        self.previous_hypothesis = None

    def consume(self, pcm):
        samples = self.np.frombuffer(pcm, dtype='<i2').astype(self.np.float32)/32768
        self.total += len(samples)
        rms = float(self.np.sqrt(self.np.mean(samples*samples)))
        quiet = rms < .004
        if len(self.processor.audio_buffer) or not quiet:
            if not len(self.processor.audio_buffer):
                self.origin = self.total-len(samples)
            self.processor.insert_audio_chunk(samples, (self.total-self.origin)/RATE)
            self.active_samples += len(samples)
            self.silence = self.silence+len(samples) if quiet else 0
            if self.silence >= 2*FRAME or self.active_samples >= MAX_WINDOW:
                self.flush('silence' if quiet else 'window_limit')
            elif self.active_samples >= RATE:
                with gpu_turn(self.request), redirect_stdout(sys.stderr):
                    result, _ = self.processor.process_iter()
                self.submit(result)
                if self.request['stage'] == 'live':
                    self.hypothesis()
        if self.request['stage'] == 'live':
            self.emit('live_progress', evidence=dict(submitted_audio_samples=self.total,
                processed_audio_seconds=self.total/RATE, asr_queue_seconds=0))

    def hypothesis(self):
        text = self.processor.get_buffer().text
        if text != self.previous_hypothesis:
            raw = text.encode('utf-8')
            self.emit('hypothesis', text=raw[:2048].decode('utf-8', 'ignore'), truncated=len(raw)>2048,
                      confirmed=False, observedWall=time.time())
            self.previous_hypothesis = text

    def flush(self, reason):
        if self.active_samples >= RATE//4:
            with gpu_turn(self.request), redirect_stdout(sys.stderr):
                result, _ = self.processor.finish()
            self.submit(result, reason)
        else:
            self.submit([], reason)
        self.processor = self.make_processor()
        self.active_samples = self.silence = 0
        if self.request['stage'] == 'live':
            self.hypothesis()

    def submit(self, tokens, final=None):
        for token in tokens:
            start = self.origin/RATE + float(token.start)
            end = self.origin/RATE + float(token.end)
            self.pending.append(dict(text=token.text, start=start, end=end))
        rows, self.pending = self.take_segments(self.pending, final=final is not None, max_seconds=8)
        for row in rows:
            self.index += 1
            start = max(self.last_start, row['start'])
            self.last_start = start
            row.update(id=self.index, start=start, end=max(start, row['end']), committed_at=time.time())
            if final and row['boundary'] == 'end_of_input':
                row['boundary'] = final
            row['timing'].update(source=TIMESTAMPS, status='estimated')
            self.emit('cue', row=row)


def live_input(request, recognition):
    feed_path = Path(request['source'])
    seen = []
    seen_events = set()
    while True:
        with feed_path.open('rb') as stream:
            raw = stream.read(2*1024**2+1)
        if len(raw) > 2*1024**2:
            raise ValueError('Qwen 录音清单超过范围。')
        feed = json.loads(raw)
        chunks = feed['chunks']
        if (feed.get('contract') != 'audio-capture-feed-v1' or feed['session'] != request['capture_session']
                or feed['task'] != request['task_id'] or len(chunks) > 3600 or chunks[:len(seen)] != seen):
            raise ValueError('Qwen 录音清单身份或前缀变化。')
        events = {row['sample']: row for row in feed['events'] if row['kind'] == 'paused' and row['id'] not in seen_events}

        def pause_boundary():
            if recognition.total in events:
                seen_events.add(events.pop(recognition.total)['id'])
                recognition.flush('capture_pause')

        for index in range(len(seen), len(chunks)):
            pause_boundary()
            chunk = chunks[index]
            if (chunk['file'] != f'capture/{index:06d}.pcm' or chunk['sequence'] != index or
                    chunk['offset'] != recognition.total or not 0 < chunk['samples'] <= FRAME):
                raise ValueError('Qwen 录音分块顺序或样本数无效。')
            path = feed_path.parent/chunk['file']
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError('Qwen 录音分块路径含链接。')
            pcm = path.read_bytes()
            if len(pcm) != chunk['samples']*2 or len(pcm) != chunk['bytes'] or hashlib.sha256(pcm).hexdigest() != chunk['sha256']:
                raise ValueError('Qwen 录音分块校验失败。')
            recognition.consume(pcm)
            seen.append(chunk)
        pause_boundary()
        if feed['samples'] != recognition.total or events:
            raise ValueError('Qwen 录音水位或暂停边界无效。')
        if feed['closed']:
            break
        time.sleep(.03)


def recognize(request, emit):
    # -I -S starts with the pinned interpreter's standard library only.
    config = request['config']
    sys.path[:0] = [config['source'], config['site_packages'], request['bridge']]
    for name, expected in config['packages'].items():
        if importlib.metadata.version(name) != expected:
            raise ValueError(f'Qwen 运行组件 {name} 需要版本 {expected}。')
    emit('state', state='loading', evidence={'backend': 'qwen3-vllm-metal'})
    started = time.time()
    with gpu_turn(request), redirect_stdout(sys.stderr):
        import huggingface_hub
        import mlx.core as mx
        import numpy as np
        from whisperlivekit import qwen3_vllm_metal_asr as metal
        from streaming_translator.segmentation import take_segments
        if mx.default_device() != mx.gpu or not Path(config['model']).is_dir():
            raise ValueError('Qwen 需要 Metal GPU 与已核验的本地模型目录。')
        def local_only(*args, **kwargs):
            raise ValueError('Qwen 仅接收已导入的本机文件。')
        huggingface_hub.snapshot_download = local_only
        huggingface_hub.hf_hub_download = local_only
        backend = metal.Qwen3VLLMMetalASR(model_dir=config['model'], vllm_dtype='bfloat16',
            qwen3_vllm_metal_audio_backend='standard', trim_sentence_buffer=False,
            holdback_words=2, min_chunk_size=1.0, max_tokens=256)
    processor_type = online_processor(metal)
    session = Recognition(lambda: processor_type(backend), request, emit, np, take_segments)
    emit('model_ready', startedWall=started, readyWall=time.time(), reused=False)
    emit('ready')
    if request['stage'] == 'live':
        live_input(request, session)
    elif request['stage'] == 'asr':
        with wave.open(request['source'], 'rb') as wavefile:
            if (wavefile.getframerate(), wavefile.getnchannels(), wavefile.getsampwidth()) != (RATE, 1, 2):
                raise ValueError('Qwen 转写入口需要 16 kHz 单声道 PCM16。')
            wavefile.setpos(session.total)
            while pcm := wavefile.readframes(FRAME):
                session.consume(pcm)
    else:
        raise ValueError('Qwen 录后精修在本版未开放。')
    session.flush('end_of_input')
    emit('complete', complete=True, evidence={'backend': 'qwen3-vllm-metal',
        'runtime': config['runtime'], 'model_revision': request['model_revision'],
        'samples': session.total, 'cue_count': session.index, 'timestamp_source': TIMESTAMPS})
