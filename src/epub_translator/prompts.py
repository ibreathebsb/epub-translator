"""Prompt text. Tuned for English to Simplified Chinese; other pairs get the general rules."""

from __future__ import annotations

import json

LANGUAGES = {
    "zh": "Simplified Chinese",
    "zh-cn": "Simplified Chinese",
    "zh-hans": "Simplified Chinese",
    "zh-sg": "Simplified Chinese",
    "zh-tw": "Traditional Chinese (Taiwan)",
    "zh-hk": "Traditional Chinese (Hong Kong)",
    "zh-hant": "Traditional Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "pt": "Portuguese",
    "it": "Italian",
    "ru": "Russian",
    "ar": "Arabic",
    "nl": "Dutch",
    "tr": "Turkish",
    "vi": "Vietnamese",
}

_SYSTEM = """\
You are an expert translator of books and journalism. You translate into {language}.

You receive one complete document from a book (a chapter or an article), cut into \
numbered segments in reading order: headings, paragraphs, list items, captions and \
similar pieces. Read the whole document first, then translate the segments you are \
asked for into {language}.

# Output
Return one JSON object and nothing else:
{{"translations": [{{"id": 1, "text": "..."}}, {{"id": 2, "text": "..."}}]}}
- One entry for every requested segment id, in the order given. Never merge, split, \
skip or add segments.
- "text" holds the translation of that segment only: no notes, no explanations, no \
source text alongside.

# Placeholders
Segments may contain placeholders that stand for markup:
- <x1>...</x1> wraps words that carry formatting or a link.
- <x2/> stands for an inline object such as a line break, an image or a note reference.
Rules:
- Every placeholder of a segment appears in its translation exactly once, with the \
same number. Never add, drop, renumber or repeat one.
- Keep the nesting: a placeholder that sits inside another one stays inside it.
- Move placeholders to where they belong in the translated sentence. A pair wraps the \
translation of the words it wrapped in the source and is never left empty.

# Translation
- Be faithful and complete: do not summarise, omit or embellish.
- Write natural, fluent {language}, as a skilled native writer would, not word for \
word. Keep the tone and register of the source.
- Keep names and terms consistent across the whole document.
- Leave unchanged: URLs, e-mail addresses, code, file names, and text that is already \
in {language}. A segment that needs no translation is returned as it is.
- Keep numbers, dates and amounts accurate. Do not convert units or currencies.
- Segments marked "kind": "heading" are titles: translate them as titles.
{extra}"""

_CHINESE_SIMPLIFIED = """
# 中文译文要求
- 使用规范、流畅的简体中文书面语，符合中国大陆的用语习惯，避免翻译腔。
- 使用中文全角标点（，。；：？！“”‘’（）——……）。书名、报刊名、影视作品名用《》。
- 人名、地名、机构名用中国大陆通行的译名，例如 Donald Trump 译作“唐纳德·特朗普”。\
没有通行译名的，音译或意译，并在本文首次出现时用括号附上原文，例如\
“阿利科·丹格特（Aliko Dangote）”，此后不再附注。
- GDP、AI、CEO 这类通用缩写保留原文。公司名、产品名没有通行中文名的保留原文。
- 数字用阿拉伯数字，数量级按中文的万、亿表达，例如 $16bn 译作“160亿美元”。
- 中文与英文字母、数字之间不加空格。
- 标题要像中文标题：简洁，末尾不加句号。
"""

_CHINESE_TRADITIONAL = """
# 中文譯文要求
- 使用規範、流暢的繁體中文書面語，符合當地的用語習慣，避免翻譯腔。
- 使用中文全形標點（，。；：？！「」『』（）——……）。書名、報刊名、影視作品名用《》。
- 人名、地名、機構名用當地通行的譯名。沒有通行譯名的，音譯或意譯，並在本文首次出現時\
用括號附上原文，此後不再附註。
- GDP、AI、CEO 這類通用縮寫保留原文。公司名、產品名沒有通行中文名的保留原文。
- 數字用阿拉伯數字，數量級按中文的萬、億表達。
- 標題要像中文標題：簡潔，末尾不加句號。
"""


def language_name(code: str) -> str:
    key = code.strip().lower().replace("_", "-")
    return LANGUAGES.get(key) or LANGUAGES.get(key.split("-")[0]) or code


def system_prompt(target: str) -> str:
    language = language_name(target)
    extra = ""
    if language.startswith("Simplified Chinese"):
        extra = _CHINESE_SIMPLIFIED
    elif language.startswith("Traditional Chinese"):
        extra = _CHINESE_TRADITIONAL
    return _SYSTEM.format(language=language, extra=extra)


def _json_lines(items: list[dict]) -> str:
    return "[\n" + ",\n".join(json.dumps(i, ensure_ascii=False) for i in items) + "\n]"


def _header(target: str, source: str | None, book: str | None) -> str:
    lines = []
    if book:
        lines.append(f"Book: {book}")
    if source:
        lines.append(f"Source language: {language_name(source)}")
    lines.append(f"Target language: {language_name(target)}")
    return "\n".join(lines)


def _payload(segments) -> list[dict]:
    items = []
    for seg in segments:
        item = {"id": seg.id}
        if seg.kind in ("heading", "title"):
            item["kind"] = "heading"
        item["text"] = seg.source
        items.append(item)
    return items


def first_message(segments, *, target: str, source: str | None, book: str | None) -> str:
    return (
        f"{_header(target, source, book)}\n\n"
        f"Translate all {len(segments)} segments of this document. Answer in JSON.\n\n"
        f"{_json_lines(_payload(segments))}"
    )


def retry_message(
    segments,
    pending,
    done: dict[int, str],
    problems: dict[int, list[str]],
    answers: dict[int, str],
    *,
    target: str,
    source: str | None,
    book: str | None,
) -> str:
    """Ask again for the segments that are still missing or were wrong.

    The whole document goes along once more so the model keeps its context.
    """
    parts = [
        _header(target, source, book),
        "",
        "This document is partly translated. Here is the whole document for context:",
        "",
        _json_lines(_payload(segments)),
    ]
    if done:
        finished = [{"id": i, "text": text} for i, text in sorted(done.items())]
        parts += [
            "",
            "These segments are finished. Keep names and terms consistent with them:",
            "",
            _json_lines(finished),
        ]
    ids = ", ".join(str(seg.id) for seg in pending)
    parts += ["", f"Translate ONLY these segments: {ids}"]
    notes = []
    for seg in pending:
        errors = problems.get(seg.id)
        if not errors:
            continue
        note = f"- id {seg.id}: {'; '.join(errors)}."
        if seg.id in answers:
            note += f" Your previous answer was: {json.dumps(answers[seg.id], ensure_ascii=False)}"
        notes.append(note)
    if notes:
        parts += ["", "Your previous answer had these problems. Fix them:", *notes]
    parts += ["", "Answer in JSON with exactly these ids."]
    return "\n".join(parts)
