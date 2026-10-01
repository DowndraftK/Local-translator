"""Build an isolated continuation of the reviewed speech patch with decoder evidence."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import textwrap

from prepare_speech_quality import prepare as prepare_quality


def prepare(source, destination, variant='diagnostic', max_inference_seconds=1.0,
            prefix_retention='input-horizon', terminal_padding_room=False):
    if not 0.5 <= max_inference_seconds <= 2.0:
        raise ValueError('Inference audio limit must be between 0.5 and 2 seconds')
    prepare_quality(source, destination)
    changes = {}
    bounded = variant == 'bounded-acoustic-terminal'
    if bounded:
        variant = 'acoustic-terminal'

    def replace(name, old, new):
        path = destination / name
        text = path.read_text()
        if text.count(old) != 1:
            raise RuntimeError(f'Expected one repair replacement: {name}: {old[:60]}')
        changes.setdefault(name, text)
        path.write_text(text.replace(old, new))

    base = 'whisperlivekit/simul_whisper/align_att_base.py'
    replace(base, '        most_attended_frame = None\n',
        '        most_attended_frame = None\n'
        '        stop_reason = "context_limit"\n'
        '        attend_before = self.state.last_attend_frame\n')
    replace(base, '                current_tokens = current_tokens[:, :token_len_before]\n                break',
        '                stop_reason = "generation_limit"\n'
        '                current_tokens = current_tokens[:, :token_len_before]\n                break')
    replace(base, '            if new_segment and self._check_no_speech(logits):\n                break',
        '            if new_segment and self._check_no_speech(logits):\n'
        '                stop_reason = "no_speech"\n                break')
    replace(base, '            if completed:\n                current_tokens = current_tokens[:, :-1]',
        '            if completed:\n                stop_reason = "eot"\n'
        '                current_tokens = current_tokens[:, :-1]')
    replace(base, '                    current_tokens = self._rewind_tokens()\n                    break',
        '                    stop_reason = "attention_rewind"\n'
        '                    current_tokens = self._rewind_tokens()\n                    break')
    replace(base, '                current_tokens = current_tokens[:, :-1]\n                break\n\n        # Post-decode',
        '                stop_reason = "attention_horizon"\n'
        '                current_tokens = current_tokens[:, :-1]\n                break\n\n        # Post-decode')
    replace(base, '        logger.info(f"Output: {self.tokenizer.decode(new_hypothesis)}")',
        '        logger.info("DECODER_TRACE %s", {"stop": stop_reason, "final": is_last,\n'
        '            "input_end": self.state.global_time_offset + self.state.cumulative_time_offset + self.segments_len(),\n'
        '            "window_start": self.state.global_time_offset + self.state.cumulative_time_offset,\n'
        '            "window_seconds": self.segments_len(), "prefix_before": token_len_before,\n'
        '            "prefix_after_decode": current_tokens.shape[1], "new_tokens": len(new_hypothesis),\n'
        '            "attend_before": attend_before, "attend_after": most_attended_frame,\n'
        '            "audio_frames": content_mel_len, "attempted_tokens": tokens_produced})\n'
        '        logger.info(f"Output: {self.tokenizer.decode(new_hypothesis)}")')

    if variant in ('keep-progress', 'aligned-progress', 'terminal-progress', 'aligned-terminal', 'acoustic-terminal'):
        replace(base, '                    current_tokens = self._rewind_tokens()',
            '                    # Only the candidate whose attention rewound is unconfirmed.\n'
            '                    # Keep the preceding accepted tokens, including their prompt\n'
            '                    # coordinates, instead of erasing an entire decode pass.\n'
            '                    current_tokens = current_tokens[:, :-1]')

    if variant in ('terminal-progress', 'aligned-terminal', 'acoustic-terminal'):
        replace(base, '            if content_mel_len - most_attended_frame <= (\n                4 if is_last else self.cfg.frame_threshold\n            ):',
            '            # Streaming needs future audio to confirm an edge word. EOF and\n'
            '            # a pause have no future input: let the existing padded encoder\n'
            '            # finish at EOT instead of throwing away the final candidate.\n'
            '            if not is_last and content_mel_len - most_attended_frame <= self.cfg.frame_threshold:')

    if variant in ('aligned-progress', 'aligned-terminal', 'acoustic-terminal'):
        # Alignment appends EOT for a second, bounded forward pass. Reserve
        # its position even if a difficult decode fills the text context.
        replace(base, '        while not completed and current_tokens.shape[1] < self.max_text_len:',
            '        decode_limit = self.max_text_len - 1\n'
            '        while not completed and current_tokens.shape[1] < decode_limit:')
        simul = 'whisperlivekit/simul_whisper/simul_whisper.py'
        helper = Path(__file__).with_name('stream_alignment.py').read_text()
        helper = helper[helper.index('def _align_committed_to_audio'):]
        if prefix_retention == 'input-horizon':
            rewrite = ('    position = 0\n'
                '    for index, batch in enumerate(self.state.tokens[1:]):\n'
                '        count = batch.shape[1]\n'
                '        self.state.token_times[index] = ends[position:position + count]\n'
                '        position += count\n')
            if helper.count(rewrite) != 1:
                raise RuntimeError('Expected one prefix alignment rewrite')
            helper = helper.replace(rewrite,
                '    # Display alignment must never move an already confirmed prefix\n'
                '    # back into or out of the live decoder window. Its retention\n'
                '    # coordinates remain frozen at the original confirmation horizon.\n')
        replace(simul, '    def _current_tokens(self):', textwrap.indent(helper, '    ') + '\n    def _current_tokens(self):')
        call = 'new_hypothesis, acoustic_onsets=True)' if variant == 'acoustic-terminal' else 'new_hypothesis)'
        replace(base, '        new_tokens_tensor = self._make_new_tokens_tensor(new_hypothesis)',
            '        alignment = (self._align_committed_to_audio(current_tokens[:, :token_len_before], encoder_feature, content_mel_len, ' + call + '\n'
            '            if hasattr(self, "_align_committed_to_audio") else None)\n'
            '        new_tokens_tensor = self._make_new_tokens_tensor(new_hypothesis)')
        if prefix_retention == 'alignment':
            replace(base, '        # Prefix eviction follows the actual input horizon that confirmed\n'
                '        # this batch, never uncertain per-token attention estimates.\n'
                '        self.state.token_times.append([self.state.cumulative_time_offset + self.segments_len()] * len(new_hypothesis))',
                '        # Experimental acoustic prefix retention, kept for reproduction.\n'
                '        self.state.token_times.append(alignment["token_ends"] if alignment else\n'
                '            [self.state.cumulative_time_offset + self.segments_len()] * len(new_hypothesis))')
        replace(base, '        self._handle_pending_tokens(split_words, split_tokens, token_timestamps)',
            '        if alignment:\n'
            '            if len(timestamped_words) != len(alignment["word_spans"]):\n'
            '                raise RuntimeError("Aligned word count differs from the committed output")\n'
            '            for token, (start, end) in zip(timestamped_words, alignment["word_spans"]):\n'
            '                token.alignment_evidence = {"raw_start": token.start, "raw_end": token.end,\n'
            '                    "method": alignment["method"], "window_start": alignment["window_start"],\n'
            '                    "window_end": alignment["window_end"]}\n'
            '                token.start, token.end = start, end\n'
            '        self._handle_pending_tokens(split_words, split_tokens, token_timestamps)')

        if terminal_padding_room:
            replace(simul, '        return super().infer(is_last)',
                '        if is_last and any(t.shape[1] for t in self.state.tokens[1:]):\n'
                '            # A full 30 s encoder window has no padded right margin.\n'
                '            # Move only wholly confirmed old input chunks out of that\n'
                '            # window. Real input end and the saved WAV remain unchanged.\n'
                '            target = min(self.cfg.audio_max_len, 29.5)\n'
                '            length, removed = self.segments_len(), 0.0\n'
                '            for segment in self.state.segments[:-1]:\n'
                '                if length <= target:\n'
                '                    break\n'
                '                seconds = segment.shape[0] / 16000\n'
                '                removed += seconds\n'
                '                length -= seconds\n'
                '            safe_before = (self.state.last_attend_frame - self.cfg.rewind_threshold) / 50\n'
                '            if removed and removed <= safe_before:\n'
                '                original_limit = self.cfg.audio_max_len\n'
                '                try:\n'
                '                    self.cfg.audio_max_len = target\n'
                '                    self.insert_audio()\n'
                '                finally:\n'
                '                    self.cfg.audio_max_len = original_limit\n'
                '        return super().infer(is_last=is_last)')

    if bounded:
        # Queue capacity limits memory, not the size of an inference step.
        # Draining 30 s at once can advance the window before its unconfirmed
        # suffix is decoded. Preserve 0.5 s input steps, while allowing a small
        # bounded merge so encoder work does not accumulate on live input.
        audio = 'whisperlivekit/audio_processor.py'
        replace(audio, '            await self.transcription_queue.put(pcm_chunk.copy())',
            '            step = max(1, round(self.sample_rate * 0.5))\n'
            '            for start in range(0, pcm_chunk.size, step):\n'
            '                await self.transcription_queue.put(pcm_chunk[start:start + step].copy())')
        replace(audio, 'get_all_from_queue(self.transcription_queue),',
            f'get_all_from_queue(self.transcription_queue, max_samples=round(self.sample_rate * {max_inference_seconds!r})),')
        queue = 'whisperlivekit/processing_queue.py'
        replace(queue, '    queue: asyncio.Queue,\n',
            '    queue: asyncio.Queue, *, max_samples: int = 0,\n')
        replace(queue, '    items.append(first_item)\n',
            '    items.append(first_item)\n'
            '    samples = first_item.size if isinstance(first_item, np.ndarray) else 0\n')
        replace(queue, '        items.append(await queue.get())\n',
            '        if max_samples and isinstance(next_item, np.ndarray):\n'
            '            if samples + next_item.size > max_samples:\n'
            '                break\n'
            '            samples += next_item.size\n'
            '        items.append(await queue.get())\n')

    manifest_path = destination.parent / 'patched-source.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['quality_patch'] = '0.2.6-stream-repair-' + ('bounded-' if bounded else '') + variant
    if bounded:
        manifest['max_inference_audio_seconds'] = max_inference_seconds
    manifest['prefix_retention'] = prefix_retention
    manifest['terminal_padding_room'] = terminal_padding_room
    manifest['patched_files'] = {str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((destination/'whisperlivekit').rglob('*.py'))}
    manifest_path.write_text(json.dumps(manifest, indent=2)+'\n')
    patch = ''.join(''.join(difflib.unified_diff(old.splitlines(keepends=True),
        (destination/name).read_text().splitlines(keepends=True), fromfile='a/'+name, tofile='b/'+name))
        for name, old in changes.items())
    (destination.parent/'stream-repair.patch').write_text(patch)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--variant', choices=['diagnostic', 'keep-progress', 'aligned-progress',
        'terminal-progress', 'aligned-terminal', 'acoustic-terminal', 'bounded-acoustic-terminal'], default='diagnostic')
    parser.add_argument('--max-inference-seconds', type=float, default=1.0)
    parser.add_argument('--prefix-retention', choices=['input-horizon', 'alignment'], default='input-horizon')
    parser.add_argument('--terminal-padding-room', action='store_true')
    args = parser.parse_args()
    prepare(args.source.resolve(), args.destination.resolve(), args.variant, args.max_inference_seconds,
            args.prefix_retention, args.terminal_padding_room)
