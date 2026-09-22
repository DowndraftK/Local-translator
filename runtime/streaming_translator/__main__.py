"""CLI worker for the native app. No network listener and no implicit downloads."""
import argparse
import asyncio
import fcntl
import json
import logging
from pathlib import Path
import signal
import sys
import time

from .store import SessionStore, atomic_json, export_subtitles


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    config = sub.add_parser('configure')
    config.add_argument('--project', type=Path, required=True)
    config.add_argument('--output', type=Path, required=True)
    for name in ['run', 'refine', 'retry', 'status', 'export', 'revise']:
        p = sub.add_parser(name)
        p.add_argument('--session', type=Path, required=True)
        if name == 'refine':
            p.add_argument('--from-session', type=Path, required=True)
            p.add_argument('--config', type=Path, required=True)
        if name == 'run':
            p.add_argument('--config', type=Path, required=True)
            audio = p.add_mutually_exclusive_group(required=True)
            audio.add_argument('--input', type=Path)
            audio.add_argument('--stdin-pcm', action='store_true')
            p.add_argument('--paced', action='store_true')
            p.add_argument('--max-audio-seconds', type=float)
            p.add_argument('--device', choices=['mps', 'cpu'])
            p.add_argument('--dtype', choices=['float32', 'float16'])
            p.add_argument('--coalesce-seconds', type=float)
            p.add_argument('--max-context-tokens', type=int)
            p.add_argument('--translation-model', default='hy-mt2:1.8b-q8')
            p.add_argument('--no-translation', action='store_true')
            p.add_argument('--endpoint', default='http://127.0.0.1:11434')
        if name == 'retry':
            p.add_argument('--retry-failed', action='store_true')
        if name == 'export':
            p.add_argument('--format', choices=['srt', 'vtt', 'txt'], required=True)
            p.add_argument('--output', type=Path, required=True)
        if name == 'revise':
            p.add_argument('--segment', type=int, required=True)
            p.add_argument('--text-file', type=Path, required=True)
    return parser.parse_args()


async def translation_loop(store, done, stop):
    from .ollama import LocalTranslator
    model = store.get('translation_model')
    if not model:
        await done.wait()
        return
    translator = LocalTranslator(store.get('endpoint'), model)
    translator.digest = store.get('translation_model_digest')
    try:
        while not stop.is_set():
            job = store.claim()
            if not job:
                if done.is_set():
                    return
                await asyncio.sleep(.1)
                continue
            try:
                result = await translator.translate(job['english'])
                if translator.digest:
                    store.update(translation_model_digest=translator.digest)
                store.finish(job, result=result)
            except asyncio.CancelledError:
                # Leave the running lease for explicit recovery on the next invocation.
                raise
            except Exception as exc:
                store.finish(job, error=str(exc))
                logging.warning('Translation %s failed: %s', job['id'], exc)
    finally:
        await translator.close()


async def execute(args, store):
    stop, done = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    started = time.monotonic()
    if args.command == 'run':
        if args.max_audio_seconds is not None and args.max_audio_seconds <= 0:
            raise ValueError('测试音频长度必须大于零。')
        config = json.loads(args.config.read_text())
        for key in ['device', 'dtype']:
            if getattr(args, key):
                config[key] = getattr(args, key)
        if args.coalesce_seconds is not None:
            if not .5 <= args.coalesce_seconds <= 5:
                raise ValueError('识别合并间隔须在 0.5–5 秒之间。')
            config['coalesce_seconds'] = args.coalesce_seconds
        if args.max_context_tokens is not None:
            if not 16 <= args.max_context_tokens <= 428:
                raise ValueError('历史文字上下文须在 16–428 个 token 之间。')
            config['max_context_tokens'] = args.max_context_tokens
        store.initialize(translation_model=None if args.no_translation else args.translation_model,
                         endpoint=args.endpoint, input_kind='microphone' if args.stdin_pcm else 'file',
                         playback_mode='paced' if args.paced else 'batch',
                         requested_audio_limit=args.max_audio_seconds)
        atomic_json(store.directory/'runtime.json', config)
    elif args.command == 'refine':
        from .refine import initialize_refinement
        config = json.loads(args.config.read_text())
        initialize_refinement(store, args.from_session)
        atomic_json(store.directory/'runtime.json', config)
    else:
        if not store.get('session_id'):
            raise ValueError('任务目录没有可恢复的数据。')
        store.recover(retry_failed=args.retry_failed)
        store.update(state='translating', stopped=False)
        done.set()

    async def publish_periodically():
        with (store.directory/'progress.jsonl').open('a') as progress:
            while True:
                snapshot = store.publish()
                progress.write(json.dumps({
                    'time': time.time(), 'state': snapshot.get('state'),
                    'received_audio_seconds': snapshot.get('received_audio_seconds'),
                    'asr_queue_seconds': snapshot.get('asr_queue_seconds'),
                    'translation_counts': snapshot['translation_counts'],
                    'segments': len(snapshot['segments']),
                })+'\n')
                progress.flush()
                await asyncio.sleep(.5)

    publisher = asyncio.create_task(publish_periodically())
    translator = asyncio.create_task(translation_loop(store, done, stop))
    stop_task = asyncio.create_task(stop.wait())
    error = None
    try:
        if args.command in ('run', 'refine'):
            try:
                if args.command == 'refine':
                    from .refine import refine
                    await refine(store, config, args.from_session, stop)
                else:
                    from .asr import recognize
                    await recognize(store, config, args.input, args.paced, stop,
                                    args.max_audio_seconds, stdin_pcm=args.stdin_pcm)
            except Exception as exc:
                error = str(exc)
                store.update(asr_error=error)
                logging.exception('ASR failed; saved English and pending jobs are retained')
            finally:
                done.set()
        store.update(state='translating')
        finished, _ = await asyncio.wait([translator, stop_task], return_when=asyncio.FIRST_COMPLETED)
        if stop_task in finished and not translator.done():
            translator.cancel()
        await asyncio.gather(translator, return_exceptions=True)
        if translator.done() and not translator.cancelled() and translator.exception():
            error = str(translator.exception())
    finally:
        for task in (publisher, translator, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(publisher, translator, stop_task, return_exceptions=True)
        counts = store.snapshot()['translation_counts']
        state = ('stopped' if stop.is_set() else 'failed' if error else
                 'needs_translation' if counts['failed'] or counts['pending'] or counts['running'] else
                 'completed' if store.get('asr_complete') else 'incomplete_asr')
        store.update(state=state, last_operation_seconds=time.monotonic()-started,
                     error=error, finished_at=time.time())
        snapshot = store.publish()
        for kind in ('txt', 'srt', 'vtt'):
            (store.directory / f'subtitles.{kind}').write_text(export_subtitles(snapshot, kind))
    print(json.dumps({'event': 'finished', 'state': state, 'segments': len(snapshot['segments']),
                      'translation_counts': counts}, ensure_ascii=False), flush=True)
    return 1 if error else 0


def main():
    args = arguments()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s', stream=sys.stderr)
    if args.command == 'configure':
        root = args.project.resolve()
        config = {'source': str(root/'artifacts/whisperlivekit-mps-20260915/source'),
                  'source_manifest': str(root/'artifacts/whisperlivekit-mps-20260915/patched-source.json'),
                  'model': str(root/'models/whisper-mps-experiment/large-v3-turbo'),
                  'model_manifest': str(root/'experiments/whisperlivekit/mps-model-manifest.json'),
                  'device': 'mps', 'dtype': 'float32', 'max_context_tokens': 128}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(args.output, config)
        return 0
    source_lock = None
    if args.command == 'refine':
        if args.from_session.resolve() == args.session.resolve():
            raise ValueError('校对结果必须保存在新任务目录，不能覆盖原始字幕。')
        if not (args.from_session/'session.sqlite').is_file():
            raise ValueError('未找到原录音任务。')
        source_lock = (args.from_session/'worker.lock').open('a')
        try:
            fcntl.flock(source_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            source_lock.close()
            raise ValueError('请先停止原任务，再进行录后校对。')
    args.session.mkdir(parents=True, exist_ok=True)
    lock = (args.session/'worker.lock').open('a')
    if args.command != 'status':
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('此任务已有工作进程运行；停止后再重试、修改或导出。')
    store = SessionStore(args.session)
    try:
        if args.command == 'status':
            print(json.dumps(store.snapshot(), ensure_ascii=False))
            return 0
        if args.command == 'export':
            source = store.get('source_path')
            target = args.output.resolve()
            protected = [args.session.resolve()/name for name in (
                'session.sqlite', 'session.sqlite-wal', 'session.sqlite-shm', 'worker.lock',
                'snapshot.json', 'runtime.json', 'audio.wav', 'warmup.wav', 'progress.jsonl', 'refinement.json')]
            if target in protected or (source and target == Path(source).resolve()):
                raise ValueError('导出路径不能覆盖原录音或任务数据库。')
            args.output.write_text(export_subtitles(store.snapshot(), args.format))
            return 0
        if args.command == 'revise':
            store.revise(args.segment, args.text_file.read_text())
            store.publish()
            return 0
        return asyncio.run(execute(args, store))
    finally:
        store.close()
        lock.close()
        if source_lock:
            source_lock.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
