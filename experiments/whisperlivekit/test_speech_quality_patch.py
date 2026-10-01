"""Execute the patched AlignAtt inference with deterministic logits, no weights."""
import ast
import asyncio
from dataclasses import dataclass, replace
import logging
import json
import os
import re
import sys
from typing import List, Tuple, Optional
from pathlib import Path
from types import SimpleNamespace
from abc import ABC, abstractmethod
from time import perf_counter
from types import ModuleType
from typing import Any, Union

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get('WLK_QUALITY_SOURCE', ROOT/'artifacts/whisperlivekit-speech-repair-20261001-final/source'))


@dataclass
class Token:
    start: float
    end: float
    text: str
    speaker: int = -1
    detected_language: str = 'en'

    def with_offset(self, offset):
        return replace(self, start=self.start+offset, end=self.end+offset)


def load_class(file, name):
    # Compile the actual pinned implementation; exclude imports which initialize
    # MLX on import. Only tensor/model hooks are fake in these control-flow tests.
    tree = ast.parse((SOURCE/'whisperlivekit/simul_whisper'/file).read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name]
    namespace = dict(ABC=ABC, abstractmethod=abstractmethod, logger=logging.getLogger(__name__),
        ASRToken=Token, ChangeSpeaker=object, np=np, AlignAttConfig=object, DEC_PAD=50257, sys=sys, List=List, Tuple=Tuple, Optional=Optional,
        _WORD_RE=re.compile(r"[^\W_]+(?:'[^\W_]+)*"), gc=SimpleNamespace(collect=lambda: None), torch=None)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE/file), 'exec'), namespace)
    return namespace[name]


def decoder(words):
    base = load_class('align_att_base.py', 'AlignAttBase')

    class Decoder(base):
        def __init__(self):
            self.words = words
            self.state = SimpleNamespace(token_times=[], segments=[np.zeros(160000)], tokens=[np.array([[1000]])],
                cumulative_time_offset=0., last_attend_frame=-200, first_timestamp=None,
                pending_incomplete_tokens=[], pending_incomplete_token_timestamps=[], pending_retries=0,
                speaker=-1, detected_language='en', global_time_offset=0)
            self.cfg = SimpleNamespace(language='en', audio_min_len=0, rewind_threshold=200, frame_threshold=4)
            self.max_text_len = 448
            self.tokenizer = SimpleNamespace(split_to_word_tokens=lambda ids: ([words[i] for i in ids], [[i] for i in ids]),
                decode=lambda ids: ''.join(words[i] for i in ids))
        def _concat_segments(self): return self.state.segments[0]
        def _encode(self, x): return np.zeros((1,500,1)), 500
        def _evaluate(self, x): pass
        def debug_print_tokens(self, x): pass
        def trim_context(self): pass
        def _current_tokens(self): return np.concatenate(self.state.tokens, axis=1)
        def fire_at_boundary(self, x): return False
        def _init_sum_logprobs(self): return None
        def _get_logits_and_cross_attn(self, *a): return np.zeros((1,1,1)), None
        def _check_no_speech(self, x): return False
        def _suppress_blank_tokens(self, x): return x
        def _apply_token_suppression(self, x): return x
        def _apply_dry_penalty(self, x, y): return x
        def _update_tokens(self, tokens, *a):
            idx = tokens.shape[1]-1
            return np.concatenate((tokens, [[idx]]), axis=1), idx >= len(words)
        def _process_cross_attention(self, *a): return None
        def _get_attended_frames(self, x): return [10], 10
        def _tokens_to_list(self, tokens, start): return tokens[0,start:].tolist()
        def _make_new_tokens_tensor(self, ids): return np.array([ids], dtype=int)
        def _clean_cache(self): pass
    Decoder.__abstractmethods__ = frozenset()
    return Decoder()


@pytest.mark.parametrize('words', [[' very',' very'], [' No',' no',' 14'], [' 3.','14'], [' Dr.',' Smith'], [' final']])
def test_only_committed_prefix_is_emitted_and_eof_delivers_tail_once(words):
    model = decoder(words)
    emitted = model.infer()
    assert ''.join(t.text for t in emitted) == ''.join(words[:-1])
    assert [x for batch in model.state.tokens[1:] for x in batch[0]] == list(range(len(words)-1))
    tail = model.infer(is_last=True)
    assert ''.join(t.text for t in tail) == words[-1]
    assert model.infer(is_last=True) == []
    assert ''.join(t.text for t in emitted+tail) == ''.join(words)


def test_committed_real_repetitions_and_rewound_times_never_delete_content():
    base = load_class('backend.py', 'SimulStreamingOnlineProcessor')
    processor = base.__new__(base)
    batches = [[Token(3, 3.1, ' very')] * 14, [Token(1, 1.1, ' no'), Token(3, 3.1, ' 14')]]
    class Model:
        cfg = SimpleNamespace(language='en')
        def infer(self, **kwargs): return batches.pop(0)
        def refresh_segment(self, **kwargs): raise AssertionError('unconfirmed audio destroyed')
    processor.model = Model()
    processor.asr = SimpleNamespace(use_full_mlx=True)
    processor.end = 4
    processor._last_committed_end = 0
    processor._recent_words = []
    processor.buffer = []
    first, _ = processor.process_iter()
    second, _ = processor.process_iter()
    assert ''.join(t.text for t in first) == ' very'*14
    assert ''.join(t.text for t in second) == ' no 14'


def test_silence_reset_uses_sample_clock_not_last_estimated_word():
    tree = ast.parse((SOURCE/'whisperlivekit/audio_processor.py').read_text())
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == 'end_silence')
    recorded = []
    obj = SimpleNamespace(transcription=SimpleNamespace(end_silence=lambda *args: recorded.append(args)),
        state=SimpleNamespace(tokens=[Token(4, 5, 'speech')]))
    for actual_start in (7, 40, 80):
        item = SimpleNamespace(start=actual_start, duration=6)
        eval(compile(ast.Expression(call), 'actual-silence-call', 'eval'), {'self': obj, 'item': item})
    assert recorded == [(6, 7), (6, 40), (6, 80)]


def test_continuation_and_repeated_eof_do_not_force_extra_speech():
    model = decoder([' Keep', ' this'])
    calls = []
    model._suppress_blank_tokens = lambda logits: (calls.append('blank'), logits)[1]
    model.infer()
    assert calls == ['blank']
    model.infer(is_last=True)
    model.infer(is_last=True)
    assert calls == ['blank'], 'A continuation must allow the model to stop at EOT'


def test_window_trims_confirmed_batches_by_input_horizon():
    tree = ast.parse((SOURCE/'whisperlivekit/simul_whisper/simul_whisper.py').read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'AlignAtt')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'insert_audio')
    space = dict(TOKENS_PER_SECOND=50, logger=logging.getLogger(__name__))
    exec(compile(ast.Module(body=[method], type_ignores=[]), 'actual-insert-audio', 'exec'), space)
    context = []
    initial = np.array([[1000]])
    state = SimpleNamespace(segments=[np.zeros(8000),np.zeros(8000)], last_attend_frame=100,
        cumulative_time_offset=0., initial_tokens=initial,
        tokens=[initial,np.empty((1,0),dtype=int),np.array([[10,11,12]]),np.array([[13,14]])],
        token_times=[[],[.5,.5,.5],[1.,1.]], context=SimpleNamespace(append_token_ids=context.extend))
    obj = SimpleNamespace(state=state, cfg=SimpleNamespace(audio_max_len=1))
    obj.segments_len = lambda: sum(len(s) for s in state.segments)/16000
    space['insert_audio'](obj, np.zeros(8000))
    assert context == [10,11,12]
    assert state.tokens[1].tolist() == [[13,14]] and state.token_times == [[1.,1.]]
    space['insert_audio'](obj, np.zeros(4000))
    assert context == [10,11,12,13,14] and len(state.tokens) == 1 and not state.token_times



def test_prefix_retention_uses_actual_input_horizon_not_attention_timestamp():
    model = decoder([' Keep', ' the', ' prefix'])
    model._get_attended_frames = lambda _: ([10], 10)
    model.infer()
    # Acoustic estimates are 0.2 s; all tokens in the confirmed batch belong
    # to the real 10 s input horizon and must be evicted together.
    assert model.state.token_times == [[10., 10.]]


def test_late_attention_rewind_keeps_earlier_accepted_words_and_delivers_tail_once():
    model = decoder([' Keep', ' these', ' words', ' once'])
    frames = iter([100, 120, 450, 100])
    model._get_attended_frames = lambda _: ([value := next(frames)], value)
    model.fire_at_boundary = lambda _: True
    first = model.infer()
    assert ''.join(t.text for t in first) == ' Keep these words'
    model._get_attended_frames = lambda _: ([450], 450)
    final = model.infer(is_last=True)
    assert ''.join(t.text for t in final) == ' once'
    assert model.infer(is_last=True) == []
    assert ''.join(t.text for t in first + final) == ' Keep these words once'


def test_eof_accepts_last_acoustic_edge_word_instead_of_waiting_for_future_audio():
    model = decoder([' final', ' word'])
    model._get_attended_frames = lambda _: ([499], 499)
    model.fire_at_boundary = lambda _: True
    assert ''.join(t.text for t in model.infer(is_last=True)) == ' final word'
    assert model.infer(is_last=True) == []


def test_decode_reserves_alignment_eot_when_text_context_fills():
    model = decoder([' first', ' second', ' third', ' fourth'])
    model.max_text_len = 4
    model.fire_at_boundary = lambda _: True
    def align(prefix, encoded, frames, accepted, **kwargs):
        assert prefix.shape[1] + len(accepted) + 1 <= model.max_text_len
    model._align_committed_to_audio = align
    assert ''.join(t.text for t in model.infer()) == ' first second'


@pytest.mark.parametrize('confirmed_progress', [True, False])
def test_terminal_encoder_room_keeps_real_end_and_never_discards_unconfirmed_start(confirmed_progress):
    manifest = json.loads((SOURCE.parent/'patched-source.json').read_text())
    if not manifest.get('terminal_padding_room'):
        pytest.skip('Only the terminal padding candidate contains this policy')
    tree = ast.parse((SOURCE/'whisperlivekit/simul_whisper/simul_whisper.py').read_text())
    original = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'AlignAtt')
    methods = [n for n in original.body if isinstance(n, ast.FunctionDef) and n.name in ('infer','insert_audio')]
    for method in methods: method.decorator_list = []
    class Parent:
        def infer(self, is_last=False):
            return self.state.cumulative_time_offset + self.segments_len()
    cls = ast.ClassDef(name='TerminalDecoder', bases=[ast.Name(id='Parent', ctx=ast.Load())],
                       keywords=[], body=methods, decorator_list=[])
    space = dict(Parent=Parent, TOKENS_PER_SECOND=50, logger=logging.getLogger(__name__))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), 'actual-terminal-window', 'exec'), space)
    model = space['TerminalDecoder']()
    first, second = np.zeros(8000), np.ones(472000)
    initial = np.array([[1000]])
    model.state = SimpleNamespace(segments=[first,second], tokens=[initial,np.array([[10]])],
        token_times=[[1.]], initial_tokens=initial, cumulative_time_offset=0.,
        last_attend_frame=1000 if confirmed_progress else 20,
        context=SimpleNamespace(append_token_ids=lambda _: None))
    model.cfg = SimpleNamespace(audio_max_len=30., rewind_threshold=200)
    model.segments_len = lambda: sum(len(s) for s in model.state.segments)/16000
    assert model.infer(is_last=True) == 30., 'Padding room must not advance the media endpoint'
    assert model.cfg.audio_max_len == 30.
    assert model.state.token_times == [[1.]]
    if confirmed_progress:
        assert model.state.cumulative_time_offset == .5
        assert model.state.segments == [second]
    else:
        assert model.state.cumulative_time_offset == 0
        assert model.state.segments == [first,second]


@pytest.mark.parametrize('limit', [8000, 16000])
def test_fast_queue_keeps_audio_steps_bounded_and_boundaries_ordered(limit):
    tree = ast.parse((SOURCE/'whisperlivekit/processing_queue.py').read_text())
    nodes = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    class Silence: pass
    class ChangeSpeaker: pass
    space = dict(asyncio=asyncio, np=np, Any=Any, Union=Union, List=List,
                 Silence=Silence, ChangeSpeaker=ChangeSpeaker, perf_counter=perf_counter)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'actual-queue', 'exec'), space)
    async def check():
        queue = space['ProcessingQueue']('test', max_samples=32000)
        pcm = np.arange(24000, dtype=np.float32)
        boundary = Silence()
        for chunk in np.split(pcm, 3): await queue.put(chunk)
        await queue.put(boundary); await queue.put(space['SENTINEL'])
        chunks = []
        while isinstance(queue._queue[0], np.ndarray):
            chunks.append(await space['get_all_from_queue'](queue, max_samples=limit))
        assert all(c.size <= limit for c in chunks)
        assert np.array_equal(np.concatenate(chunks), pcm)
        assert await space['get_all_from_queue'](queue, max_samples=8000) is boundary
        assert await space['get_all_from_queue'](queue, max_samples=8000) is space['SENTINEL']
        await queue.join()
        assert queue.queued_samples == 0
    asyncio.run(check())


def test_large_vad_span_is_split_without_changing_samples():
    tree = ast.parse((SOURCE/'whisperlivekit/audio_processor.py').read_text())
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == '_enqueue_active_audio')
    space = dict(np=np)
    exec(compile(ast.Module(body=[method], type_ignores=[]), 'actual-enqueue', 'exec'), space)
    async def check():
        queue = asyncio.Queue()
        obj = SimpleNamespace(transcription_queue=queue, sample_rate=16000,
                              args=SimpleNamespace(diarization=False))
        pcm = np.arange(25793, dtype=np.float32)
        await space['_enqueue_active_audio'](obj, pcm)
        chunks = [queue.get_nowait() for _ in range(queue.qsize())]
        assert [len(c) for c in chunks] == [8000, 8000, 8000, 1793]
        assert np.array_equal(np.concatenate(chunks), pcm)
        pcm[:] = -1
        assert chunks[0][0] == 0, 'Queued audio must own its samples'
    asyncio.run(check())


@pytest.mark.parametrize('acoustic', [False, True])
def test_alignment_masks_history_keeps_word_atoms_and_records_only_confirmed_suffix(monkeypatch, acoustic):
    import torch
    timing = ModuleType('whisperlivekit.whisper.timing')
    def dtw(cost):
        assert cost.device.type == 'cpu'
        assert tuple(cost.shape) == (5, 150), 'History must not compete with the live text'
        return np.arange(5), np.array([0, 10, 20, 50, 90])
    timing.dtw = dtw
    timing.median_filter = lambda matrix, width: matrix
    monkeypatch.setitem(sys.modules, 'whisperlivekit.whisper.timing', timing)
    tree = ast.parse((SOURCE/'whisperlivekit/simul_whisper/simul_whisper.py').read_text())
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == '_align_committed_to_audio')
    space = dict(np=np, torch=torch)
    exec(compile(ast.Module(body=[method], type_ignores=[]), 'actual-alignment', 'exec'), space)
    def forward(tokens, encoder, return_cross_attn):
        assert tokens.tolist() == [[999, 888, 1000, 1001, 1002, 10, 11, 12, 13, 2000]]
        return None, [torch.zeros((1, 1, tokens.shape[1], 150))]
    def split(ids):
        return (['prefix', 'word'] if ids == [10, 11, 12, 13] else ['word'],
                [[10, 11], [12, 13]] if ids == [10, 11, 12, 13] else [[12, 13]])
    pcm = torch.ones(48000) * .01
    pcm[6400:14400] = 0  # Measured 0.4–0.9 s quiet gap, within the new word span.
    obj = SimpleNamespace(device='cpu',
        state=SimpleNamespace(tokens=[torch.tensor([[1000, 1001, 1002]]), torch.tensor([[10, 11]])],
            token_times=[[9, 9]], initial_token_length=3, cumulative_time_offset=2., global_time_offset=10.),
        tokenizer=SimpleNamespace(eot=2000, split_to_word_tokens=split),
        model=SimpleNamespace(decoder=forward, alignment_heads=SimpleNamespace(indices=lambda: torch.tensor([[0], [0]]))),
        _concat_segments=lambda: pcm, segments_len=lambda: 3.)
    current = torch.tensor([[999, 888, 1000, 1001, 1002, 10, 11]])
    aligned = space['_align_committed_to_audio'](obj, current, torch.zeros(1), 150, [12, 13], acoustic)
    retention = json.loads((SOURCE.parent/'patched-source.json').read_text()).get('prefix_retention', 'alignment')
    if retention == 'input-horizon':
        assert obj.state.token_times == [[9, 9]], 'Display alignment must preserve confirmed retention coordinates'
    else:
        assert obj.state.token_times == [[2.4, 2.4]], 'Experimental acoustic word atoms remain intact'
    assert aligned['token_ends'] == [3.8, 3.8]
    assert aligned['word_spans'] == [(12.9 if acoustic else 12.4, 13.8)]
    assert (aligned['window_start'], aligned['window_end']) == (12., 15.)
