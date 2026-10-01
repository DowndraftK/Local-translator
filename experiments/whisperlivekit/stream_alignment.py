"""Method injected into the pinned Torch decoder; reuse its weights and encoder output."""


def _align_committed_to_audio(self, current_tokens, encoder_feature, content_mel_len, new_hypothesis,
                             acoustic_onsets=False):
    from whisperlivekit.whisper.timing import dtw, median_filter

    if not new_hypothesis:
        return None
    # Only the committed suffix belongs here; a boundary policy may still hold
    # the last decoded word. Rebuild from the frozen prefix and accepted IDs.
    accepted = torch.tensor([new_hypothesis], dtype=torch.long, device=self.device)
    current_tokens = torch.cat([current_tokens[:1], accepted], dim=1)
    prefix_count = sum(t.shape[1] for t in self.state.tokens[1:])
    text_count = prefix_count + len(new_hypothesis)
    prompt_count = current_tokens.shape[1] - text_count
    if prompt_count < self.state.initial_token_length:
        raise RuntimeError('Alignment lost the live decoder prompt coordinates')
    # The text before SOT is history, outside this audio window. It informs
    # decoding but must not compete with live words in the alignment matrix.
    eot = torch.full((1, 1), self.tokenizer.eot, dtype=torch.long, device=self.device)
    alignment_tokens = torch.cat([current_tokens[:1], eot], dim=1)
    _, cross = self.model.decoder(alignment_tokens, encoder_feature, return_cross_attn=True)
    heads = torch.stack([cross[int(layer)][0, int(head)]
        for layer, head in self.model.alignment_heads.indices().T])
    heads = heads[:, prompt_count - 1:-1, :content_mel_len].float()
    if heads.shape[1] != text_count + 1 or heads.shape[2] == 0:
        raise RuntimeError('Alignment token/audio dimensions differ from the confirmed prefix')
    heads = heads.softmax(dim=-1)
    std, mean = torch.std_mean(heads, dim=-2, keepdim=True, unbiased=False)
    heads = median_filter((heads - mean) / (std + 1e-8), 7)
    matrix = heads.mean(dim=0)
    # Whisper's CPU DTW casts to float64; MPS cannot cast there, so move the
    # small attention matrix to CPU before calling that existing implementation.
    token_indices, frames = dtw(-matrix.cpu())
    jumps = np.pad(np.diff(token_indices), (1, 0), constant_values=1).astype(bool)
    times = frames[jumps] / 50.0 + self.state.cumulative_time_offset
    if len(times) != text_count + 1 or not np.isfinite(times).all():
        raise RuntimeError('Alignment failed to cover every confirmed token boundary')
    # Word atoms remain intact when the audio window advances. Unlike attention
    # argmax or the input arrival horizon, these are monotonic acoustic boundaries.
    all_text = current_tokens[0, prompt_count:].tolist()
    _, atoms = self.tokenizer.split_to_word_tokens(all_text)
    ends = []
    position = 0
    for atom in atoms:
        position += len(atom)
        ends.extend([float(times[position])] * len(atom))
    if len(ends) != text_count:
        raise RuntimeError('Alignment word atoms do not cover the confirmed token sequence')
    position = 0
    for index, batch in enumerate(self.state.tokens[1:]):
        count = batch.shape[1]
        self.state.token_times[index] = ends[position:position + count]
        position += count
    words, atoms = self.tokenizer.split_to_word_tokens(new_hypothesis)
    spans = []
    position = prefix_count
    offset = self.state.global_time_offset
    envelope = None
    if acoustic_onsets:
        pcm = self._concat_segments().detach().cpu().numpy()
        padded = np.pad(np.abs(pcm), (0, (-len(pcm)) % 160))
        envelope = padded.reshape(-1, 160).max(axis=1) >= 80 / 32768
    for word, atom in zip(words, atoms):
        end = position + len(atom)
        start_time, end_time = float(times[position]), float(times[end])
        if envelope is not None:
            a = max(0, int((start_time - self.state.cumulative_time_offset) * 100))
            b = min(len(envelope), int(np.ceil((end_time - self.state.cumulative_time_offset) * 100)))
            quiet_start = None
            onset = None
            # DTW is monotonic but may allocate the inter-word silence to a
            # following word. Move only across a measured >=300 ms quiet gap,
            # and only when this same interval contains audible samples after it.
            # Never move a word through continuous speech or use wall time.
            for frame in range(a, b):
                if not envelope[frame]:
                    if quiet_start is None:
                        quiet_start = frame
                else:
                    if quiet_start is not None and frame - quiet_start >= 30:
                        onset = frame
                    quiet_start = None
            if onset is not None:
                start_time = min(end_time, max(start_time,
                    self.state.cumulative_time_offset + onset / 100))
        spans.append((offset + start_time, offset + end_time))
        position = end
    return {'token_ends': ends[prefix_count:], 'word_spans': spans,
        'window_start': offset + self.state.cumulative_time_offset,
        'window_end': offset + self.state.cumulative_time_offset + self.segments_len(),
        'method': 'same_model_attention_dtw_silence_onset' if acoustic_onsets else 'same_model_attention_dtw'}
