"""Execute the patched AlignAtt inference with deterministic logits, no weights."""
import ast
from dataclasses import dataclass, replace
import logging
import os
import re
import sys
from typing import List, Tuple, Optional
from pathlib import Path
from types import SimpleNamespace
from abc import ABC, abstractmethod

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get('WLK_QUALITY_SOURCE', ROOT/'artifacts/whisperlivekit-speech-quality-20260930-r8/source'))


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
