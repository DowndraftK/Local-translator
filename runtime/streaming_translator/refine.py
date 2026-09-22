"""A separate post-recording version; never replace the live transcript."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import math
import os
from pathlib import Path
import shutil
import time
import wave

from .store import SessionStore, atomic_json, file_sha256


def initialize_refinement(store, parent):
    parent = Path(parent).resolve()
    if parent == store.directory.resolve():
        raise ValueError('校对结果必须保存在新任务目录。')
    if not (parent/'session.sqlite').is_file() or not (parent/'audio.wav').is_file():
        raise ValueError('原任务缺少数据库或已保存的录音。')
    original = SessionStore(parent)
    try:
        if not original.get('session_id'):
            raise ValueError('原任务尚未建立。')
        store.initialize(translation_model=original.get('translation_model'),
            endpoint=original.get('endpoint'), input_kind='refinement',
            parent_session_id=original.get('session_id'), parent_session_path=str(parent),
            parent_asr_complete=original.get('asr_complete', False),
            expected_audio_sha256=original.get('audio_sha256'),
            translation_model_digest=original.get('translation_model_digest'))
    finally:
        original.close()


def result_tokens(result, duration):
    """Validate the complete result before creating any translation jobs."""
    batches = []
    for segment in result['segments']:
        words = segment.get('words') or [{'word': segment['text'],
                                         'start': segment['start'], 'end': segment['end']}]
        batch = []
        for word in words:
            start, end = float(word['start']), float(word['end'])
            if not all(math.isfinite(t) for t in (start, end)) or end < start:
                raise ValueError('校对结果包含无效时间戳；原始字幕未改动。')
            batch.append({'text': str(word['word']), 'start': max(0, min(duration, start)),
                          'end': max(0, min(duration, end))})
        batches.append(batch)
    return batches


def transcribe_local(audio_path, model):
    import soundfile as sf
    import mlx.core as mx
    import mlx_whisper
    audio, rate = sf.read(audio_path, dtype='float32')
    if rate != 16000 or audio.ndim != 1:
        raise ValueError('保存的音频不是 16 kHz 单声道。')
    if mx.default_device() != mx.gpu:
        raise ValueError('录后校对需要可用的 MLX GPU。')
    started = time.monotonic()
    result = mlx_whisper.transcribe(audio, path_or_hf_repo=str(Path(model).resolve()),
        language='en', word_timestamps=True, temperature=0.0, verbose=False)
    return result, time.monotonic()-started, {'backend': 'mlx-whisper',
        'mlx_default_device': str(mx.default_device()), 'mlx': mx.__version__,
        'temperature': 0.0, 'word_timestamps': True}


async def refine(store, config, parent, stop):
    from .asr import validate_resources
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=1, thread_name_prefix='refinement-gpu'))
    store.update(state='loading')
    _, manifest = await asyncio.to_thread(validate_resources, config)
    source = Path(parent)/'audio.wav'
    target = store.directory/'audio.wav'
    await asyncio.to_thread(shutil.copyfile, source, target)
    digest = await asyncio.to_thread(file_sha256, target)
    expected = store.get('expected_audio_sha256')
    if expected and digest != expected:
        raise ValueError('保存的录音与原任务校验值不符；请检查文件。')
    with wave.open(str(target), 'rb') as audio:
        if audio.getframerate() != 16000 or audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise ValueError('保存的音频格式不受支持。')
        samples = audio.getnframes()
    duration = samples/16000
    if duration > 7200:
        raise ValueError('本版录后校对限两小时以内，以控制整段解码的内存占用。')
    store.update(state='refining', audio_path=str(target.resolve()), audio_sha256=digest,
        audio_seconds=duration, input_samples=samples, model_revision=manifest['revision'])
    store.publish()
    if stop.is_set():
        return
    result, elapsed, evidence = await asyncio.to_thread(transcribe_local, target, config['model'])
    atomic_json(store.directory/'refinement.json', result)
    store.update(refinement_seconds=elapsed, device_evidence=evidence)
    if stop.is_set():
        return
    batches = result_tokens(result, duration)
    for sequence, batch in enumerate(batches):
        if stop.is_set():
            return
        store.ingest(sequence, batch)
        await asyncio.sleep(0)
    store.ingest(len(batches), [], final=True)
    store.update(received_pcm_samples=samples, received_audio_seconds=duration,
                 processed_audio_seconds=duration)
