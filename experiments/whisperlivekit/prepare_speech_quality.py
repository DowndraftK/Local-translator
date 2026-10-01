"""Rebuild the 0.2.6 patch from the reviewed upstream snapshot, never in place."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path

from prepare_mps_trial import prepare as prepare_mps


def prepare(source, destination, stage='input-clock'):
    expected = json.loads(Path(__file__).with_name('speech-quality-baseline.json').read_text())
    for name, digest in expected.items():
        if hashlib.sha256((source/name).read_bytes()).hexdigest() != digest:
            raise ValueError(f'Quality patch baseline differs: {name}')
    prepare_mps(source, destination)
    changes = {}

    def replace(name, old, new):
        path = destination / name
        text = path.read_text()
        if text.count(old) != 1:
            raise RuntimeError(f'Expected exactly one replacement: {name}')
        changes.setdefault(name, text)
        path.write_text(text.replace(old, new))

    base = 'whisperlivekit/simul_whisper/align_att_base.py'
    # _split_tokens intentionally withholds the unfinished final word from the
    # decoder prefix. It must also withhold it from the public committed output.
    replace(base,
        '        timestamped_words = self._build_timestamped_words(\n'
        '            split_words, split_tokens, token_timestamps\n'
        '        )\n',
        '        committed_count = len(split_words) if fire_detected or is_last else max(0, len(split_words) - 1)\n'
        '        timestamped_words = self._build_timestamped_words(\n'
        '            split_words[:committed_count], split_tokens[:committed_count], token_timestamps\n'
        '        )\n')
    if stage in ('safe-commit', 'media-time', 'continuation-eot', 'window-clock', 'pause-boundary', 'input-clock'):
        backend = 'whisperlivekit/simul_whisper/backend.py'
        original = (destination/backend).read_text()
        start = original.index('            stable_words = self._filter_stable_words(timestamped_words)')
        end = original.index('            self.buffer = []\n            self._last_committed_end', start)
        replace(backend, original[start:end],
            '            # infer() now returns only decoder-confirmed tokens. Timestamp jitter\n'
            '            # is not evidence that a token is a replay; never delete its text.\n'
            '            stable_words = timestamped_words\n'
            '            self.quality_notices = []\n'
            '            if any(float(t.end or 0) < self._last_committed_end - 1 for t in stable_words):\n'
            '                logger.warning("QUALITY_REVIEW timestamp_rewind end=%s raw=%s", self.end, stable_words)\n'
            '                self.quality_notices.append({"kind": "timestamp_rewind", "end": self.end})\n'
            '            if self._has_repetition_loop(self._recent_words + self._words_from_tokens(stable_words)):\n'
            '                logger.warning("QUALITY_REVIEW possible_repetition end=%s raw=%s", self.end, stable_words)\n'
            '                self.quality_notices.append({"kind": "possible_repetition", "end": self.end})\n'
            '            # Keep audio, the committed prefix and pending words. A string\n'
            '            # repetition heuristic cannot justify destroying a live window.\n')
        original = (destination/backend).read_text()
        start = original.index('    def _reset_after_unstable_output')
        end = original.index('    def _remember_committed_words', start)
        replace(backend, original[start:end], '')
    if stage in ('media-time', 'continuation-eot', 'window-clock', 'pause-boundary', 'input-clock'):
        replace('whisperlivekit/audio_processor.py',
            'self.transcription.end_silence(item.duration, self.state.tokens[-1].end if self.state.tokens else 0)',
            'self.transcription.end_silence(item.duration, item.start or 0.0)')
    if stage in ('continuation-eot', 'window-clock', 'pause-boundary', 'input-clock'):
        replace(base, '            if new_segment:\n                logits = self._suppress_blank_tokens(logits)',
            '            # A continuation must be allowed to end without inventing words.\n'
            '            if new_segment and not any(t.shape[1] for t in self.state.tokens[1:]):\n'
            '                logits = self._suppress_blank_tokens(logits)')
    if stage in ('window-clock', 'pause-boundary', 'input-clock'):
        replace(base, '        self.state.tokens.append(new_tokens_tensor)',
            '        self.state.tokens.append(new_tokens_tensor)\n'
            '        self.state.token_times.append(token_timestamps[:len(new_hypothesis)])')
        simul = 'whisperlivekit/simul_whisper/simul_whisper.py'
        replace(simul, '        self.state.tokens = [self.state.initial_tokens]\n',
            '        self.state.tokens = [self.state.initial_tokens]\n'
            '        self.state.token_times = []\n')
        replace(simul,
            '            if len(self.state.tokens) > 1:\n'
            '                self.state.context.append_token_ids(self.state.tokens[1][0, :].tolist())\n'
            '                self.state.tokens = [self.state.initial_tokens] + self.state.tokens[2:]',
            '            # Input chunks and inference batches are not one-to-one: silence\n'
            '            # flushes add calls and coalescing merges input chunks. Trim the\n'
            '            # confirmed prefix by its audio coordinates, not list position.\n'
            '            while len(self.state.tokens) > 1:\n'
            '                times = self.state.token_times[0]\n'
            '                count = next((i for i, t in enumerate(times) if t >= self.state.cumulative_time_offset), len(times))\n'
            '                if count:\n'
            '                    self.state.context.append_token_ids(self.state.tokens[1][0, :count].tolist())\n'
            '                if count == self.state.tokens[1].shape[1]:\n'
            '                    self.state.tokens.pop(1)\n'
            '                    self.state.token_times.pop(0)\n'
            '                else:\n'
            '                    self.state.tokens[1] = self.state.tokens[1][:, count:]\n'
            '                    self.state.token_times[0] = times[count:]\n'
            '                    break')
    if stage in ('pause-boundary', 'input-clock'):
        replace('whisperlivekit/audio_processor.py',
            '                await self._queue_tokens_for_translation(new_tokens)\n',
            '                await self._queue_tokens_for_translation(new_tokens)\n'
            '                if isinstance(item, Silence) and item.has_ended:\n'
            '                    await self._emit_stream_event_after_snapshot("silence_transcription_ended", item.end or 0.0)\n')
    if stage == 'input-clock':
        replace(base, '        self.state.token_times.append(token_timestamps[:len(new_hypothesis)])',
            '        # Prefix eviction follows the actual input horizon that confirmed\n'
            '        # this batch, never uncertain per-token attention estimates.\n'
            '        self.state.token_times.append([self.state.cumulative_time_offset + self.segments_len()] * len(new_hypothesis))')
        replace('whisperlivekit/simul_whisper/simul_whisper.py',
            'if t >= self.state.cumulative_time_offset',
            'if t > self.state.cumulative_time_offset')
    manifest_path = destination.parent / 'patched-source.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['quality_patch'] = '0.2.6-' + stage
    # Seal all executable source, including previously unmodified alignment and
    # audio processor files. Do not bless changes dynamically at application run.
    manifest['patched_files'] = {str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((destination/'whisperlivekit').rglob('*.py'))}
    manifest_path.write_text(json.dumps(manifest, indent=2)+'\n')
    patch = ''.join(''.join(difflib.unified_diff(old.splitlines(keepends=True),
        (destination/name).read_text().splitlines(keepends=True), fromfile='a/'+name, tofile='b/'+name))
        for name, old in changes.items())
    (destination.parent/'speech-quality.patch').write_text(patch)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--stage', choices=['committed-only', 'safe-commit', 'media-time', 'continuation-eot', 'window-clock', 'pause-boundary', 'input-clock'], default='input-clock')
    args = parser.parse_args()
    prepare(args.source.resolve(), args.destination.resolve(), args.stage)
