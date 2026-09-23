"""Archive pipe input independently of ASR backpressure, with a durable WAV header."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import wave


def pcm_prefix_digest(path, samples):
    digest = hashlib.sha256()
    with wave.open(str(path), 'rb') as stream:
        remaining = samples
        while remaining:
            data = stream.readframes(min(remaining, 65536))
            if not data:
                raise ValueError('已保存音频短于恢复检查点。')
            digest.update(data)
            remaining -= len(data)//2
    return digest.hexdigest()


def resource_digest(config):
    # Include the actual manifests and all behavior-affecting configuration.
    contents = {k: v for k, v in config.items() if k not in ('source', 'model', 'source_manifest', 'model_manifest')}
    for key in ('source_manifest', 'model_manifest'):
        contents[key] = json.loads(Path(config[key]).read_text())
    return hashlib.sha256(json.dumps(contents, sort_keys=True).encode()).hexdigest()


class PrefixHasher:
    def __init__(self, path):
        self.path = path
        self.samples = 0
        self.hasher = hashlib.sha256()

    def digest(self, samples):
        if samples < self.samples:
            raise ValueError('恢复检查点不能倒退。')
        with wave.open(str(self.path), 'rb') as audio:
            audio.setpos(self.samples)
            while self.samples < samples:
                data = audio.readframes(min(65536, samples-self.samples))
                if not data:
                    raise ValueError('恢复检查点超过已保存的音频。')
                self.hasher.update(data)
                self.samples += len(data)//2
        return self.hasher.hexdigest()


class PCMArchive:
    def __init__(self, store):
        self.store = store
        self.path = store.directory/'audio.wav'
        self.samples = 0
        self.done = asyncio.Event()
        self.error = None

    async def receive(self, stream=None):
        transport = None
        try:
            if stream is None:
                stream = asyncio.StreamReader(limit=65536)
                loop = asyncio.get_running_loop()
                transport, _ = await loop.connect_read_pipe(
                    lambda: asyncio.StreamReaderProtocol(stream), sys.stdin.buffer)
            with self.path.open('wb') as raw, wave.open(raw, 'wb') as media:
                media.setnchannels(1); media.setsampwidth(2); media.setframerate(16000)
                media.writeframes(b'')
                raw.flush(); os.fsync(raw.fileno())
                self.store.update(audio_path=str(self.path.resolve()), durable_audio_samples=0)
                partial = b''
                while True:
                    data = await stream.read(16000)
                    if not data:
                        if partial:
                            raise ValueError('PCM 输入以不完整的 16 位样本结束。')
                        break
                    data = partial + data
                    partial = data[len(data)//2*2:]
                    data = data[:len(data)//2*2]
                    if not data:
                        continue
                    media.writeframes(data)
                    raw.flush(); os.fsync(raw.fileno())
                    self.samples += len(data)//2
                    self.store.update(durable_audio_samples=self.samples,
                        audio_path=str(self.path.resolve()), audio_seconds=self.samples/16000,
                        received_audio_seconds=self.samples/16000)
        except BaseException as exc:
            self.error = exc
            raise
        finally:
            if transport:
                transport.close()
            self.done.set()


def validate_resume(store, config):
    """Read-only checks; callers must hold the session lock before rolling back."""
    if not store.get('session_id'):
        raise ValueError('任务目录没有可恢复的数据。')
    if store.get('asr_complete'):
        raise ValueError('此任务已完成识别；可继续补译或创建独立校对版。')
    audio = store.directory/'audio.wav'
    with wave.open(str(audio), 'rb') as media:
        if (media.getnchannels(), media.getsampwidth(), media.getframerate()) != (1, 2, 16000):
            raise ValueError('恢复音频必须是 16 kHz 单声道 PCM16。')
        samples = media.getnframes()
        if samples <= 0 or audio.stat().st_size < 44+samples*2:
            raise ValueError('已保存录音为空或不完整，无法恢复。')
    checkpoint = store.latest_checkpoint()
    fingerprint = resource_digest(config)
    expected = checkpoint['resources_digest'] if checkpoint else store.get('resources_digest')
    if expected and expected != fingerprint:
        raise ValueError('识别配置或资源清单已改变；请重新处理录音并保留原任务。')
    if checkpoint and (checkpoint['sample'] > samples or
                       pcm_prefix_digest(audio, checkpoint['sample']) != checkpoint['media_digest']):
        raise ValueError('录音与恢复检查点不一致，原任务未修改。')
    expected_hash = store.get('audio_sha256')
    if expected_hash:
        from .store import file_sha256
        if file_sha256(audio) != expected_hash:
            raise ValueError('已保存录音校验失败，原任务未修改。')
    return checkpoint
