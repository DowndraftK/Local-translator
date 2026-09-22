"""Bounded-memory conversion; archive a canonical local PCM copy before inference."""
from pathlib import Path
import wave


def prepare_audio(source, output):
    import av
    output = Path(output)
    temporary = output.with_suffix('.preparing.wav')
    with av.open(str(source)) as container, wave.open(str(temporary), 'wb') as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(16000)
        resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
        samples = 0
        for frame in container.decode(audio=0):
            for pcm in resampler.resample(frame):
                data = pcm.to_ndarray().astype('<i2', copy=False).tobytes()
                sink.writeframesraw(data)
                samples += len(data)//2
        for pcm in resampler.resample(None):
            data = pcm.to_ndarray().astype('<i2', copy=False).tobytes()
            sink.writeframesraw(data)
            samples += len(data)//2
    if not samples:
        raise ValueError('录音没有可解码的音频样本。')
    temporary.replace(output)
    return samples
