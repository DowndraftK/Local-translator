"""Create original, synthetic speech with independent PCM waveform anchors.

This measures segment timing only on clean synthetic speech. It is not human
speech annotation, microphone validation, or a substitute for the TED cases.
"""
import argparse
import json
from pathlib import Path
import subprocess
import wave

import numpy as np
from streaming_translator.audio import prepare_audio

SENTENCES = [
    'Please open the blue notebook.',
    'The train leaves at fourteen minutes past nine.',
    'I did not approve the payment.',
    'The result is three point one four.',
    'Yes, yes, that is correct.',
    'We should keep the original recording.',
    'Please open the blue notebook.',
    'The second measurement is smaller.',
    'Doctor Smith will join us tomorrow.',
    'No, no, I meant the other room.',
    'A short pause comes after this sentence.',
    'The recording continues from this point.',
    'This example has a negative number.',
    'You must not delete the repeated words.',
    'The total is fourteen, not forty.',
    'My first answer was wrong, I mean my second answer.',
    'Very, very good work today.',
    'The final page contains two tables.',
    'We have reached the end of the lesson.',
    'Please save the last sentence.',
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    pieces, anchors, position = [], [], 0
    for i, sentence in enumerate(SENTENCES):
        aiff, wav = args.output/f'{i:02}.aiff', args.output/f'{i:02}.wav'
        subprocess.run(['say', '-v', 'Daniel', '-r', '155', '-o', str(aiff), sentence], check=True)
        prepare_audio(aiff, wav)
        with wave.open(str(wav), 'rb') as source:
            data = source.readframes(source.getnframes())
        samples = np.frombuffer(data, dtype='<i2')
        audible = np.flatnonzero(np.abs(samples.astype(np.int32)) >= 80)
        if len(audible) == 0: raise ValueError('Empty synthesis')
        padding = 6 if i in (0, 11) else 2
        pieces.append(bytes(16000*padding*2)); position += 16000*padding
        anchors.append({'id': i+1, 'text': sentence, 'start_sample': int(position+audible[0]),
            'end_sample': int(position+audible[-1]+1), 'uncertainty_seconds': .08,
            'method': 'Known isolated utterance; outer PCM amplitude >=80/32768; +/-80 ms low-energy phoneme uncertainty'})
        pieces.append(data); position += len(samples)
        if i == 10: pause_sample = position
    with wave.open(str(args.output/'alignment.wav'), 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
        out.writeframes(b''.join(pieces))
    (args.output/'anchors.json').write_text(json.dumps({'scope': 'synthetic clean speech; not human listening annotations',
        'created_before_inference': True, 'rate': 16000, 'pause_sample': pause_sample, 'anchors': anchors}, indent=2))
    (args.output/'reference.txt').write_text('\n'.join(SENTENCES)+'\n')
    print(json.dumps({'samples': position, 'seconds': position/16000, 'anchors': len(anchors), 'pause_sample': pause_sample}))


if __name__ == '__main__':
    main()
