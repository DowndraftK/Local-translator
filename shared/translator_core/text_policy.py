"""Current concise policy. Statistics and frozen historical routing are separate.

This identity is diagnostic, never a recipe, saved task or plan migration.
"""
from dataclasses import replace

from .text_rules import (NONE, TRANSLATION, TextAnalysis, NewTextAssessment,
                         measure_text, rule_identity as legacy_identity)

POLICY_VERSION = "concise-text-routing-v1"


def policy_identity():
    return replace(legacy_identity(), rule=POLICY_VERSION)


def analyze_concise_text(source, direction):
    facts = measure_text(source, direction)
    return TextAnalysis(facts.direction, facts.is_empty, facts.graphemes,
                        facts.english_words, NONE if facts.is_empty else TRANSLATION,
                        policy_identity())


def assess_concise_text(source, direction):
    analysis = analyze_concise_text(source, direction)
    return NewTextAssessment(analysis, analysis.target_kind)
