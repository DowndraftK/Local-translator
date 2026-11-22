"""Owned speech-interpreter entry point. No CLI default endpoints or capture."""
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sys
import threading
import time
import wave

ACTIVE_EXECUTION = None
FACTS = None


def emit(kind, **fields):
    if ACTIVE_EXECUTION is not None:
        fields['execution'] = ACTIVE_EXECUTION
    print(json.dumps(dict(event=kind, **fields), ensure_ascii=False, allow_nan=False), file=FACTS, flush=True)


def transcribe_refinement(audio_path, model, language='en'):
    import soundfile as sf
    import mlx.core as mx
    import mlx_whisper
    audio, rate = sf.read(audio_path, dtype='float32')
    if rate != 16000 or audio.ndim != 1 or len(audio) > 9_600_000:
        raise ValueError('audio_limit: 精修输入须为最多10分钟规范音频')
    if mx.default_device() != mx.gpu:
        raise ValueError('录后精修需要可用的MLX GPU。')
    started = time.monotonic()
    # Fixed, single repair for the preserved T07 cross-window repetition case.
    # Preserve every returned word and the unmodified complete model response.
    result = mlx_whisper.transcribe(audio, path_or_hf_repo=str(Path(model).resolve()),
        language=language, word_timestamps=True, temperature=0.0, verbose=False, condition_on_previous_text=False)
    return result, time.monotonic() - started, dict(backend='mlx-whisper', mlx_default_device=str(mx.default_device()),
        mlx=mx.__version__, temperature=0.0, word_timestamps=True, condition_on_previous_text=False,
        profile='t08-refine-independent-windows-v1')


def decode(request):
    import av
    source = Path(request['source']); output = Path(request['output'])
    temporary = output.with_suffix('.preparing.wav')
    samples = 0; first_pts = None; nonzero_samples = 0
    declared_samples = None
    if request['suffix'] == '.wav':
        try:
            with wave.open(str(source), 'rb') as declared:
                declared_samples = round(declared.getnframes() * 16000 / declared.getframerate())
        except wave.Error:
            pass  # PyAV can decode WAV codecs outside the stdlib PCM reader.
    try:
        with av.open(str(source)) as media, wave.open(str(temporary), 'wb') as sink:
            if not media.streams.audio: raise ValueError('audio_empty: 文件没有音轨')
            track = media.streams.audio[0]
            formats = {'.wav': ('wav',), '.aiff': ('aiff',), '.aif': ('aiff',), '.mp3': ('mp3',),
                       '.flac': ('flac',), '.ogg': ('ogg',), '.m4a': ('mov', 'mp4', 'm4a', '3gp', '3g2', 'mj2')}
            if not set(media.format.name.split(',')).intersection(formats[request['suffix']]):
                raise ValueError('audio_format: 后缀与实际容器不符')
            rate = track.codec_context.sample_rate; channels = len(track.codec_context.layout.channels)
            if not 8000 <= rate <= 192000 or not 1 <= channels <= 8:
                raise ValueError('audio_limit: 采样率或声道超限')
            info = dict(container=media.format.name, codec=track.codec_context.name, track=track.index,
                        sourceRate=rate, sourceChannels=channels, startPTS=track.start_time,
                        timeBase=str(track.time_base), metadataDuration=media.duration,
                        ptsPolicy='concatenate decoded frames, no inserted PTS gaps', codecDelay='not_exposed')
            sink.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            converter = av.AudioResampler(format='s16', layout='mono', rate=16000)
            def write(frames):
                nonlocal samples, nonzero_samples
                for pcm in frames:
                    values = pcm.to_ndarray().astype('<i2', copy=False)
                    nonzero_samples += int((values != 0).sum())
                    data = values.tobytes()
                    samples += len(data) // 2
                    if samples > 28_800_000: raise ValueError('audio_limit: 实际解码超过30分钟')
                    sink.writeframesraw(data)
            for frame in media.decode(track):
                if frame.samples > 1_048_576: raise ValueError('audio_limit: 解码单帧超限')
                if first_pts is None: first_pts = frame.pts
                write(converter.resample(frame))
            write(converter.resample(None))
        if not samples: raise ValueError('audio_empty: 没有可解码的样本')
        if declared_samples is not None and abs(samples - declared_samples) > 2:
            raise ValueError('audio_decode: WAV声明样本数与实际解码不符，来源可能截断')
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno()); fingerprint = hashlib.file_digest(stream, 'sha256').hexdigest()
        if temporary.stat().st_size != 44 + samples * 2: raise ValueError('audio_incomplete: WAV不完整')
        temporary.replace(output); info['firstFramePTS'] = first_pts
        emit('decoded', pcm=dict(contract='audio-pcm-v1', rate=16000, channels=1, sampleWidth=2,
            samples=samples, fingerprint=fingerprint, byteCount=44 + samples * 2,
            filename='pcm-' + fingerprint + '.wav', media=info, exactZeroPCM=nonzero_samples == 0))
    finally:
        temporary.unlink(missing_ok=True)


def execution_model_view(config, folder):
    if config.get('model_variant') != 'small.en':
        return config
    model = Path(config['model'])
    checkpoint = model/'model.safetensors'
    metadata = model/'config.json'
    if not checkpoint.is_file() or not metadata.is_file():
        raise ValueError('缺少 Whisper small.en 的固定模型文件。')
    # Only expose the bound checkpoint to both loaders. SimulStreaming uses
    # the directory suffix to select the English-only tokenizer.
    view = folder/'small.en'
    view.mkdir(mode=0o700)
    (view/'config.json').symlink_to(metadata)
    (view/'model.safetensors').symlink_to(checkpoint)
    (view/'weights.safetensors').symlink_to(checkpoint)
    return dict(config, model=str(view))


async def speech(request):
    # runtime/ bytes are the fixed speech.bridge resource, verified before launch.
    sys.path.insert(0, request['bridge'])
    from streaming_translator.store import SessionStore, atomic_json
    from streaming_translator.lifecycle import release_speech_resources
    folder = Path(request['folder'])
    config = execution_model_view(request['config'], folder)
    stop = asyncio.Event(); loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop.set)
    class ExecutionFacts(SessionStore):
        last_progress = 0.0
        def update(self, **values):
            super().update(**values)
            if request['stage'] == 'live' and time.monotonic() - self.last_progress >= .5:
                self.last_progress = time.monotonic()
                emit('live_progress', evidence={k: self.get(k) for k in ('submitted_audio_samples',
                    'processed_audio_seconds', 'asr_backlog_seconds', 'asr_queue_seconds', 'state', 'load_seconds')})
        def publish(self):
            super().publish()
            emit('state', state=self.get('state'), pending=self.get('pending_tokens', []) != [],
                 evidence={k: self.get(k) for k in ('device_evidence', 'load_seconds', 'asr_calls',
                    'asr_call_seconds', 'queue_peak_seconds', 'vad_active_samples', 'vad_silence_samples',
                    'encoder_lifecycle', 'model_revision', 'asr_complete', 'received_pcm_samples')})
        def ingest(self, sequence, tokens, **flags):
            ids = super().ingest(sequence, tokens, **flags)
            for index in ids:
                row = dict(self.db.execute('SELECT * FROM segments WHERE id=?', (index,)).fetchone())
                evidence = self.db.execute('SELECT evidence FROM segment_timing WHERE segment_id=?', (index,)).fetchone()
                row['timing'] = json.loads(evidence[0]) if evidence else {}
                emit('cue', row=row)
            return ids
        def flush_confirmed_pause(self, timestamp):
            from streaming_translator.segmentation import take_segments
            pending = self.get('pending_tokens', [])
            if not pending:
                return
            with self.transaction():
                segments, remaining = take_segments(pending, final=True)
                for segment in segments:
                    segment['boundary'] = 'vad_pause'
                ids = [self._add_segment(segment) for segment in segments]
                self.set('pending_tokens', remaining)
                self.set('last_confirmed_pause', dict(audio_seconds=timestamp, wall=time.time(),
                    after_ASR_sequence=self.next_sequence()-1, segments=ids))
            for index in ids:
                row = dict(self.db.execute('SELECT * FROM segments WHERE id=?', (index,)).fetchone())
                row['timing'] = json.loads(self.db.execute(
                    'SELECT evidence FROM segment_timing WHERE segment_id=?', (index,)).fetchone()[0])
                emit('cue', row=row)
        def checkpoint(self, sample, media_digest, resources_digest):
            super().checkpoint(sample, media_digest, resources_digest)
            emit('checkpoint', checkpoint=self.latest_checkpoint())
    store = ExecutionFacts(folder)
    try:
        live = request['stage'] == 'live'
        if not live: shutil.copyfile(request['source'], folder / 'audio.wav')
        store.initialize(translation_model=None, input_kind='T08-owned-execution-facts' if live else 'T07-owned-execution-facts', endpoint='http://127.0.0.1:1')
        if live:
            sys.path.insert(0, str(Path(__file__).parent))
            from capture_speech import configure
            configure(request, stop, emit, store)
        else:
            store.update(audio_seconds=request['samples'] / 16000, input_samples=request['samples'],
                         resume_sample=request.get('offset', 0), audio_sha256=request['audio_hash'],
                         pending_tokens=request.get('pending_tokens', []))
        if request['stage'] in ('asr', 'live'):
            from streaming_translator.asr import recognize
            await recognize(store, config, None, False, stop, resume=not live, stdin_pcm=live)
            emit('complete', complete=bool(store.get('asr_complete')), evidence=store.snapshot(limit=0))
        else:
            if request['samples'] > 9_600_000: raise ValueError('audio_limit: 精修超过10分钟')
            sys.path.insert(0, config['source'])
            from streaming_translator.resources import apply_asset_mapping
            apply_asset_mapping('refinement')
            from streaming_translator.refine import result_tokens
            store.update(state='refining'); store.publish()
            result, elapsed, evidence = await asyncio.to_thread(
                transcribe_refinement, folder / 'audio.wav', config['model'], config.get('language','en'))
            atomic_json(folder / 'refinement.json', result)
            if stop.is_set(): return
            batches = result_tokens(result, request['samples'] / 16000)
            for sequence, batch in enumerate(batches):
                if stop.is_set(): return
                store.ingest(sequence, batch)
            store.ingest(len(batches), [], final=True)
            emit('complete', complete=True, evidence=dict(refinement_seconds=elapsed, device_evidence=evidence,
                 model_revision=request['model_revision']))
    finally:
        store.close()
        if request.get('retain_model'):
            emit('session_released', evidence=dict(model_retained=True))
        else:
            evidence = await asyncio.to_thread(release_speech_resources)
            emit('released', evidence=evidence)


async def live_host(request):
    global ACTIVE_EXECUTION
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        while raw := await asyncio.to_thread(sys.stdin.buffer.readline, 16385):
            if len(raw)>16384:
                raise ValueError('Speech host command exceeds capacity')
            command = json.loads(raw)
            if command.get('action')=='shutdown':
                break
            if command.get('action')!='recognize':
                raise ValueError('Unknown speech host command')
            path = Path(command['request'])
            if not path.is_relative_to(Path(request['folder']).parent) or path.is_symlink():
                raise ValueError('Speech session path outside owned root')
            current = json.loads(path.read_bytes())
            if current.get('stage')!='live' or not current.get('retain_model'):
                raise ValueError('Invalid reusable speech session')
            ACTIVE_EXECUTION = current['execution']
            success = False
            message = None
            try:
                await speech(current)
                success = True
            except Exception as exc:
                message = str(exc)
                emit('failure', message=message, exception=type(exc).__name__)
            finally:
                emit('live_finished', success=success, message=message)
                ACTIVE_EXECUTION = None
    finally:
        from streaming_translator.lifecycle import release_speech_resources
        emit('released', evidence=await asyncio.to_thread(release_speech_resources))


def main():
    global FACTS
    request = json.loads(Path(sys.argv[1]).read_bytes())
    expected_parent = request.get('parent_pid')
    if type(expected_parent) is int:
        def watch_parent():
            while os.getppid() == expected_parent:
                time.sleep(.5)
            (Path(request['folder']) / 'parent-ended.json').write_text(json.dumps(dict(parent_pid=expected_parent,
                observed_parent=os.getppid(), wall=time.time(), forced_exit_after_seconds=10)))
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(10)
            os._exit(3)
        threading.Thread(target=watch_parent, name='owned-parent-watch', daemon=True).start()
    if request['stage'] == 'live_host': asyncio.run(live_host(request))
    elif request['stage'] == 'decode': decode(request)
    elif request['config'].get('backend') == 'qwen3-vllm-metal':
        FACTS = os.fdopen(os.dup(sys.stdout.fileno()), 'w', encoding='utf-8')
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        sys.path.insert(0, str(Path(__file__).parent))
        from qwen_audio_helper import recognize
        recognize(request, emit)
    else: asyncio.run(speech(request))


if __name__ == '__main__':
    try: main()
    except BaseException as exc:
        emit('failure', message=str(exc), exception=type(exc).__name__)
        raise
