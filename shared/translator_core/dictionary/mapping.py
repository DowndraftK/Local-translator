"""Traceable source-group mapping; never fill absent lexical fields."""
import csv
import io
import re

from ..text_rules import WHITESPACE

CSV_HEADER = ["word", "phonetic", "definition", "translation", "pos", "collins", "oxford",
              "tag", "bnc", "frq", "exchange", "detail", "audio"]
CC = re.compile(r"^(\S+) (\S+) (\[\[.*?\]\]|\[.*?\]) /(.*)/$")
WS = re.compile("[" + re.escape(WHITESPACE) + "]+")
PUNCT = str.maketrans({"\u2018": "'", "\u2019": "'", "\u2010": "-", "\u2011": "-"})
ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
REFERENCE = re.compile(r"^(CL:|see |also written |variant of |old variant of |erhua variant of |erhua form of |Taiwan pr\.|abbr\. for |surname )", re.I)
POS = re.compile(r"^((?:(?:[A-Za-z]+(?:/[A-Za-z]+)?\.)\s*)+)")


def normalize(text, direction):
    value = text.strip(WHITESPACE)
    if direction == "en-zh":
        value = WS.sub(" ", value).translate(PUNCT).translate(ASCII_LOWER)
    return value


def split_outside(text, separators):
    depth = start = 0
    for i, ch in enumerate(text):
        if ch in "(（":
            depth += 1
        elif ch in ")）":
            depth = max(0, depth - 1)
        elif ch in separators and not depth:
            if text[start:i].strip():
                yield text[start:i].strip()
            start = i + 1
    if text[start:].strip():
        yield text[start:].strip()


def leading_labels(value):
    labels = []
    while value.startswith("(") and ")" in value:
        end = value.index(")")
        labels.append(value[1:end]); value = value[end + 1:].strip()
    return value, labels


def units(row, direction):
    """Yield one record lazily; retain context for every synonym in its group.

    Source-group ordinals are not assertions of semantic polysemy. No cross-
    group/record deduplication is allowed. Same-text synonyms in a group are
    deduplicated only when their explicit local labels also agree.
    """
    locator = dict(record_id=row["id"], line_start=row["line_start"], line_end=row["line_end"])
    if direction == "zh-en":
        match = CC.fullmatch(row["raw"].rstrip("\r\n"))
        if not match:
            raise ValueError("invalid_cedict_record")
        traditional, headword, phonetic, definition = match.groups()
        for group_no, group in enumerate(definition.split("/"), 1):
            # A group prefix applies to all synonyms, including ones that do
            # not repeat it. Explicit labels on later alternatives are retained.
            group_body, group_labels = leading_labels(group)
            group_reference = bool(REFERENCE.match(group_body))
            group_format_information = group_body.startswith(("lit.", "fig."))
            seen = set()
            for ordinal, gloss in enumerate(split_outside(group, ";"), 1):
                text, local_labels = leading_labels(gloss)
                text = text or gloss
                labels = list(dict.fromkeys(group_labels + local_labels))
                key = (text, tuple(labels))
                if key in seen:
                    continue
                seen.add(key)
                reference = group_reference or bool(REFERENCE.match(text))
                # Conservative deterministic format boundary. lit./fig. remain
                # complete source information, with no unreviewed copy phrase.
                info = reference or group_format_information or len(text.split()) > 12 or text.startswith((
                    "used in ", "used to ", "classifier for ", "a type of ", "lit.", "fig."))
                value = dict(unit_id=f'{row["id"]}:{group_no}:{ordinal}', headword=headword,
                    traditional=traditional, phonetic=phonetic[2:-2] if phonetic.startswith("[[") else phonetic[1:-1],
                    source_group=group, raw_gloss=gloss, text=text,
                    source_group_ordinal=group_no, source_gloss_ordinal=ordinal,
                    semantic_sense_unverified=True, group_labels=group_labels, local_labels=local_labels,
                    labels=labels, kind="reference" if reference else "definition" if info else "expression",
                    copy_text=None if info else text, source_locator=locator)
                yield value
    else:
        fields = csv_fields(row["raw"])
        for group_no, line in enumerate(fields["translation"].replace("\\n", "\n").splitlines(), 1):
            match = POS.match(line)
            pos = match.group(1).strip() if match else None
            body = line[match.end():] if match else line
            seen = set()
            for ordinal, text in enumerate(split_outside(body, ",，;；"), 1):
                if text in seen:
                    continue
                seen.add(text)
                value = dict(unit_id=f'{row["id"]}:{group_no}:{ordinal}', headword=fields["word"],
                    source_group=line, raw_gloss=line, text=text, copy_text=text, kind="chinese_gloss",
                    source_group_ordinal=group_no, source_gloss_ordinal=ordinal,
                    semantic_sense_unverified=True, source_locator=locator)
                if pos:
                    value["pos"] = pos
                if fields["phonetic"]:
                    value["phonetic"] = fields["phonetic"]
                # Word-level fields stay word-level. They are not injected as
                # properties of individual meanings.
                yield value
        if not fields["translation"].strip():
            yield dict(unit_id=f'{row["id"]}:0:0', headword=fields["word"], kind="entry_information",
                       text=None, copy_text=None, status="translation_missing", source_locator=locator,
                       source_group_ordinal=0, source_gloss_ordinal=0)


def csv_fields(raw):
    values = next(csv.reader(io.StringIO(raw, newline=""), strict=True))
    if len(values) != len(CSV_HEADER):
        raise ValueError("invalid_ecdict_record")
    return dict(zip(CSV_HEADER, values))
