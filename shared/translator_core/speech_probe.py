"""Exercise the actual speech routes with the bundled English calibration WAV."""
import hashlib
import re
from pathlib import Path
import threading
import time
import uuid
import wave

from . import Cancelled, CoreError
from .resources import atomic_json
from .store import atomic_write

CLIP = Path(__file__).with_name('live_warmup.wav')


def run_live(audio, folder, binding, cancel, receive):
    source = folder/'capture-feed.json'
    chunks = []
    session, task = str(uuid.uuid4()), str(uuid.uuid4())
    directory = folder/'capture'
    directory.mkdir()
    lock = folder/'gpu.lock'
    atomic_write(b'', lock)
    ready, stable, done, closed = (threading.Event() for _ in range(4))
    failures = []
    samples = 0

    def save(complete=False):
        atomic_json(source, dict(contract='audio-capture-feed-v1', session=session, task=task,
                    samples=samples, chunks=chunks, events=[], warmup_requested=False, closed=complete))

    def feed():
        nonlocal samples
        try:
            deadline = time.monotonic()+180
            while not ready.wait(.03):
                if done.is_set() or cancel.is_set():
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError('等待同传后端加载超时。')
            with wave.open(str(CLIP), 'rb') as media:
                while samples < 205000 and not (done.is_set() or cancel.is_set()):
                    if samples == 168000:
                        cutoff = time.monotonic()+8
                        while not stable.wait(.05) and not (done.is_set() or cancel.is_set()) and time.monotonic() < cutoff:
                            pass
                    pcm = media.readframes(min(8000, 168000-samples if samples<168000 else 205000-samples))
                    name = f'capture/{len(chunks):06d}.pcm'
                    atomic_write(pcm, folder/name)
                    chunks.append(dict(file=name, sequence=len(chunks), offset=samples, samples=len(pcm)//2,
                                       bytes=len(pcm), sha256=hashlib.sha256(pcm).hexdigest()))
                    samples += len(pcm)//2
                    save()
                    done.wait(.06)
        except (OSError, CoreError, ValueError) as exc:
            failures.append(str(exc))
        finally:
            closed.set()
            save(True)

    def event(value, path):
        kind = value['event']
        if kind == 'ready':
            ready.set()
        if kind == 'cue' and not closed.is_set():
            stable.set()
        receive(value, path, closed.is_set())

    save()
    thread = threading.Thread(target=feed)
    thread.start()
    try:
        audio.execute('live', source, binding, cancel, event, capture_session=session,
                      task_id=task, gpu_lock=str(lock), translation_pending=str(folder/'translation.pending'))
    finally:
        done.set()
        thread.join()
    if failures:
        raise CoreError(failures[0], 'speech_probe')
    return stable.is_set(), samples


def probe_modes(audio, root, modes, cancel, report, make_binding):
    results = {}
    source_sha = hashlib.sha256(CLIP.read_bytes()).hexdigest()
    with wave.open(str(CLIP), 'rb') as media:
        samples = media.getnframes()
    for capability in modes:
        if cancel.is_set():
            raise Cancelled()
        stage = {'speech.continuous_asr': 'live', 'speech.file_asr': 'asr', 'speech.refine': 'refine'}[capability]
        report({'live': '正在离线检查英语同传与尾句收尾…', 'asr': '正在离线检查录音转写…',
                'refine': '正在离线检查录后精修…'}[stage])
        cues = []
        complete = False
        execution = None
        stable, count = None, samples

        def receive(event, folder, after_close=None):
            nonlocal complete, execution
            execution = str(folder)
            if event['event'] == 'cue':
                cues.append(dict(event['row'], after_close=after_close))
            if event['event'] == 'complete':
                complete = event.get('complete') is True

        try:
            binding = make_binding(capability)
            if stage == 'live':
                stable, count = run_live(audio, root, binding, cancel, receive)
            else:
                audio.execute(stage, CLIP, binding, cancel, receive, samples=samples, audio_hash=source_sha)
            tail = re.search(r'\blast\s+sentence\b', ' '.join(row['english'].lower() for row in cues)) is not None
            tail_after_close = any(row['after_close'] and 'sentence' in row['english'].lower() for row in cues)
            passed = complete and tail and (stage != 'live' or stable and tail_after_close)
            results[capability] = dict(passed=passed, complete=complete, input_samples=count,
                stable_before_close=stable, tail_found=tail, tail_after_close=tail_after_close,
                execution=execution, cues=[row['english'][:128] for row in cues[:24]],
                message='英语试音通过。' if passed else '未确认完整结束、稳定字幕或英语试音尾句，请查看执行详情。')
        except (OSError, CoreError, ValueError) as exc:
            results[capability] = dict(passed=False, message=str(exc)[:800], execution=execution)
    return results
