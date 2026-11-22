"""Complete-input policy for new text operations, shared by Qt and the worker.

QtCore supplies grapheme facts on every platform. This module neither schedules
generation nor changes a saved recipe/plan. Byte and cursor coordinates are not
grapheme counts. See docs/2026-10-22-T03-A共享规则与接续.md.
"""
import re
import unicodedata
from dataclasses import asdict, dataclass
from functools import lru_cache

from . import CoreError
from .contracts import MAX_INPUT_BYTES, SOURCE_BYTES, foundation_json, sha

RULE_VERSION = "complete-text-routing-v1"
COUNTING_VERSION = "qt6102-grapheme-v1"
# Frozen Foundation-compatible trim set, including U+200B, for existing text
# task compatibility. This is deliberately NOT str.strip/isspace or regex \s.
WHITESPACE = "\t\n\v\f\r \u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u200b\u2028\u2029\u202f\u205f\u3000"
_WORDS = re.compile("[" + re.escape(WHITESPACE) + "]+")
NONE = "none"
TRANSLATION = "translation"
EXPLANATION = "translation_with_explanation"
MAX_ENGLISH_WORDS = 20
MAX_ENGLISH_GRAPHEMES = 160
MAX_CHINESE_GRAPHEMES = 40


@dataclass(frozen=True)
class RuleIdentity:
    rule: str = RULE_VERSION
    counting: str = COUNTING_VERSION
    qt: str = "6.10.2"
    pyside: str = "6.10.2"
    word_categories: str = "unicode-LN-15.0.0"
    whitespace: str = "foundation-compatible-explicit-v1"
    newlines: str = "CRLF-to-LF-statistics-only-v1"


@lru_cache(maxsize=1)
def _backend():
    try:
        from PySide6 import __version__
        from PySide6.QtCore import QTextBoundaryFinder, qVersion
    except ImportError as exc:
        raise CoreError("文字统计需要项目固定的 Qt 环境。", "text_counting_unavailable") from exc
    if qVersion() != "6.10.2" or __version__ != "6.10.2" or unicodedata.unidata_version != "15.0.0":
        raise CoreError("文字统计环境已变化，请复核共同规则后再使用。", "text_counting_incompatible")
    return QTextBoundaryFinder


def rule_identity():
    _backend()  # Refuse an unknown implementation; never fall back to len().
    return RuleIdentity()


@dataclass(frozen=True)
class TextAnalysis:
    direction: str
    is_empty: bool
    graphemes: int
    english_words: int | None
    target_kind: str
    identity: RuleIdentity


@dataclass(frozen=True)
class TextFacts:
    direction: str
    is_empty: bool
    graphemes: int
    english_words: int | None


def measure_text(source, direction):
    """Analyze the FULL input before any packing; never mutate its raw bytes.

    Only the statistical view trims WHITESPACE and maps CRLF to LF. Internal
    spaces, punctuation and logical newlines each participate in Qt graphemes.
    English tokens are whitespace-separated, with >=1 Unicode letter/number;
    apostrophes/hyphens and mixed scripts do not introduce additional splits.
    Capacity checks remain separate, even for a single huge combining cluster.
    """
    from .languages import valid_direction
    if not isinstance(source, str) or not valid_direction(direction):
        raise CoreError("文字统计需要完整原文及有效翻译方向。", "text_rule_input_invalid")
    rule_identity()
    view = source.strip(WHITESPACE).replace("\r\n", "\n")
    finder_type = _backend()
    finder = finder_type(finder_type.Grapheme, view)
    graphemes = 0
    while finder.toNextBoundary() >= 0:
        # The returned end is UTF-16. Count boundaries, NOT that offset.
        graphemes += 1
    words = None
    if direction.startswith('en-'):
        words = sum(any(unicodedata.category(c)[0] in "LN" for c in token)
                    for token in _WORDS.split(view))
    empty = not view
    return TextFacts(direction, empty, graphemes, words)


def analyze_text(source, direction):
    """Frozen T03-A/experiment routing. New operations use text_policy."""
    facts = measure_text(source, direction)
    graphemes, words, empty = facts.graphemes, facts.english_words, facts.is_empty
    target = NONE if empty else EXPLANATION if direction in ('en-zh', 'zh-en') and (
        graphemes <= MAX_CHINESE_GRAPHEMES if direction == "zh-en" else
        words <= MAX_ENGLISH_WORDS and graphemes <= MAX_ENGLISH_GRAPHEMES) else TRANSLATION
    return TextAnalysis(direction, empty, graphemes, words, target, rule_identity())


@dataclass(frozen=True)
class NewTextAssessment:
    analysis: TextAnalysis
    execution_kind: str
    explanation_available: bool = False


def assess_new_text(source, direction):
    """T03-A capability gate, before execution: only T02 translation is enabled.

    A target is not an executable recipe. No failed explanation request or
    fallback occurs: detailed generation has never been dispatched here.
    """
    analysis = analyze_text(source, direction)
    return NewTextAssessment(analysis, NONE if analysis.is_empty else TRANSLATION)


@dataclass(frozen=True)
class ShortExplanationPreparation:
    """Ephemeral T03-C boundary, NOT a persisted task, recipe or result schema."""
    source_utf8: bytes
    direction: str
    analysis: TextAnalysis
    fixed_configuration: bytes
    utf8_start: int
    utf8_end: int
    binding: str
    version: str = "short-explanation-preparation-v1"


def prepare_short_explanation(source, direction, *, fixed_configuration):
    """Prepare one complete byte-preserving unit, without a platform splitter.

    The caller must supply B-validated configuration bytes in T03-C. Their
    contents remain opaque here; this preparation does not approve a recipe or
    enable generation. The current per-unit UTF-8 budget remains a hard guard.
    """
    if not isinstance(source, str):
        raise CoreError("短任务需要完整原文。", "text_rule_input_invalid")
    raw = source.encode("utf-8")
    if len(raw) > MAX_INPUT_BYTES:
        raise CoreError("原文超出 1MiB UTF-8 容量保护。", "resource_limit")
    analysis = analyze_text(source, direction)
    if analysis.target_kind != EXPLANATION:
        raise CoreError("完整输入不适用短文本详解准备。", "short_explanation_ineligible")
    if len(raw) > SOURCE_BYTES:
        raise CoreError("完整短输入超出单单元 UTF-8 容量；保留原文，不拆分详解。", "short_unit_capacity")
    if not isinstance(fixed_configuration, bytes) or not fixed_configuration:
        raise CoreError("接续方须提供已验证的固定配置字节。", "fixed_configuration_required")
    binding = sha(foundation_json(dict(version="short-explanation-preparation-v1",
        source_sha256=sha(raw), direction=direction, rules=asdict(analysis.identity),
        configuration_sha256=sha(fixed_configuration), utf8_start=0, utf8_end=len(raw),
        target_kind=EXPLANATION)))
    return ShortExplanationPreparation(raw, direction, analysis, fixed_configuration, 0, len(raw), binding)
