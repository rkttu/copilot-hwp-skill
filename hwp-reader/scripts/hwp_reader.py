#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 rkttu@rkttu.com (남정현)
# SPDX-License-Identifier: Apache-2.0
#
# 본 제품은 한글과컴퓨터의 한/글 문서 파일(.hwp) 공개 문서를 참고하여 개발하였습니다.
"""
hwp_reader.py -- read Hancom Office HWP (5.x) and HWPX documents.

Self-contained: needs only the Python 3.8+ standard library.  No network
access, no pip installs, no native code, no OS-specific APIs.  Built to run
inside sandboxed agent runtimes (e.g. the Microsoft 365 Copilot / Copilot
Studio code interpreter, which is Linux).

Supported input
  .hwp   HWP 5.x binary (OLE compound file), compressed or not, including
         distribution-protected ("배포용") documents.
  .hwpx  OWPML (KS X 6101) zip package.
Detected and reported with a clear error (cannot be read):
  password-protected / DRM HWP, encrypted HWPX, HWP 3.x (HWP 97) and older.

Command line
  python3 hwp_reader.py FILE [FILE ...]            # Markdown to stdout
  python3 hwp_reader.py FILE -f text               # plain text
  python3 hwp_reader.py FILE -f json -o out.json   # structured JSON
  python3 hwp_reader.py FILE --info                # metadata + statistics
  python3 hwp_reader.py --help

Library
  import hwp_reader
  doc = hwp_reader.read_document("report.hwp")
  print(doc.to_markdown())

Exit codes: 0 ok, 1 unexpected error, 2 usage error, 3 unsupported format,
4 encrypted/password-protected, 5 corrupt/unreadable file.
"""

import argparse
import datetime
import html
import io
import json
import os
import re
import struct
import sys
import zipfile
import zlib
import xml.etree.ElementTree as ET

__version__ = "0.1.0"

__all__ = [
    "read_document", "iter_input_documents", "Document", "HwpReaderError", "UnsupportedFormatError",
    "EncryptedDocumentError", "CorruptDocumentError", "main",
]


# =========================================================================
# Errors and limits
# =========================================================================

class HwpReaderError(Exception):
    """Base class for all errors raised by this module."""
    exit_code = 1


class UnsupportedFormatError(HwpReaderError):
    """The file is not a HWP 5.x / HWPX document this reader understands."""
    exit_code = 3


class EncryptedDocumentError(HwpReaderError):
    """The document is password-protected, DRM-protected or encrypted."""
    exit_code = 4


class CorruptDocumentError(HwpReaderError):
    """The file is damaged or violates the format in an unrecoverable way."""
    exit_code = 5


class _XmlSyntaxError(CorruptDocumentError):
    """Malformed XML in an HWPX part (internal; may be salvaged)."""


MAX_INPUT_SIZE = 1024 * 1024 * 1024        # refuse input files larger than 1 GiB
MAX_STREAM_SIZE = 128 * 1024 * 1024       # max decompressed size of one HWP stream / zip part
MAX_TOTAL_DECOMPRESSED = 256 * 1024 * 1024  # decompressed bytes per document (all streams/parts)
MAX_XML_PART = 64 * 1024 * 1024           # max size of one HWPX XML part (real sections: < 5 MB)
MAX_RECORDS = 1500000                     # HWP records per document (largest real: ~640k)
MAX_ELEMENTS = 1000000                    # HWPX XML elements per document (real: < 200k)
MAX_DEPTH = 128                           # nesting depth units (tables / text boxes / notes)
MAX_TABLE_CELLS = 200000                  # rows*cols cap when building a grid
MAX_NUMBER = 1000000                      # numbers beyond this are printed as plain digits
MAX_WARNINGS = 100


# =========================================================================
# Small binary helpers (all tolerant of short buffers)
# =========================================================================

def _u8(b, o, d=0):
    return b[o] if 0 <= o < len(b) else d


def _u16(b, o, d=0):
    if o < 0 or o + 2 > len(b):
        return d
    return struct.unpack_from("<H", b, o)[0]


def _i16(b, o, d=0):
    if o < 0 or o + 2 > len(b):
        return d
    return struct.unpack_from("<h", b, o)[0]


def _u32(b, o, d=0):
    if o < 0 or o + 4 > len(b):
        return d
    return struct.unpack_from("<I", b, o)[0]


def _i32(b, o, d=0):
    if o < 0 or o + 4 > len(b):
        return d
    return struct.unpack_from("<i", b, o)[0]


def _u64(b, o, d=0):
    if o < 0 or o + 8 > len(b):
        return d
    return struct.unpack_from("<Q", b, o)[0]


def _wstr(b, o):
    """Read a HWP string (UINT16 length in UTF-16 code units + UTF-16LE data).

    Returns (text, next_offset).  A missing/short buffer yields ('', len(b)).
    """
    n = _u16(b, o, None)
    if n is None:
        return "", len(b)
    end = o + 2 + 2 * n
    if end > len(b):
        return "", len(b)
    return b[o + 2:end].decode("utf-16-le", "replace"), end


def _ctrl_id_str(v):
    """Convert a HWP 4-char control id (big-endian packed in a UINT32) to text."""
    return "".join(chr((v >> s) & 0xFF) for s in (24, 16, 8, 0))


# =========================================================================
# Document model (format independent)
# =========================================================================

class Paragraph(object):
    """One paragraph.  ``inlines`` mixes str with inline/anchored objects."""
    __slots__ = ("inlines", "style", "heading_level", "list_type",
                 "list_level", "label", "page_break")

    def __init__(self):
        self.inlines = []
        self.style = None
        self.heading_level = None   # 1..6 when the paragraph is an outline heading
        self.list_type = None       # 'number' | 'bullet' | None
        self.list_level = 0         # 0-based nesting level of numbering/bullets
        self.label = None           # rendered numbering / bullet label, e.g. "1." "가." "•"
        self.page_break = False


class Table(object):
    __slots__ = ("n_rows", "n_cols", "cells", "caption")

    def __init__(self):
        self.n_rows = 0
        self.n_cols = 0
        self.cells = []
        self.caption = []           # list of Paragraph


class Cell(object):
    __slots__ = ("row", "col", "row_span", "col_span", "blocks", "row_hint")

    def __init__(self, row=None, col=None, row_span=1, col_span=1, blocks=None):
        self.row = row
        self.col = col
        self.row_span = row_span
        self.col_span = col_span
        self.blocks = blocks if blocks is not None else []
        self.row_hint = None        # serialization row, used when addresses are missing


class Picture(object):
    __slots__ = ("name", "alt", "caption", "path")

    def __init__(self, name=None, alt=None, caption=None):
        self.name = name            # binary item name, e.g. BIN0001.png / image1.png
        self.alt = alt
        self.caption = caption or []
        self.path = None            # set when images are extracted to disk


class TextBox(object):
    """Text living in a drawing object, text box, group or unknown control."""
    __slots__ = ("kind", "blocks", "caption")

    def __init__(self, kind, blocks, caption=None):
        self.kind = kind
        self.blocks = blocks
        self.caption = caption or []


class Equation(object):
    __slots__ = ("script",)

    def __init__(self, script):
        self.script = script


class Placeholder(object):
    """Embedded object whose content is not text (OLE, chart, video...)."""
    __slots__ = ("kind",)

    def __init__(self, kind):
        self.kind = kind


class NoteRef(object):
    __slots__ = ("kind", "blocks")  # kind: 'footnote' | 'endnote'

    def __init__(self, kind, blocks):
        self.kind = kind
        self.blocks = blocks


class Comment(object):
    __slots__ = ("kind", "blocks")  # kind: 'hidden' (숨은 설명) | 'memo' (메모)

    def __init__(self, kind, blocks):
        self.kind = kind
        self.blocks = blocks


class FieldStart(object):
    """Start of a field.  ``url`` for hyperlinks; ``memo`` for memo anchors:
    either a list of Paragraph (memo body) or a key resolved via Document._memos;
    ``hint`` is the guide text of a click-here (누름틀) field, shown when empty."""
    __slots__ = ("kind", "url", "memo", "hint")

    def __init__(self, kind, url=None, memo=None, hint=None):
        self.kind = kind
        self.url = url
        self.memo = memo
        self.hint = hint


class FieldEnd(object):
    __slots__ = ()


class HeaderFooter(object):
    __slots__ = ("kind", "apply", "blocks")

    def __init__(self, kind, apply, blocks):
        self.kind = kind            # 'header' | 'footer'
        self.apply = apply          # 'both' | 'even' | 'odd'
        self.blocks = blocks


class Section(object):
    __slots__ = ("blocks", "headers", "memos")

    def __init__(self):
        self.blocks = []            # list of Paragraph
        self.headers = []           # list of HeaderFooter (headers and footers)
        self.memos = []             # list of Comment not anchored in a paragraph


_INLINE_BLOCK_TYPES = (Table, TextBox)


class Document(object):
    """A parsed HWP/HWPX document."""

    def __init__(self):
        self.path = None
        self.format = None          # 'hwp' | 'hwpx'
        self.version = None
        self.metadata = {}
        self.flags = {}
        self.sections = []
        self.warnings = []
        self.preview_text = None
        self._bin_loader = None     # callable(name) -> bytes | None
        self._bin_names = []
        self._memos = {}            # memo key -> list of Paragraph (bodies stored apart from anchors)

    # -- diagnostics -------------------------------------------------------
    def warn(self, message):
        if message in self.warnings:
            return
        if len(self.warnings) < MAX_WARNINGS:
            self.warnings.append(message)
        elif len(self.warnings) == MAX_WARNINGS:
            self.warnings.append("(추가 경고 생략 / further warnings suppressed)")

    # -- binary data ---------------------------------------------------------
    def binary_names(self):
        """Names of embedded binary items (images, OLE objects ...)."""
        return list(self._bin_names)

    def binary(self, name):
        """Return the bytes of an embedded binary item, or None."""
        if self._bin_loader is None or not name:
            return None
        try:
            return self._bin_loader(name)
        except HwpReaderError as e:
            self.warn("바이너리 '%s' 읽기 실패: %s" % (name, e))
            return None

    def extract_images(self, directory):
        """Write every referenced picture into ``directory``; returns paths."""
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as e:
            raise HwpReaderError("그림 저장 폴더를 만들 수 없습니다: %s (%s)" % (directory, e))
        written = {}
        for pic in _iter_pictures(self):
            if not pic.name:
                continue
            if pic.name in written:
                pic.path = written[pic.name]
                continue
            data = self.binary(pic.name)
            if data is None:
                continue
            safe = re.sub(r"[^\w.\-]+", "_", os.path.basename(pic.name.replace("\\", "/")))
            if safe.strip(".") == "":
                safe = "image.bin"
            stem, ext = os.path.splitext(safe)
            path = os.path.join(directory, safe)
            k = 2
            while os.path.exists(path):           # never overwrite another picture
                path = os.path.join(directory, "%s-%d%s" % (stem, k, ext))
                k += 1
            try:
                with open(path, "xb") as f:
                    f.write(data)
            except OSError as e:
                self.warn("그림 저장 실패 %s: %s" % (path, e))
                continue
            pic.path = path
            written[pic.name] = path
        return sorted(set(written.values()))

    # -- rendering -----------------------------------------------------------
    def _render(self, mode, options):
        try:
            r = _Renderer(self, mode, **options)
            return r.render_dict() if mode == "json" else r.render()
        except (MemoryError, RecursionError) as e:
            raise CorruptDocumentError("문서가 너무 크거나 복잡해 출력할 수 없습니다 (%s)"
                                       % type(e).__name__)

    def to_markdown(self, **options):
        return self._render("markdown", options)

    def to_text(self, **options):
        return self._render("text", options)

    def to_dict(self, **options):
        return self._render("json", options)

    def to_json(self, indent=2, **options):
        return json.dumps(self.to_dict(**options), ensure_ascii=False, indent=indent)

    def info(self):
        """Metadata and statistics (no full text)."""
        stats = {"sections": len(self.sections), "paragraphs": 0, "non_empty_paragraphs": 0,
                 "tables": 0, "pictures": 0, "equations": 0, "footnotes": 0,
                 "endnotes": 0, "text_boxes": 0, "headers_footers": 0, "comments": 0,
                 "characters": 0}
        stats["comments"] += len(self._memos)
        for sec in self.sections:
            stats["headers_footers"] += len(sec.headers)
            stats["comments"] += len(sec.memos)
            _count_blocks(sec.blocks, stats)
        # characters: non-whitespace document text (body, cells, boxes, notes),
        # without table syntax or object markers; markdown_chars: the size of
        # the default output, to decide whether to page through it
        text = self.to_text(headers_footers=False, notes=False, comments=False)
        stats["markdown_chars"] = len(self.to_markdown())
        out = {
            "file": self.path,
            "format": self.format,
            "version": self.version,
            "flags": self.flags,
            "metadata": self.metadata,
            "statistics": stats,
            "binary_items": self.binary_names(),
            "preview": _shorten(text.strip(), 400),
            "warnings": self.warnings,
        }
        return out


def _shorten(s, n):
    s = re.sub(r"\s+", " ", s or "")
    return s if len(s) <= n else s[:n - 1] + "…"


def _count_blocks(blocks, stats, depth=0):
    if depth > MAX_DEPTH:
        return
    for p in blocks:
        stats["paragraphs"] += 1
        chars = 0
        for it in p.inlines:
            if isinstance(it, str):
                chars += len(re.sub(r"\s+", "", it))
        stats["characters"] += chars
        if chars:
            stats["non_empty_paragraphs"] += 1
        for it in p.inlines:
            if isinstance(it, Table) and _is_single_cell(it):
                stats["text_boxes"] += 1                 # one-cell "box" tables
                _count_blocks(it.caption, stats, depth + 1)
                for c in it.cells:
                    _count_blocks(c.blocks, stats, depth + 1)
            elif isinstance(it, Table):
                stats["tables"] += 1
                _count_blocks(it.caption, stats, depth + 1)
                for c in it.cells:
                    _count_blocks(c.blocks, stats, depth + 1)
            elif isinstance(it, TextBox):
                stats["text_boxes"] += 1
                _count_blocks(it.blocks, stats, depth + 1)
                _count_blocks(it.caption, stats, depth + 1)
            elif isinstance(it, Picture):
                stats["pictures"] += 1
            elif isinstance(it, Equation):
                stats["equations"] += 1
            elif isinstance(it, NoteRef):
                stats["footnotes" if it.kind == "footnote" else "endnotes"] += 1
                _count_blocks(it.blocks, stats, depth + 1)
            elif isinstance(it, Comment):
                stats["comments"] += 1
            elif isinstance(it, FieldStart) and isinstance(it.memo, list):
                stats["comments"] += 1


def _iter_pictures(doc):
    def walk(blocks, depth):
        if depth > MAX_DEPTH:
            return
        for p in blocks:
            for it in p.inlines:
                if isinstance(it, Picture):
                    yield it
                    for x in walk(it.caption, depth + 1):
                        yield x
                elif isinstance(it, Table):
                    for x in walk(it.caption, depth + 1):
                        yield x
                    for c in it.cells:
                        for x in walk(c.blocks, depth + 1):
                            yield x
                elif isinstance(it, TextBox):
                    for x in walk(it.blocks, depth + 1):
                        yield x
                elif isinstance(it, (NoteRef, Comment)):
                    for x in walk(it.blocks, depth + 1):
                        yield x
    for sec in doc.sections:
        for x in walk(sec.blocks, 0):
            yield x
        for hf in sec.headers:
            for x in walk(hf.blocks, 0):
                yield x


# =========================================================================
# Automatic numbering (shared by HWP and HWPX)
# =========================================================================

_HANGUL_JAMO = "ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ"
_CIRCLED_HANGUL_SYLLABLES = "".join(chr(c) for c in range(0x326E, 0x327C))
_CIRCLED_HANGUL_JAMO = "".join(chr(c) for c in range(0x3260, 0x326E))
_KOREAN_DIGITS = ["", "일", "이", "삼", "사", "오", "육", "칠", "팔", "구"]
_HANJA_DIGITS = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九"]
_GAN = "갑을병정무기경신임계"
_GAN_HANJA = "甲乙丙丁戊己庚辛壬癸"

# HWP 5 number-shape codes -> OWPML numFormat names (same order in both specs)
_NUMBER_FORMATS = [
    "DIGIT", "CIRCLED_DIGIT", "ROMAN_CAPITAL", "ROMAN_SMALL", "LATIN_CAPITAL",
    "LATIN_SMALL", "CIRCLED_LATIN_CAPITAL", "CIRCLED_LATIN_SMALL",
    "HANGUL_SYLLABLE", "CIRCLED_HANGUL_SYLLABLE", "HANGUL_JAMO",
    "CIRCLED_HANGUL_JAMO", "HANGUL_PHONETIC", "IDEOGRAPH", "CIRCLED_IDEOGRAPH",
    "DECAGON_CIRCLE", "DECAGON_CIRCLE_HANJA",
]


def _roman(n):
    if not 0 < n < 4000:
        return str(n)
    vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
            (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
            (5, "V"), (4, "IV"), (1, "I")]
    out = []
    for v, s in vals:
        while n >= v:
            out.append(s)
            n -= v
    return "".join(out)


def _latin(n):
    """A..Z, then cycling (as Hangul displays it)."""
    return chr(ord("A") + (n - 1) % 26) if n > 0 else str(n)


# 가나다 numbering: 14 initial consonants x vowels ㅏ ㅓ ㅗ ㅜ ㅡ ㅣ (84 values), then cycles
_GANADA_CHO = (0, 2, 3, 5, 6, 7, 9, 11, 12, 14, 15, 16, 17, 18)
_GANADA_JUNG = (0, 4, 8, 13, 18, 20)


def _ganada(n):
    k = (n - 1) % 84
    return chr(0xAC00 + (_GANADA_CHO[k % 14] * 21 + _GANADA_JUNG[k // 14]) * 28)


_SYMBOLS = "*†‡§"

# Private-use code points Hangul documents carry instead of real characters.
# U+F0xx are Symbol/Wingdings glyphs (Hancom's own answer key in a public test
# document maps F076 F09F F0A0 F0A7 F0F2 F0FE; the rest are standard Wingdings /
# Symbol positions); U+F53A is the old-Hangul "ᄒᆞᆫ" of "ᄒᆞᆫ글"; U+F0854/F0855
# are paired title brackets and U+F02B1.. numbered section markers.
_PUA_MAP = {
    0xF06C: "●", 0xF06E: "■", 0xF06F: "□", 0xF075: "◆", 0xF076: "❖",
    0xF09E: "•", 0xF09F: "•", 0xF0A0: "▪", 0xF0A7: "▪", 0xF0B7: "•",
    0xF0D8: "➢", 0xF0E8: "➔", 0xF0F2: "⇩", 0xF0FC: "✓", 0xF0FE: "☑",
    0xF53A: "ᄒᆞᆫ",
    0xF0854: "「", 0xF0855: "」",
}
for _k in range(9):
    _PUA_MAP[0xF02B1 + _k] = chr(0x2460 + _k)          # ① .. ⑨
_PUA_TABLE = dict(_PUA_MAP)


def _map_pua(s):
    """Replace known private-use glyph codes with their Unicode characters."""
    return s.translate(_PUA_TABLE) if s else s


def _bullet_char(ch, default="•"):
    """Map a stored bullet character (often a Symbol/Wingdings PUA code) to text."""
    if not ch:
        return default
    cp = ord(ch[0])
    if cp in _PUA_MAP:
        return _PUA_MAP[cp]
    if 0xE000 <= cp <= 0xF8FF or cp < 32 or 0xD800 <= cp <= 0xDFFF or cp >= 0xF0000:
        return default
    return ch[0]


def _sino(n, digits, ten):
    if not 0 < n < 100:
        return str(n)
    tens, ones = divmod(n, 10)
    s = ""
    if tens:
        s += (digits[tens] if tens > 1 else "") + ten
    return s + digits[ones]


def format_number(n, fmt):
    """Render ``n`` in a HWP/OWPML number format (name or HWP5 code)."""
    if isinstance(fmt, int):
        fmt = _NUMBER_FORMATS[fmt] if 0 <= fmt < len(_NUMBER_FORMATS) else \
            ("SYMBOL" if fmt == 0x80 else "DIGIT")
    fmt = (fmt or "DIGIT").upper()
    if n <= 0 or n > MAX_NUMBER:
        return str(n)
    if fmt == "CIRCLED_DIGIT":
        return chr(0x2460 + (n - 1) % 20)
    if fmt == "ROMAN_CAPITAL":
        return _roman(n)
    if fmt == "ROMAN_SMALL":
        return _roman(n).lower()
    if fmt == "LATIN_CAPITAL":
        return _latin(n)
    if fmt == "LATIN_SMALL":
        return _latin(n).lower()
    if fmt == "CIRCLED_LATIN_CAPITAL":
        return chr(0x24B6 + (n - 1) % 26)
    if fmt == "CIRCLED_LATIN_SMALL":
        return chr(0x24D0 + (n - 1) % 26)
    if fmt == "HANGUL_SYLLABLE":
        return _ganada(n)
    if fmt == "CIRCLED_HANGUL_SYLLABLE":
        return _CIRCLED_HANGUL_SYLLABLES[(n - 1) % 14]
    if fmt == "HANGUL_JAMO":
        return _HANGUL_JAMO[(n - 1) % 14]
    if fmt == "CIRCLED_HANGUL_JAMO":
        return _CIRCLED_HANGUL_JAMO[(n - 1) % 14]
    if fmt == "HANGUL_PHONETIC":
        return _sino(n, _KOREAN_DIGITS, "십")
    if fmt == "IDEOGRAPH":
        return _sino(n, _HANJA_DIGITS, "十")
    if fmt == "CIRCLED_IDEOGRAPH":
        return chr(0x3280 + (n - 1) % 10)
    if fmt == "DECAGON_CIRCLE":
        return _GAN[(n - 1) % 10]
    if fmt == "DECAGON_CIRCLE_HANJA":
        return _GAN_HANJA[(n - 1) % 10]
    if fmt == "SYMBOL":
        reps = (n - 1) // 4 + 1
        return _SYMBOLS[(n - 1) % 4] * reps if reps <= 8 else str(n)
    return str(n)


class _NumberingDef(object):
    """One numbering definition: per-level (format string, number format, start)."""
    __slots__ = ("levels",)

    def __init__(self):
        self.levels = {}            # level (1-based) -> (template, numfmt, start)


class _NumberingState(object):
    """Counters for automatic paragraph numbering, in document order.

    Follows Hangul's observable behaviour: each numbering id keeps its own
    counters (switching back to an id continues it), counters continue across
    sections, plain and bulleted paragraphs do not interrupt a list, and a
    level that was never reached shows its start number.
    """

    def __init__(self, definitions):
        self.defs = definitions     # id -> _NumberingDef
        self.counters = {}          # id -> {level: value}

    def next_label(self, num_id, level):
        """Advance the counter of ``level`` (1-based) and return the label."""
        d = self.defs.get(num_id)
        if d is None or not d.levels:
            return None
        counts = self.counters.setdefault(num_id, {})
        tmpl, fmt, start = d.levels.get(level, ("", "DIGIT", 1))
        counts[level] = counts[level] + 1 if level in counts else start
        for deeper in [k for k in counts if k > level]:
            del counts[deeper]
        if not tmpl:
            return None

        def value(lv):
            v = counts.get(lv)
            if v is None:
                v = d.levels.get(lv, ("", "DIGIT", 1))[2]
                counts[lv] = v
            return format_number(v, d.levels.get(lv, ("", "DIGIT", 1))[1])

        def repl(m):
            tok = m.group(1)
            if tok in ("n", "N"):
                path = ".".join(value(lv) for lv in range(1, level + 1))
                return path + ("." if tok == "N" else "")
            lv = 10 if tok == ":" else int(tok)
            if lv > level:
                return ""
            return value(lv)

        label = re.sub(r"\^(10|[1-9]|:|n|N)", repl, tmpl).strip()
        return label or None

    def reset(self, num_id=None):
        if num_id is None:
            self.counters.clear()
        else:
            self.counters.pop(num_id, None)


# =========================================================================
# OLE / Compound File Binary reader ([MS-CFB])
# =========================================================================

_OLE_MAGIC = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"
_MAXREGSECT = 0xFFFFFFFA
_NOSTREAM = 0xFFFFFFFF


class _DirEntry(object):
    __slots__ = ("name", "type", "left", "right", "child", "start", "size", "path")


class CompoundFile(object):
    """Minimal read-only Compound File Binary parser (no dependencies)."""

    def __init__(self, data):
        if len(data) < 512 or data[:8] != _OLE_MAGIC:
            raise UnsupportedFormatError("OLE 복합 문서가 아닙니다 (not an OLE compound file)")
        self.data = data
        major = _u16(data, 0x1A)
        sec_shift = _u16(data, 0x1E)
        mini_shift = _u16(data, 0x20)
        if sec_shift not in (9, 12):
            sec_shift = 12 if major == 4 else 9
        if not 2 <= mini_shift < sec_shift:
            mini_shift = 6
        self.major = major
        self.ss = 1 << sec_shift
        self.mss = 1 << mini_shift
        (n_fat, first_dir, _tx, cutoff, first_minifat, n_minifat,
         first_difat, n_difat) = struct.unpack_from("<8I", data, 0x2C)
        self.cutoff = cutoff or 4096
        self.n_sectors = max(0, (len(data) - self.ss + self.ss - 1) // self.ss)

        # DIFAT -> FAT.  A FAT never needs more sectors than it takes to map
        # every sector of the file, so both walks are bounded by the file size
        # (a crafted DIFAT could otherwise list the same sector millions of times).
        per = self.ss // 4
        fat_needed = (self.n_sectors + per - 1) // per + 1
        difat = list(struct.unpack_from("<109I", data, 0x4C))
        sid = first_difat
        seen = set()
        max_difat = (max(0, fat_needed - 109) + per - 2) // max(1, per - 1) + 1
        while sid <= _MAXREGSECT and len(seen) < max_difat:
            if sid in seen or sid >= self.n_sectors:
                break
            seen.add(sid)
            vals = struct.unpack("<%dI" % per, self._sector(sid))
            difat.extend(vals[:-1])
            sid = vals[-1]
        fat_sids = []
        used = set()
        for s in difat:
            if s <= _MAXREGSECT and s < self.n_sectors and s not in used:
                used.add(s)
                fat_sids.append(s)
                if len(fat_sids) >= fat_needed:
                    break
        fat_bytes = b"".join(self._sector(s) for s in fat_sids)
        if not fat_bytes:
            raise CorruptDocumentError("OLE FAT이 비어 있습니다 (empty FAT)")
        self.fat = struct.unpack("<%dI" % (len(fat_bytes) // 4), fat_bytes)

        # directory
        dir_bytes = b"".join(self._sector(s) for s in self._chain(first_dir, self.fat))
        entries = []
        for i in range(len(dir_bytes) // 128):
            e = dir_bytes[i * 128:(i + 1) * 128]
            etype = e[66]
            if etype not in (1, 2, 5):
                entries.append(None)
                continue
            ent = _DirEntry()
            nlen = _u16(e, 64)
            ent.name = e[:max(0, min(nlen, 64) - 2)].decode("utf-16-le", "replace")
            ent.type = etype
            ent.left, ent.right, ent.child = struct.unpack_from("<3I", e, 68)
            ent.start = _u32(e, 116)
            ent.size = _u64(e, 120)
            if major == 3:
                ent.size &= 0xFFFFFFFF
            ent.path = None
            entries.append(ent)
        if not entries or entries[0] is None or entries[0].type != 5:
            raise CorruptDocumentError("OLE 루트 디렉터리가 없습니다 (missing root entry)")
        self.entries = entries
        root = entries[0]

        # mini stream & mini FAT
        self.minifat = ()
        self.mini_stream = b""
        if root.start <= _MAXREGSECT and root.size:
            ms = b"".join(self._sector(s) for s in self._chain(root.start, self.fat))
            self.mini_stream = ms[:root.size]
        if first_minifat <= _MAXREGSECT:
            mf = b"".join(self._sector(s) for s in self._chain(first_minifat, self.fat))
            self.minifat = struct.unpack("<%dI" % (len(mf) // 4), mf[:len(mf) // 4 * 4])

        self.warnings = []          # problems noticed while reading streams
        # path index (iterative walk of the red-black trees)
        self.by_path = {}
        stack = [(root.child, "")]
        visited = set()
        while stack:
            idx, prefix = stack.pop()
            if idx == _NOSTREAM or idx >= len(entries) or idx in visited:
                continue
            visited.add(idx)
            ent = entries[idx]
            if ent is None:
                continue
            stack.append((ent.left, prefix))
            stack.append((ent.right, prefix))
            ent.path = prefix + ent.name
            self.by_path.setdefault(ent.path.lower(), ent)
            if ent.type == 1:
                stack.append((ent.child, ent.path + "/"))

    def _sector(self, sid):
        off = (sid + 1) * self.ss
        if off >= len(self.data):
            raise CorruptDocumentError("OLE 섹터 범위 초과 (sector %d out of range)" % sid)
        chunk = self.data[off:off + self.ss]
        if len(chunk) < self.ss:
            chunk += b"\0" * (self.ss - len(chunk))
        return chunk

    def _chain(self, start, table, max_len=None):
        out = []
        sid = start
        seen = set()
        limit = max_len if max_len is not None else len(table) + 1
        while sid <= _MAXREGSECT and len(out) < limit:
            if sid in seen:
                raise CorruptDocumentError("OLE 섹터 체인 순환 (sector chain loop)")
            seen.add(sid)
            out.append(sid)
            if sid >= len(table):
                break
            sid = table[sid]
        return out

    def exists(self, path):
        return path.lower() in self.by_path

    def is_stream(self, path):
        e = self.by_path.get(path.lower())
        return e is not None and e.type == 2

    def listdir(self, storage):
        """Names of direct children of ``storage`` ('' for root)."""
        prefix = storage.lower().rstrip("/") + "/" if storage else ""
        out = []
        for key, ent in self.by_path.items():
            if key.startswith(prefix) and "/" not in key[len(prefix):]:
                out.append(ent.path[len(prefix):] if prefix else ent.path)
        return sorted(out)

    def streams(self):
        return sorted(e.path for e in self.by_path.values() if e.type == 2)

    def stream_key(self, path):
        """Identity of a stream's storage (mini or regular, start sector)."""
        ent = self.by_path.get(path.lower())
        if ent is None:
            return None
        return (ent.size < self.cutoff, ent.start)

    def read(self, path, max_size=MAX_STREAM_SIZE):
        ent = self.by_path.get(path.lower())
        if ent is None or ent.type != 2:
            raise KeyError(path)
        size = ent.size
        if size > max_size or size > len(self.data) + self.ss:
            raise CorruptDocumentError("스트림 크기 비정상 (%s: %d bytes)" % (path, size))
        if size == 0:
            return b""
        if size < self.cutoff:
            mss = self.mss
            chain = self._chain(ent.start, self.minifat, size // mss + 2)
            buf = b"".join(self.mini_stream[s * mss:(s + 1) * mss] for s in chain)
        else:
            chain = self._chain(ent.start, self.fat, size // self.ss + 2)
            buf = b"".join(self._sector(s) for s in chain if s < self.n_sectors)
        if len(buf) < size:
            self.warnings.append("%s: 스트림이 잘렸습니다 (truncated stream: %d of %d bytes)"
                                 % (path, len(buf), size))
        return buf[:size]


# =========================================================================
# AES-128 (decryption only, ECB) -- for distribution-protected HWP documents
# =========================================================================

_AES_TABLES = None


def _aes_tables():
    global _AES_TABLES
    if _AES_TABLES is not None:
        return _AES_TABLES

    def rotl8(x, s):
        return ((x << s) | (x >> (8 - s))) & 0xFF

    def gmul(a, b):
        r = 0
        while b:
            if b & 1:
                r ^= a
            a = ((a << 1) ^ 0x11B) if a & 0x80 else (a << 1)
            b >>= 1
        return r

    sbox = [0] * 256
    p = q = 1
    while True:
        p = (p ^ (p << 1) ^ (0x1B if p & 0x80 else 0)) & 0xFF
        q = (q ^ (q << 1)) & 0xFF
        q = (q ^ (q << 2)) & 0xFF
        q = (q ^ (q << 4)) & 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ rotl8(q, 1) ^ rotl8(q, 2) ^ rotl8(q, 3) ^ rotl8(q, 4)
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    td0 = []
    for x in range(256):
        s = inv[x]
        td0.append((gmul(s, 14) << 24) | (gmul(s, 9) << 16) | (gmul(s, 13) << 8) | gmul(s, 11))
    td1 = [((v >> 8) | (v << 24)) & 0xFFFFFFFF for v in td0]
    td2 = [((v >> 16) | (v << 16)) & 0xFFFFFFFF for v in td0]
    td3 = [((v >> 24) | (v << 8)) & 0xFFFFFFFF for v in td0]
    _AES_TABLES = (sbox, inv, td0, td1, td2, td3)
    return _AES_TABLES


class AES128Decryptor(object):
    """Pure-Python AES-128 block decryption (FIPS-197, T-table variant)."""

    def __init__(self, key):
        if len(key) != 16:
            raise ValueError("AES-128 key must be 16 bytes")
        sbox, inv, td0, td1, td2, td3 = _aes_tables()
        w = list(struct.unpack(">4I", key))
        rcon = 1
        for i in range(4, 44):
            t = w[i - 1]
            if i % 4 == 0:
                t = ((sbox[(t >> 16) & 255] << 24) | (sbox[(t >> 8) & 255] << 16)
                     | (sbox[t & 255] << 8) | sbox[t >> 24]) ^ (rcon << 24)
                rcon = ((rcon << 1) ^ 0x11B) if rcon & 0x80 else (rcon << 1)
            w.append(w[i - 4] ^ t)
        rk = []
        for r in range(10, -1, -1):
            rk.extend(w[4 * r:4 * r + 4])
        for j in range(4, 40):
            v = rk[j]
            rk[j] = (td0[sbox[v >> 24]] ^ td1[sbox[(v >> 16) & 255]]
                     ^ td2[sbox[(v >> 8) & 255]] ^ td3[sbox[v & 255]])
        self._rk = rk

    def decrypt_ecb(self, data):
        sbox, inv, td0, td1, td2, td3 = _aes_tables()
        rk = self._rk
        n = len(data) - len(data) % 16
        out = bytearray(n)
        st = struct.Struct(">4I")
        unpack, pack = st.unpack_from, st.pack_into
        for off in range(0, n, 16):
            s0, s1, s2, s3 = unpack(data, off)
            s0 ^= rk[0]
            s1 ^= rk[1]
            s2 ^= rk[2]
            s3 ^= rk[3]
            k = 4
            for _ in range(9):
                t0 = td0[s0 >> 24] ^ td1[(s3 >> 16) & 255] ^ td2[(s2 >> 8) & 255] ^ td3[s1 & 255] ^ rk[k]
                t1 = td0[s1 >> 24] ^ td1[(s0 >> 16) & 255] ^ td2[(s3 >> 8) & 255] ^ td3[s2 & 255] ^ rk[k + 1]
                t2 = td0[s2 >> 24] ^ td1[(s1 >> 16) & 255] ^ td2[(s0 >> 8) & 255] ^ td3[s3 & 255] ^ rk[k + 2]
                t3 = td0[s3 >> 24] ^ td1[(s2 >> 16) & 255] ^ td2[(s1 >> 8) & 255] ^ td3[s0 & 255] ^ rk[k + 3]
                s0, s1, s2, s3 = t0, t1, t2, t3
                k += 4
            o0 = ((inv[s0 >> 24] << 24) | (inv[(s3 >> 16) & 255] << 16)
                  | (inv[(s2 >> 8) & 255] << 8) | inv[s1 & 255]) ^ rk[40]
            o1 = ((inv[s1 >> 24] << 24) | (inv[(s0 >> 16) & 255] << 16)
                  | (inv[(s3 >> 8) & 255] << 8) | inv[s2 & 255]) ^ rk[41]
            o2 = ((inv[s2 >> 24] << 24) | (inv[(s1 >> 16) & 255] << 16)
                  | (inv[(s0 >> 8) & 255] << 8) | inv[s3 & 255]) ^ rk[42]
            o3 = ((inv[s3 >> 24] << 24) | (inv[(s2 >> 16) & 255] << 16)
                  | (inv[(s1 >> 8) & 255] << 8) | inv[s0 & 255]) ^ rk[43]
            pack(out, off, o0, o1, o2, o3)
        return bytes(out)


def _ms_rand(seed):
    """Microsoft Visual C ``srand(seed); rand()`` sequence."""
    state = seed & 0xFFFFFFFF
    while True:
        state = (state * 214013 + 2531011) & 0xFFFFFFFF
        yield (state >> 16) & 0x7FFF


def _distribution_keys(blob):
    """Candidate AES keys hidden in a HWPTAG_DISTRIBUTE_DOC_DATA payload.

    The 256-byte payload is XOR-scrambled with an MSVC rand() stream seeded
    by its first 4 bytes; the AES-128 key sits at offset 4 + (seed & 0xF).
    The most widely implemented variant leaves the 4 seed bytes untouched;
    the alternative is returned second as a fallback.
    """
    seed = _u32(blob, 0)
    keys = []
    for skip_seed in (True, False):
        rnd = _ms_rand(seed)
        buf = bytearray(blob[:256])
        count = 0
        key = 0
        for i in range(len(buf)):
            if count == 0:
                key = next(rnd) & 0xFF
                count = (next(rnd) & 0x0F) + 1
            if i >= 4 or not skip_seed:
                buf[i] ^= key
            count -= 1
        off = 4 + (seed & 0x0F)
        k = bytes(buf[off:off + 16])
        if len(k) == 16 and k not in keys:
            keys.append(k)
    return keys


# =========================================================================
# Compression helpers
# =========================================================================

class _Budget(object):
    """Per-document allowance of decompressed bytes / records / elements."""

    def __init__(self, nbytes=MAX_TOTAL_DECOMPRESSED, records=MAX_RECORDS, elements=MAX_ELEMENTS):
        self.bytes = nbytes
        self.records = records
        self.elements = elements

    def take_bytes(self, n, what):
        self.bytes -= n
        if self.bytes < 0:
            raise CorruptDocumentError(
                "%s: 문서 전체 압축 해제 한도(%d MB) 초과 (document too large when decompressed)"
                % (what, MAX_TOTAL_DECOMPRESSED // (1024 * 1024)))


def _inflate(data, what="stream", doc=None, limit=MAX_STREAM_SIZE, budget=None):
    """Raw-deflate (HWP) decompression with zip-bomb and truncation guards."""
    if budget is not None:
        limit = min(limit, max(0, budget.bytes))
    last_error = None
    for wbits in (-15, 15):
        d = zlib.decompressobj(wbits)
        parts = []
        total = 0
        capped = False
        try:
            step = 1 << 18
            for pos in range(0, len(data), step):
                chunk = data[pos:pos + step]
                while chunk:
                    out = d.decompress(chunk, 1 << 22)
                    parts.append(out)
                    total += len(out)
                    if total > limit:
                        capped = True
                        break
                    chunk = d.unconsumed_tail
                if d.eof or capped:
                    break
            if capped:
                # keep the first ``limit`` bytes; the record parser handles
                # the cut tail, so the reader gets everything up to the limit
                if doc is not None:
                    doc.warn("%s: 크기 한도에 도달해 이후 내용을 생략했습니다 "
                             "(size limit reached; rest omitted)" % what)
                result = b"".join(parts)[:limit]
                if budget is not None:
                    budget.bytes = 0
                return result
            parts.append(d.flush())
        except zlib.error as e:
            last_error = e
            if total and wbits == -15:
                if doc is not None:
                    doc.warn("%s: 압축 데이터 일부 손상, 앞부분만 사용 (partially corrupt: %s)" % (what, e))
                if budget is not None:
                    budget.take_bytes(total, what)
                return b"".join(parts)
            continue
        if not d.eof and data and not total:
            raise CorruptDocumentError("%s: 압축 스트림이 잘려 내용이 없습니다 (truncated deflate stream)"
                                       % what)
        if not d.eof and doc is not None and total:
            doc.warn("%s: 압축 스트림이 잘렸습니다 (truncated deflate stream)" % what)
        if budget is not None:
            budget.take_bytes(total, what)
        return b"".join(parts)
    raise CorruptDocumentError("%s 압축 해제 실패 (inflate failed: %s)" % (what, last_error))


# =========================================================================
# HWP 5.x records
# =========================================================================

TAG_DOCUMENT_PROPERTIES = 0x10
TAG_ID_MAPPINGS = 0x11
TAG_BIN_DATA = 0x12
TAG_FACE_NAME = 0x13
TAG_BORDER_FILL = 0x14
TAG_CHAR_SHAPE = 0x15
TAG_TAB_DEF = 0x16
TAG_NUMBERING = 0x17
TAG_BULLET = 0x18
TAG_PARA_SHAPE = 0x19
TAG_STYLE = 0x1A
TAG_DOC_DATA = 0x1B
TAG_DISTRIBUTE_DOC_DATA = 0x1C
TAG_PARA_HEADER = 0x42
TAG_PARA_TEXT = 0x43
TAG_PARA_CHAR_SHAPE = 0x44
TAG_PARA_LINE_SEG = 0x45
TAG_PARA_RANGE_TAG = 0x46
TAG_CTRL_HEADER = 0x47
TAG_LIST_HEADER = 0x48
TAG_PAGE_DEF = 0x49
TAG_FOOTNOTE_SHAPE = 0x4A
TAG_PAGE_BORDER_FILL = 0x4B
TAG_SHAPE_COMPONENT = 0x4C
TAG_TABLE = 0x4D
TAG_SC_LINE = 0x4E
TAG_SC_RECTANGLE = 0x4F
TAG_SC_ELLIPSE = 0x50
TAG_SC_ARC = 0x51
TAG_SC_POLYGON = 0x52
TAG_SC_CURVE = 0x53
TAG_SC_OLE = 0x54
TAG_SC_PICTURE = 0x55
TAG_SC_CONTAINER = 0x56
TAG_CTRL_DATA = 0x57
TAG_EQEDIT = 0x58
TAG_SC_TEXTART = 0x5A
TAG_FORM_OBJECT = 0x5B
TAG_MEMO_SHAPE = 0x5C
TAG_MEMO_LIST = 0x5D
TAG_CHART_DATA = 0x5F
TAG_VIDEO_DATA = 0x62

# PARA_TEXT control characters (code units 0..31)
_CHAR_CTRLS = frozenset([0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31])   # 1 code unit
_INLINE_CTRLS = frozenset([4, 5, 6, 7, 8, 9, 19, 20])                     # 8 code units
_EXT_CTRLS = frozenset([1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23])  # 8 code units, own CTRL_HEADER

# controls that carry no readable text
_SILENT_CTRLS = frozenset(["secd", "cold", "pghd", "pgct", "pgnp", "bokm",
                           "idxm", "nwno", "%%*d", "%%*a"])


class _Rec(object):
    __slots__ = ("tag", "level", "data", "children")

    def __init__(self, tag, level, data):
        self.tag = tag
        self.level = level
        self.data = data
        self.children = []


class _CtrlMark(object):
    __slots__ = ("code", "ctrl_id")

    def __init__(self, code, ctrl_id):
        self.code = code
        self.ctrl_id = ctrl_id


_FIELD_END_MARK = object()


def _iter_records(data, doc=None, what="stream"):
    pos = 0
    n = len(data)
    while pos + 4 <= n:
        hdr = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        tag = hdr & 0x3FF
        level = (hdr >> 10) & 0x3FF
        size = (hdr >> 20) & 0xFFF
        if size == 0xFFF:
            if pos + 4 > n:
                break
            size = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        if pos + size > n:
            if doc is not None:
                doc.warn("%s: 레코드가 잘렸습니다 (truncated record, tag 0x%X)" % (what, tag))
            yield tag, level, data[pos:]
            return
        yield tag, level, data[pos:pos + size]
        pos += size


def _build_tree(records, budget=None, what="stream", doc=None):
    root = _Rec(-1, -1, b"")
    stack = [root]
    count = 0
    for tag, level, payload in records:
        count += 1
        if budget is not None and count % 4096 == 0:
            if budget.records < 4096:
                # keep what was read so far instead of dropping the section
                budget.records = 0
                if doc is not None:
                    doc.warn("%s: 레코드 수 한도에 도달해 이후 내용을 생략했습니다 "
                             "(record limit reached; rest of the section omitted)" % what)
                break
            budget.records -= 4096
        node = _Rec(tag, level, payload)
        while len(stack) > 1 and stack[-1].level >= level:
            stack.pop()
        stack[-1].children.append(node)
        stack.append(node)
    return root.children


def _parse_para_text(data):
    """Split a PARA_TEXT payload into str pieces and control markers."""
    n = len(data) // 2
    if n == 0:
        return []
    units = struct.unpack("<%dH" % n, data[:2 * n])
    pieces = []
    i = 0
    run = 0
    while i < n:
        c = units[i]
        if c >= 32:
            i += 1
            continue
        if i > run:
            pieces.append(data[2 * run:2 * i].decode("utf-16-le", "replace"))
        if c in _CHAR_CTRLS:
            if c == 10:
                pieces.append("\n")
            elif c == 30 or c == 31:
                pieces.append(" ")
            # 24 is a discretionary hyphen (break point only): no visible text
            i += 1
        else:
            ctrl_id = (units[i + 1] | (units[i + 2] << 16)) if i + 2 < n else 0
            if c == 9:
                pieces.append("\t")
            elif c in _EXT_CTRLS:
                pieces.append(_CtrlMark(c, ctrl_id))
            elif c == 4:
                pieces.append(_FIELD_END_MARK)
            i += 8
        run = i
    if run < n:
        pieces.append(data[2 * run:2 * n].decode("utf-16-le", "replace"))
    return pieces


def _hyperlink_target(command):
    """Target of a HWP hyperlink command: ``<target>[|<label>];<type>;<x>;<y>;``.

    ``\\`` escapes ``: ? ; | \\``; other backslashes (Windows paths) are kept.
    Returns None for bookmark-only links (empty target).
    """
    if not command:
        return None
    out = []
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if c == "\\" and i + 1 < n and command[i + 1] in ":?;|\\":
            out.append(command[i + 1])
            i += 2
            continue
        if c in ";|":
            break
        out.append(c)
        i += 1
    return _usable_link("".join(out))


def _usable_link(target):
    """None for link targets a reader cannot follow: internal bookmark / outline
    ids ("?#123", "#x") and script or data URLs."""
    target = (target or "").strip()
    if not target or target.startswith(("?", "#")):
        return None
    if re.match(r"(?i)\s*(javascript|vbscript|data)\s*:", target):
        return None
    return target


def _param_string(params, name):
    """Value of ``<name>:wstring:<n>:<text>`` in a HWP parameter string."""
    if not params:
        return None
    m = re.search(r"(?:^|[\s:])%s:wstring:(\d+):" % re.escape(name), params)
    if not m:
        return None
    start = m.end()
    value = params[start:start + int(m.group(1))].strip()
    return value or None


def _button_caption(caption):
    """Form-control caption: '&&' is the Windows escape for a literal '&' and a
    single '&' marks the keyboard accelerator."""
    if not caption:
        return ""
    return re.sub(r"&(&?)", lambda m: "&" if m.group(1) else "", caption).strip()


def _clickhere_hint(command):
    """Guide text of a click-here field: ``...Direction:wstring:<n>:<text> HelpState...``."""
    return _param_string(command, "Direction")


_AUTONUM_TYPES = {0: "PAGE", 1: "FOOTNOTE", 2: "ENDNOTE", 3: "PICTURE", 4: "TABLE",
                  5: "EQUATION", 6: "TOTAL_PAGE"}


def _auto_number_text(kind, number, fmt, prefix=0, suffix=0):
    """Text of an auto-number control.  Page numbers cannot be known without
    layout, and footnote/endnote numbers are rendered by the note markers."""
    if isinstance(kind, int):
        kind = _AUTONUM_TYPES.get(kind, "UNKNOWN")
    if kind in ("PAGE", "TOTAL_PAGE", "FOOTNOTE", "ENDNOTE", "UNKNOWN") or not number:
        return None

    def ch(v):
        if isinstance(v, str):
            return v
        return chr(v) if 32 <= v < 0xD800 or 0xE000 <= v <= 0xFFFD else ""
    return ch(prefix) + format_number(number, fmt) + ch(suffix)


def _filetime_to_iso(v):
    if not v:
        return None
    try:
        dt = datetime.datetime(1601, 1, 1) + datetime.timedelta(microseconds=v // 10)
    except (OverflowError, ValueError):
        return None
    if dt.year < 1980 or dt.year > 2200:
        return None
    return dt.replace(microsecond=0).isoformat()


_SUMMARY_PIDS = {2: "title", 3: "subject", 4: "author", 5: "keywords",
                 6: "comments", 7: "template", 8: "last_saved_by",
                 9: "revision", 11: "last_printed", 12: "created",
                 13: "last_saved", 14: "page_count", 20: "date"}


def _parse_summary_info(data):
    """Parse an OLE property set stream (\\005HwpSummaryInformation)."""
    out = {}
    if len(data) < 48 or _u16(data, 0) != 0xFFFE:
        return out
    base = _u32(data, 44)
    nprops = _u32(data, base + 4)
    codepage = None
    for k in range(min(nprops, 256)):
        pid = _u32(data, base + 8 + 8 * k)
        poff = base + _u32(data, base + 12 + 8 * k)
        vt = _u16(data, poff)
        val = None
        if vt == 0x1F:                       # VT_LPWSTR
            cnt = _u32(data, poff + 4)
            raw = data[poff + 8:poff + 8 + 2 * min(cnt, 65536)]
            val = raw.decode("utf-16-le", "replace").split("\0", 1)[0]
        elif vt == 0x1E:                     # VT_LPSTR
            cnt = _u32(data, poff + 4)
            raw = data[poff + 8:poff + 8 + min(cnt, 65536)].split(b"\0", 1)[0]
            enc = "cp949" if codepage in (949, None) else "cp%d" % codepage
            try:
                val = raw.decode(enc, "replace")
            except LookupError:
                val = raw.decode("latin-1")
        elif vt == 0x40:                     # VT_FILETIME
            val = _filetime_to_iso(_u64(data, poff + 4))
        elif vt == 0x03:                     # VT_I4
            val = _i32(data, poff + 4)
        elif vt == 0x02:                     # VT_I2
            val = _i16(data, poff + 4)
            if pid == 1:
                codepage = val & 0xFFFF
                continue
        if pid == 1 or pid not in _SUMMARY_PIDS:
            continue
        if isinstance(val, str):
            val = val.strip()
        if val is None or val == "":
            continue
        if pid == 14 and (not isinstance(val, int) or val <= 0):
            continue
        out[_SUMMARY_PIDS[pid]] = val
    return out


class _BinItem(object):
    __slots__ = ("kind", "storage_id", "ext", "compression", "name")


def _parse_bin_item(d):
    it = _BinItem()
    prop = _u16(d, 0)
    t = prop & 0x0F
    it.kind = {0: "link", 1: "embedding", 2: "storage"}.get(t, "unknown")
    it.compression = (prop >> 4) & 0x03
    it.storage_id = None
    it.ext = ""
    it.name = None
    if t == 0:
        abs_path, o = _wstr(d, 2)
        rel_path, o = _wstr(d, o)
        it.name = rel_path or abs_path or None
    else:
        it.storage_id = _u16(d, 2)
        it.ext, _ = _wstr(d, 4)
        it.name = "BIN%04X.%s" % (it.storage_id, it.ext or "bin")
    return it


class _HwpParser(object):
    """HWP 5.x binary document -> Document."""

    def __init__(self, data, doc):
        self.doc = doc
        self.ole = CompoundFile(data)
        if not self.ole.is_stream("FileHeader"):
            raise UnsupportedFormatError(
                "HWP 문서가 아닌 OLE 파일입니다 (OLE file without HWP FileHeader; "
                "e.g. .doc/.xls)")
        fh = self.ole.read("FileHeader")
        if not fh.startswith(b"HWP Document File"):
            raise UnsupportedFormatError("HWP FileHeader 서명이 없습니다 (bad signature)")
        ver = _u32(fh, 32)
        flags = _u32(fh, 36)
        flags2 = _u32(fh, 40)
        self.ver = ((ver >> 24) & 255, (ver >> 16) & 255, (ver >> 8) & 255, ver & 255)
        doc.format = "hwp"
        doc.version = "%d.%d.%d.%d" % self.ver
        self.compressed = bool(flags & 0x01)
        self.distribution = bool(flags & 0x04)
        doc.flags = {
            "compressed": self.compressed,
            "password": bool(flags & 0x02),
            "distribution": self.distribution,
            "script": bool(flags & 0x08),
            "drm": bool(flags & 0x10),
            "xml_template": bool(flags & 0x20),
            "history": bool(flags & 0x40),
            "signature": bool(flags & 0x80),
            "certificate_encryption": bool(flags & 0x100),
            "certificate_drm": bool(flags & 0x400),
            "ccl": bool(flags & 0x800),
            "track_changes": bool(flags & 0x4000),
            "copy_restricted": bool(flags2 & 0x02),
        }
        if self.ver[0] < 5:
            raise UnsupportedFormatError("HWP %s 형식은 지원하지 않습니다 (only HWP 5.x)" % doc.version)
        if flags & 0x02:
            raise EncryptedDocumentError(
                "암호가 설정된 HWP 문서입니다. 한글에서 암호를 해제한 뒤 다시 저장해 주세요 "
                "(password-protected document)")
        if flags & (0x10 | 0x100 | 0x400):
            raise EncryptedDocumentError(
                "DRM/인증서로 암호화된 HWP 문서입니다 (DRM or certificate-encrypted document)")
        self.styles = []
        self.para_shapes = []
        self.bin_items = []
        self.numberings = {}
        self.bullets = {}
        self.outline_id = None
        self.numbering_state = None
        self.cur_section = None
        self.budget = _Budget()

    # -- streams ---------------------------------------------------------------
    def _stream(self, name, compressed=None):
        raw = self.ole.read(name)
        if compressed is None:
            compressed = self.compressed
        if compressed:
            return _inflate(raw, name, self.doc, budget=self.budget)
        self.budget.take_bytes(len(raw), name)
        return raw

    def parse(self):
        doc = self.doc
        self._read_summary()
        self._read_preview()
        try:
            self._read_docinfo()
        except (HwpReaderError, KeyError, struct.error) as e:
            doc.warn("DocInfo 해석 실패: %s" % e)
        self.numbering_state = _NumberingState(self.numberings)
        storage = "BodyText"
        if self.distribution:
            if self.ole.exists("ViewText"):
                storage = "ViewText"
            else:
                doc.warn("배포용 문서이지만 ViewText 저장소가 없습니다")
        names = []
        for name in self.ole.listdir(storage):
            m = re.match(r"^section(\d+)$", name, re.I)
            if m and self.ole.is_stream(storage + "/" + name):
                names.append((int(m.group(1)), name))
        names.sort()
        if not names:
            raise CorruptDocumentError("본문(%s/Section*) 스트림이 없습니다 (no body sections)" % storage)
        seen_streams = set()
        for idx, name in names:
            path = storage + "/" + name
            key = self.ole.stream_key(path)
            if key in seen_streams:
                doc.warn("%s: 다른 구역과 같은 데이터를 가리켜 건너뜁니다 (aliased stream skipped)" % path)
                continue
            seen_streams.add(key)
            if self.budget.bytes <= 0 or self.budget.records <= 0:
                doc.warn("문서 크기 한도에 도달해 나머지 구역을 생략했습니다 (size limit reached)")
                break
            try:
                raw = self.ole.read(path)
                if storage == "ViewText":
                    data = self._decrypt_view_text(raw, path)
                elif self.compressed:
                    data = _inflate(raw, path, doc, budget=self.budget)
                else:
                    self.budget.take_bytes(len(raw), path)
                    data = raw
                self._parse_section(data, path)
            except HwpReaderError as e:
                doc.warn("%s 해석 실패: %s" % (path, e))
            except (struct.error, IndexError, ValueError, KeyError) as e:
                doc.warn("%s 해석 중 오류: %r" % (path, e))
        for w in self.ole.warnings:
            doc.warn(w)
        if not _preview_fallback(doc, "PrvText") and not doc.sections:
            raise CorruptDocumentError("본문을 읽을 수 없습니다 (body could not be read)%s"
                                       % (": " + doc.warnings[0] if doc.warnings else ""))
        doc._bin_loader = self._load_bin
        doc._bin_names = [b.name for b in self.bin_items if b.name and b.kind != "link"]
        return doc

    def _read_summary(self):
        name = "\x05HwpSummaryInformation"
        if self.ole.is_stream(name):
            try:
                self.doc.metadata = _parse_summary_info(self.ole.read(name))
            except (HwpReaderError, struct.error) as e:
                self.doc.warn("요약 정보 해석 실패: %s" % e)

    def _read_preview(self):
        if self.ole.is_stream("PrvText"):
            try:
                raw = self.ole.read("PrvText")
                self.doc.preview_text = raw[:len(raw) // 2 * 2].decode("utf-16-le", "replace").rstrip("\0")
            except HwpReaderError:
                pass

    def _load_bin(self, name):
        item = None
        for b in self.bin_items:
            if b.name == name:
                item = b
                break
        path = "BinData/" + name
        if not self.ole.is_stream(path):
            return None
        raw = self.ole.read(path)
        comp = item.compression if item is not None else 0
        want = self.compressed if comp == 0 else comp == 1
        if want:
            try:
                return _inflate(raw, path)
            except CorruptDocumentError:
                return raw
        return raw

    # -- DocInfo -----------------------------------------------------------------
    def _read_docinfo(self):
        data = self._stream("DocInfo")
        for tag, level, d in _iter_records(data, self.doc, "DocInfo"):
            if tag == TAG_BIN_DATA:
                self.bin_items.append(_parse_bin_item(d))
            elif tag == TAG_PARA_SHAPE:
                self.para_shapes.append(self._parse_para_shape(d))
            elif tag == TAG_STYLE:
                name, o = _wstr(d, 0)
                eng, o = _wstr(d, o)
                self.styles.append(name or eng or None)
            elif tag == TAG_NUMBERING:
                self.numberings[len(self.numberings) + 1] = self._parse_numbering(d)
            elif tag == TAG_BULLET:
                self.bullets[len(self.bullets) + 1] = self._parse_bullet(d)

    @staticmethod
    def _parse_para_shape(d):
        prop1 = _u32(d, 0)
        head_type = (prop1 >> 23) & 0x03          # 0 none, 1 outline, 2 number, 3 bullet
        level = (prop1 >> 25) & 0x07               # 0-based, saturates at 6
        if len(d) >= 58:
            tail = _u32(d, 54)                     # true 0-based level 0..9 (5.0.2.5+)
            if 0 <= tail <= 9:
                level = max(level, tail)
        num_id = _u16(d, 30)                       # numbering / bullet id, 1-based, 0 = default
        return (head_type, level, num_id)

    @staticmethod
    def _parse_numbering(d):
        """HWPTAG_NUMBERING: per level a 12-byte head info + format string."""
        nd = _NumberingDef()
        o = 0
        for lv in range(1, 8):
            if o + 12 > len(d):
                break
            prop = _u32(d, o)
            o += 12
            tmpl, o = _wstr(d, o)
            nd.levels[lv] = [tmpl, (prop >> 5) & 0x1F, 1]
        o += 2                                     # UINT16 start: not the displayed start
        if o + 28 <= len(d):                       # per-level starts (5.0.2.5+)
            for lv in range(1, 8):
                if lv in nd.levels:
                    nd.levels[lv][2] = _u32(d, o) or 1
                o += 4
        for lv in range(8, 11):                    # levels 8..10 (5.1+), when present
            if o + 12 > len(d):
                break
            prop = _u32(d, o)
            o += 12
            tmpl, o = _wstr(d, o)
            nd.levels[lv] = [tmpl, (prop >> 5) & 0x1F, 1]
        for lv in range(8, 11):
            if lv in nd.levels and o + 4 <= len(d):
                nd.levels[lv][2] = _u32(d, o) or 1
                o += 4
        nd.levels = dict((k, tuple(v)) for k, v in nd.levels.items())
        return nd

    @staticmethod
    def _parse_bullet(d):
        ch = _u16(d, 12)
        return _bullet_char(chr(ch) if ch else "", "●")

    # -- distribution documents ------------------------------------------------
    def _decrypt_view_text(self, raw, what):
        hdr = _u32(raw, 0)
        tag = hdr & 0x3FF
        size = (hdr >> 20) & 0xFFF
        if tag != TAG_DISTRIBUTE_DOC_DATA or size < 256 or len(raw) < 4 + size:
            raise CorruptDocumentError("배포용 문서 헤더가 올바르지 않습니다 (bad DISTRIBUTE_DOC_DATA)")
        blob = raw[4:4 + 256]
        body = raw[4 + size:]
        last = None
        for key in _distribution_keys(blob):
            plain = AES128Decryptor(key).decrypt_ecb(body)
            try:
                data = _inflate(plain, what, budget=self.budget) if self.compressed else plain
            except CorruptDocumentError as e:
                last = e
                continue
            first = _u32(data, 0)
            if (first & 0x3FF) == TAG_PARA_HEADER and ((first >> 10) & 0x3FF) == 0:
                return data
            last = CorruptDocumentError("복호화 결과 검증 실패")
        raise CorruptDocumentError("배포용 문서 복호화 실패 (distribution decryption failed: %s)" % last)

    # -- body ----------------------------------------------------------------------
    def _parse_section(self, data, what):
        roots = _build_tree(_iter_records(data, self.doc, what), self.budget, what, self.doc)
        sec = Section()
        self.cur_section = sec
        self.doc.sections.append(sec)
        # find the section definition first: it carries the outline numbering id
        for node in roots[:1]:
            for ch in node.children:
                if ch.tag == TAG_CTRL_HEADER and _ctrl_id_str(_u32(ch.data, 0)) == "secd":
                    oid = _u16(ch.data, 18)        # 1-based numbering id for outlines
                    self.outline_id = oid if oid else self.outline_id
        # Memo bodies (MEMO_LIST, LIST_HEADER, PARA_HEADER...) and trailing
        # master-page lists (LIST_HEADER, PARA_HEADER...) follow the last
        # paragraph as level-1 records, so a level-based tree hangs them under
        # that paragraph.  A LIST_HEADER is never a legitimate direct child of
        # a PARA_HEADER, which makes the split unambiguous.  Detach them first.
        trailing = []                              # [kind, [nodes]]
        for node in roots:
            if node.tag == TAG_MEMO_LIST:
                trailing.append(["memo", [node]])
                continue
            if trailing and node.tag in (TAG_LIST_HEADER, TAG_PARA_HEADER) and node.level > 0:
                trailing[-1][1].append(node)
                continue
            kids = node.children
            start = None
            for i, ch in enumerate(kids):
                if ch.tag in (TAG_MEMO_LIST, TAG_LIST_HEADER):
                    start = i
                    break
            if start is None:
                continue
            for m in kids[start:]:
                if m.tag == TAG_MEMO_LIST:
                    trailing.append(["memo", [m]])
                elif m.tag == TAG_LIST_HEADER:
                    if trailing and trailing[-1][0] == "memo" and len(trailing[-1][1]) == 1:
                        trailing[-1][1].append(m)  # the memo's own list header
                    else:
                        trailing.append(["master", [m]])
                elif trailing:
                    trailing[-1][1].append(m)
            del kids[start:]
        for node in roots:
            if node.tag == TAG_PARA_HEADER:
                sec.blocks.append(self._paragraph(node, 0))
        for kind, group in trailing:
            if kind == "memo":
                number = _u32(group[0].data, 0, None)
                self.doc._memos[("hwp", number)] = self._list_blocks(group[1:], 1)
            else:
                blocks = self._list_blocks(group, 1)
                if blocks:
                    sec.headers.append(HeaderFooter("master", "both", blocks))

    def _apply_heading(self, p, shape_id):
        if not 0 <= shape_id < len(self.para_shapes):
            return
        head_type, level, num_id = self.para_shapes[shape_id]
        if head_type == 0:
            return
        if head_type == 1:
            p.heading_level = min(level + 1, 6)
            p.list_level = level
            nid = num_id or self.outline_id
            if nid:
                p.label = self.numbering_state.next_label(nid, level + 1)
        elif head_type == 2:
            p.list_type = "number"
            p.list_level = level
            p.label = self.numbering_state.next_label(num_id, level + 1)
        elif head_type == 3:
            p.list_type = "bullet"
            p.list_level = level
            p.label = self.bullets.get(num_id, "●")

    def _paragraph(self, node, depth):
        p = Paragraph()
        h = node.data
        shape_id = _u16(h, 8)
        style_id = _u8(h, 10)
        p.page_break = bool(_u8(h, 11) & 0x04)
        if 0 <= style_id < len(self.styles):
            p.style = self.styles[style_id]
        text_node = None
        ctrls = []
        for ch in node.children:
            if ch.tag == TAG_PARA_TEXT and text_node is None:
                text_node = ch
            elif ch.tag == TAG_CTRL_HEADER:
                ctrls.append(ch)
        pieces = _parse_para_text(text_node.data) if text_node is not None else []
        # numbering counters advance even for empty numbered paragraphs (as in Hangul)
        self._apply_heading(p, shape_id)
        # the k-th extended control char corresponds to the k-th CTRL_HEADER child
        k = 0
        for piece in pieces:
            if isinstance(piece, str):
                p.inlines.append(piece)
            elif piece is _FIELD_END_MARK:
                p.inlines.append(FieldEnd())
            elif k < len(ctrls):
                self._control(ctrls[k], p, depth, piece.ctrl_id)
                k += 1
        for c in ctrls[k:]:
            self._control(c, p, depth)
        return p

    def _control(self, node, p, depth, text_ctrl_id=None):
        cid = _ctrl_id_str(_u32(node.data, 0))
        if cid == "secd":
            # master pages (바탕쪽) live in the section definition's subtree
            for _lh, paras in self._groups(node.children):
                blocks = [self._paragraph(pp, depth + 1) for pp in paras]
                if blocks:
                    self.cur_section.headers.append(HeaderFooter("master", "both", blocks))
            return
        if cid in _SILENT_CTRLS:
            return
        if depth >= MAX_DEPTH:
            self.doc.warn("개체 중첩이 너무 깊어 일부를 생략했습니다 (nesting limit)")
            return
        try:
            if cid == "tbl ":
                p.inlines.append(self._table(node, depth + 1))
            elif cid == "gso ":
                p.inlines.extend(self._gso(node, depth + 1))
            elif cid == "eqed":
                p.inlines.append(self._equation(node))
                caption = []
                for _lh, paras in self._groups(node.children):
                    caption.extend(self._paragraph(pp, depth + 1) for pp in paras)
                if caption:
                    p.inlines.append(TextBox("caption", caption))
            elif cid == "form":
                text = self._form_object(node)
                if text:
                    p.inlines.append(text)
            elif cid in ("head", "foot"):
                prop = _u32(node.data, 4)
                apply = {0: "both", 1: "even", 2: "odd"}.get(prop & 0x03, "both")
                self.cur_section.headers.append(HeaderFooter(
                    "header" if cid == "head" else "footer", apply,
                    self._list_blocks(node.children, depth + 1)))
            elif cid in ("fn  ", "en  "):
                p.inlines.append(NoteRef("footnote" if cid == "fn  " else "endnote",
                                         self._list_blocks(node.children, depth + 1)))
            elif cid == "tcmt":
                p.inlines.append(Comment("hidden", self._list_blocks(node.children, depth + 1)))
            elif cid == "atno":
                s = _auto_number_text(_u32(node.data, 4) & 0x0F, _u16(node.data, 8),
                                      (_u32(node.data, 4) >> 4) & 0xFF,
                                      _u16(node.data, 12), _u16(node.data, 14))
                if s:
                    p.inlines.append(s)
            elif cid == "tdut":
                main, o = _wstr(node.data, 4)
                sub, o = _wstr(node.data, o)
                if main:
                    p.inlines.append("%s(%s)" % (main, sub) if sub else main)
            elif cid == "tcps":
                text, o = _wstr(node.data, 4)
                if text:
                    p.inlines.append(text)
            elif cid.startswith("%"):
                cmd, end = _wstr(node.data, 9)
                memo_mark = _ctrl_id_str(text_ctrl_id) if text_ctrl_id else ""
                if cid == "%%me" or memo_mark == "%%me" or cmd.startswith("MEMO/"):
                    number = _u32(node.data, end + 4, None)
                    if number is None:
                        parts = cmd.split("/")
                        number = _int(parts[2] if len(parts) > 2 else None, None)
                    p.inlines.append(FieldStart("memo", memo=("hwp", number)))
                elif cid == "%hlk":
                    p.inlines.append(FieldStart(cid, _hyperlink_target(cmd)))
                elif cid == "%clk":
                    p.inlines.append(FieldStart(cid, hint=_clickhere_hint(cmd)))
                else:
                    p.inlines.append(FieldStart(cid))
            else:
                blocks = self._all_paragraphs(node, depth + 1)
                if blocks:
                    p.inlines.append(TextBox(cid.strip() or "object", blocks))
        except (struct.error, IndexError, ValueError, KeyError, TypeError) as e:
            self.doc.warn("컨트롤 '%s' 해석 실패: %r" % (cid, e))

    @staticmethod
    def _form_object(node):
        """Text of a form control: check box / radio button state + caption."""
        for ch in node.children:
            if ch.tag != TAG_FORM_OBJECT:
                continue
            kind = ch.data[:4]
            params, _ = _wstr(ch.data, 12)
            caption = _button_caption(_param_string(params, "Caption"))
            value = re.search(r"Value:int:(-?\d+)", params)
            checked = bool(value and value.group(1) not in ("0", "-0"))
            if kind == b"tbc+":                    # check box
                return ("☑ " if checked else "☐ ") + (caption or "")
            if kind == b"tbr+":                    # radio button
                return ("◉ " if checked else "○ ") + (caption or "")
            if kind == b"tbp+" and caption:        # push button
                return "[%s]" % caption
            if kind == b"tde+":                    # edit box
                return _param_string(params, "Text") or None
            return None
        return None

    def _list_blocks(self, nodes, depth):
        """Paragraphs of LIST_HEADER-based lists among ``nodes``."""
        blocks = []
        for ch in nodes:
            if ch.tag == TAG_PARA_HEADER:
                blocks.append(self._paragraph(ch, depth))
            elif ch.tag == TAG_LIST_HEADER:
                for g in ch.children:
                    if g.tag == TAG_PARA_HEADER:
                        blocks.append(self._paragraph(g, depth))
        return blocks

    def _all_paragraphs(self, node, depth):
        blocks = []
        stack = [iter(node.children)]
        while stack:
            for ch in stack[-1]:
                if ch.tag == TAG_PARA_HEADER:
                    blocks.append(self._paragraph(ch, depth))
                else:
                    stack.append(iter(ch.children))
                    break
            else:
                stack.pop()
        return blocks

    @staticmethod
    def _groups(nodes):
        groups = []
        cur = None
        for k in nodes:
            if k.tag == TAG_LIST_HEADER:
                cur = (k, [g for g in k.children if g.tag == TAG_PARA_HEADER])
                groups.append(cur)
            elif k.tag == TAG_PARA_HEADER:
                if cur is None:
                    cur = (None, [])
                    groups.append(cur)
                cur[1].append(k)
        return groups

    def _table(self, node, depth):
        t = Table()
        kids = node.children
        ti = None
        for i, k in enumerate(kids):
            if k.tag == TAG_TABLE:
                ti = i
                break
        row_counts = []
        if ti is not None:
            td = kids[ti].data
            t.n_rows = _u16(td, 4)
            t.n_cols = _u16(td, 6)
            for r in range(t.n_rows):
                if 18 + 2 * r + 2 <= len(td):
                    row_counts.append(_u16(td, 18 + 2 * r))
            before, after = kids[:ti], kids[ti + 1:]
        else:
            before, after = [], kids
        for _lh, paras in self._groups(before):
            t.caption.extend(self._paragraph(pp, depth) for pp in paras)
        for lh, paras in self._groups(after):
            c = Cell()
            if lh is not None and len(lh.data) >= 16:
                c.col = _u16(lh.data, 8)
                c.row = _u16(lh.data, 10)
                c.col_span = max(1, _u16(lh.data, 12))
                c.row_span = max(1, _u16(lh.data, 14))
            c.blocks = [self._paragraph(pp, depth) for pp in paras]
            t.cells.append(c)
        if row_counts and sum(row_counts) == len(t.cells):
            i = 0
            for r, n in enumerate(row_counts):
                for _ in range(n):
                    t.cells[i].row_hint = r
                    i += 1
        _normalize_cells(t, self.doc)
        return t

    def _gso(self, node, depth):
        caption = []
        for _lh, paras in self._groups(node.children):
            caption.extend(self._paragraph(pp, depth) for pp in paras)
        items = []
        for ch in node.children:
            if ch.tag == TAG_SHAPE_COMPONENT:
                self._shape_component(ch, items, depth, 0)
        alt = self._object_description(node.data)
        pics = [x for x in items if isinstance(x, Picture)]
        if alt and pics and not pics[0].alt:
            pics[0].alt = alt
        if caption:
            if len(items) == 1 and isinstance(items[0], Picture):
                items[0].caption = caption
            elif len(items) == 1 and isinstance(items[0], TextBox):
                items[0].caption = caption
            else:
                items.append(TextBox("caption", caption))
        return items

    def _shape_component(self, sc, items, depth, level):
        if level > MAX_DEPTH:
            return
        for ch in sc.children:
            if ch.tag == TAG_SC_PICTURE:
                items.append(self._picture(ch))
            elif ch.tag == TAG_SC_OLE:
                items.append(Placeholder("ole"))
            elif ch.tag == TAG_SC_TEXTART:
                # four points (8 x INT32) then the text as a WORD-length string
                text, _ = _wstr(ch.data, 32)
                text = text.strip()
                if text:
                    para = Paragraph()
                    para.inlines.append(text)
                    items.append(TextBox("textart", [para]))
            elif ch.tag in (TAG_SHAPE_COMPONENT, TAG_SC_CONTAINER):
                self._shape_component(ch, items, depth, level + 1)
        blocks = []
        for _lh, paras in self._groups(sc.children):
            blocks.extend(self._paragraph(pp, depth) for pp in paras)
        if blocks:
            items.append(TextBox("textbox", blocks))

    def _picture(self, rec):
        bid = _u16(rec.data, 71)
        name = None
        if 1 <= bid <= len(self.bin_items):
            name = self.bin_items[bid - 1].name
        return Picture(name=name)

    @staticmethod
    def _object_description(d):
        # common object properties: id(4) prop(4) y(4) x(4) w(4) h(4) z(4)
        # margins(2*4) instance id(4) page-break prevention(4) -> description
        if len(d) < 46:
            return None
        text, end = _wstr(d, 44)
        if not text or end > len(d):
            return None
        return _clean_alt(text)

    def _equation(self, node):
        for ch in node.children:
            if ch.tag == TAG_EQEDIT:
                script, _ = _wstr(ch.data, 4)
                return Equation(_clean_script(script))
        return Equation("")


def _blocks_have_text(blocks, depth=0):
    if depth > MAX_DEPTH:
        return False
    for p in blocks:
        for it in p.inlines:
            if isinstance(it, str):
                if it.strip():
                    return True
            elif isinstance(it, Table):
                if _blocks_have_text(it.caption, depth + 1) or any(
                        _blocks_have_text(c.blocks, depth + 1) for c in it.cells):
                    return True
            elif isinstance(it, (TextBox, NoteRef, Comment)):
                if _blocks_have_text(it.blocks, depth + 1):
                    return True
            elif not isinstance(it, (FieldStart, FieldEnd)):
                return True
    return False


def _preview_fallback(doc, label):
    """Replace the body with the stored preview text when the body could not
    be read: no section survived, or reading reported problems and produced
    no text at all.  A cleanly parsed but empty document is left alone."""
    preview = doc.preview_text or ""
    if not re.sub(r"[\s<>]+", "", preview):
        return False
    if doc.sections and (not doc.warnings or
                         any(_blocks_have_text(s.blocks) for s in doc.sections)):
        return False
    doc.warn("본문을 읽지 못해 미리보기 텍스트(%s)로 대체합니다. 문서 앞부분만 들어 있습니다 "
             "(body unreadable; using the preview text, which holds only the beginning "
             "of the document)" % label)
    sec = Section()
    for line in preview.splitlines():
        p = Paragraph()
        p.inlines.append(line)
        sec.blocks.append(p)
    doc.sections[:] = [sec]
    return True


def _clean_script(script):
    """Equation scripts contain CR/LF, runs of spaces and Hancom's spacing
    tokens (` = quarter space, ~ = space); normalise them to single spaces."""
    return re.sub(r"[\s`~]+", " ", script or "").strip()


def _clean_alt(text):
    text = (text or "").strip()
    if not text:
        return None
    m = re.search(r"원본 그림의 이름\s*:\s*(.+?)(?=\s*원본 그림의 크기|[\r\n]|$)", text)
    if m:
        return m.group(1).strip()
    first = text.splitlines()[0].strip()
    if first in ("그림입니다.", "그리기 개체입니다.", "표입니다.", "수식입니다.",
                 "글상자입니다.", "사각형입니다.", "묶음 개체입니다."):
        return None
    return _shorten(text, 120)


def _normalize_cells(t, doc):
    """Make cell addresses consistent; fall back to grid filling when needed."""
    cells = t.cells
    if not cells:
        t.n_rows = t.n_cols = 0
        return
    if len(cells) > MAX_TABLE_CELLS:
        doc.warn("표 셀이 너무 많아 일부를 생략했습니다 (table truncated)")
        del cells[MAX_TABLE_CELLS:]
    limit = 4096                                   # plausible rows / columns per table
    for c in cells:
        c.row_span = min(max(1, c.row_span or 1), limit)
        c.col_span = min(max(1, c.col_span or 1), limit)
        if c.row is not None and not 0 <= c.row < 1 << 20:
            c.row = None
        if c.col is not None and not 0 <= c.col < 1 << 20:
            c.col = None
    if sum(c.row_span * c.col_span for c in cells) > 4 * MAX_TABLE_CELLS:
        doc.warn("표 셀 병합 범위가 비정상이라 병합을 무시했습니다 (cell spans ignored)")
        for c in cells:
            c.row_span = c.col_span = 1
    valid = all(c.row is not None and c.col is not None for c in cells)
    if valid:
        seen = set()
        for c in cells:
            if (c.row, c.col) in seen:
                valid = False
                break
            seen.add((c.row, c.col))
        if valid and t.n_rows and t.n_cols:
            if any(c.row >= t.n_rows or c.col >= t.n_cols for c in cells):
                valid = False
    if not valid:
        if any(c.row is not None or c.col is not None for c in cells):
            doc.warn("표 셀 주소가 비정상이라 순서대로 배치했습니다 (cell addresses rebuilt)")
        occupied = set()
        next_free = {}                             # row -> first column not known to be taken

        def take(c, r, col):
            c.row, c.col = r, col
            for rr in range(r, r + c.row_span):
                for cc in range(col, col + c.col_span):
                    occupied.add((rr, cc))

        if all(c.row_hint is not None for c in cells):
            # place each cell in its serialized row, at the first free column
            for c in cells:
                r = c.row_hint
                col = next_free.get(r, 0)
                while (r, col) in occupied:
                    col += 1
                next_free[r] = col
                take(c, r, col)
        else:
            n_cols = max(1, min(t.n_cols or max(c.col_span for c in cells), limit))
            r = col = 0
            for c in cells:
                while (r, col) in occupied:
                    col += 1
                    if col >= n_cols:
                        col = 0
                        r += 1
                c.col_span = min(c.col_span, n_cols - col)
                take(c, r, col)
    cells.sort(key=lambda c: (c.row, c.col))
    _clip_overlaps(cells, doc)
    # dimensions come from the cells present: declared counts can be bogus
    ext_r = max(c.row + c.row_span for c in cells)
    ext_c = max(c.col + c.col_span for c in cells)
    area = sum(c.row_span * c.col_span for c in cells)
    # Renumber rows/columns to the boundaries cells actually use.  This drops
    # rows/columns that no cell starts or ends in (empty gaps between far-apart
    # anchors, or slices covered by one big merged cell) without changing what
    # any cell holds or which cells share a row or column.
    rb = sorted(set([c.row for c in cells] + [c.row + c.row_span for c in cells]))
    cb = sorted(set([c.col for c in cells] + [c.col + c.col_span for c in cells]))
    if rb[0] != 0:
        rb.insert(0, 0)
    if cb[0] != 0:
        cb.insert(0, 0)
    ri = dict((v, i) for i, v in enumerate(rb))
    ci = dict((v, i) for i, v in enumerate(cb))
    for c in cells:
        r2, c2 = ri[c.row + c.row_span], ci[c.col + c.col_span]
        c.row, c.col = ri[c.row], ci[c.col]
        c.row_span, c.col_span = r2 - c.row, c2 - c.col
    if ext_r * ext_c > 4 * area + 64:
        doc.warn("표의 빈 행·열을 압축했습니다 (sparse table compacted)")
    ext_r = max(c.row + c.row_span for c in cells)
    ext_c = max(c.col + c.col_span for c in cells)
    if (t.n_rows or 0) > 2 * ext_r + 8 or (t.n_cols or 0) > 2 * ext_c + 8:
        doc.warn("표의 선언된 크기(%dx%d)가 실제 셀 범위(%dx%d)와 달라 실제 범위를 사용합니다"
                 % (t.n_rows or 0, t.n_cols or 0, ext_r, ext_c))
    t.n_rows, t.n_cols = ext_r, ext_c


def _clip_overlaps(cells, doc):
    """Shrink spans so no two cells cover the same slot (cells sorted by row, col).

    A later cell's anchor inside an earlier span clips that span; a later
    cell's own span is cut where it would run into slots already taken.
    Spans only ever shrink, so the result is rectangular HTML.
    """
    owner = {}
    clipped = False
    for c in cells:
        prev = owner.get((c.row, c.col))
        if prev is not None and prev is not c:
            clipped = True
            old = [(rr, cc) for rr in range(prev.row, prev.row + prev.row_span)
                   for cc in range(prev.col, prev.col + prev.col_span)]
            if prev.row == c.row:
                prev.col_span = max(1, min(prev.col_span, c.col - prev.col))
            else:
                prev.row_span = max(1, min(prev.row_span, c.row - prev.row))
            for slot in old:
                if owner.get(slot) is prev and not (
                        prev.row <= slot[0] < prev.row + prev.row_span and
                        prev.col <= slot[1] < prev.col + prev.col_span):
                    del owner[slot]
        # cut this cell's span where it meets slots that are already taken
        width = c.col_span
        for cc in range(c.col + 1, c.col + c.col_span):
            if (c.row, cc) in owner:
                width = cc - c.col
                break
        height = c.row_span
        for rr in range(c.row + 1, c.row + c.row_span):
            if any((rr, cc) in owner for cc in range(c.col, c.col + width)):
                height = rr - c.row
                break
        if (width, height) != (c.col_span, c.row_span):
            clipped = True
            c.col_span, c.row_span = width, height
        for rr in range(c.row, c.row + c.row_span):
            for cc in range(c.col, c.col + c.col_span):
                owner[(rr, cc)] = c
    if clipped:
        doc.warn("겹치는 셀 병합을 정리했습니다 (overlapping cell spans clipped)")


# =========================================================================
# HWPX (OWPML) reader
# =========================================================================

def _ln(tag):
    """Local name of an ElementTree tag (namespace stripped)."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _attr(el, name, default=None):
    """Attribute lookup ignoring namespaces."""
    v = el.get(name)
    if v is not None:
        return v
    for k, v in el.attrib.items():
        if _ln(k) == name:
            return v
    return default


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class _HwpxParser(object):
    """HWPX (zip + OWPML XML) document -> Document."""

    _SHAPES = frozenset(["rect", "ellipse", "arc", "polygon", "curve",
                         "connectLine", "line", "textart"])
    _PLACEHOLDERS = {"ole": "ole", "chart": "chart", "video": "video"}
    _FORM = frozenset(["btn", "radioBtn", "checkBtn", "comboBox", "listBox",
                       "edit", "scrollBar"])

    def __init__(self, data, doc):
        self.doc = doc
        try:
            self.zf = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, NotImplementedError,
                EOFError, OSError, KeyError, struct.error, UnicodeDecodeError) as e:
            raise CorruptDocumentError("ZIP 구조가 손상되었습니다 (bad zip: %s)" % e)
        self.names = {}
        for info in self.zf.infolist():
            self.names.setdefault(info.filename.lower(), info)
        self.budget = _Budget()
        doc.format = "hwpx"
        has_section = any(re.match(r"^contents/section\d+\.xml$", n) for n in self.names)
        if "mimetype" in self.names:
            mt = self._read("mimetype", 256).strip().decode("ascii", "replace")
            doc.flags["mimetype"] = mt
            if mt and "hwp" not in mt.lower() and not has_section:
                raise UnsupportedFormatError("HWPX가 아닌 ZIP 문서입니다 (mimetype %s)" % mt)
        if not has_section and "contents/content.hpf" not in self.names:
            raise UnsupportedFormatError(
                "ZIP 파일이지만 HWPX 문서가 아닙니다 (zip without Contents/section*.xml)")
        manifest = self.names.get("meta-inf/manifest.xml")
        if manifest is not None:
            raw = self._read("meta-inf/manifest.xml", MAX_XML_PART)
            if b"encryption-data" in raw or b"encryptiondata" in raw.lower():
                raise EncryptedDocumentError(
                    "암호가 설정된 HWPX 문서입니다 (password-protected HWPX)")
        self.styles = {}
        self.para_prs = {}
        self.numberings = {}
        self.bullets = {}
        self.bin_items = {}            # manifest id -> href
        self.section_paths = []
        self.outline_id = None
        self.numbering_state = None
        self.cur_section = None
        self._masters_seen = set()
        self._deleting = None          # Id of the tracked deletion being skipped
        self._closable = set()         # deletion Ids that end inside the current paragraph
        self._track_changes = False

    # -- zip access -----------------------------------------------------------------
    def _read(self, lname, limit=MAX_STREAM_SIZE, charge=True):
        info = self.names.get(lname.lower())
        if info is None:
            raise KeyError(lname)
        if info.file_size > limit:
            raise CorruptDocumentError("ZIP 항목이 너무 큽니다 (%s)" % info.filename)
        if info.file_size > 4 * 1024 * 1024 and info.file_size > 1000 * max(1, info.compress_size):
            raise CorruptDocumentError("비정상적인 압축률입니다 (zip bomb guard: %s)" % info.filename)
        if charge:
            self.budget.take_bytes(info.file_size, info.filename)
        try:
            with self.zf.open(info) as f:
                data = f.read(limit + 1)
        except (zipfile.BadZipFile, RuntimeError, zlib.error, NotImplementedError, EOFError,
                OSError, ValueError, struct.error, KeyError) as e:
            if "encrypt" in str(e).lower() or info.flag_bits & 0x1:
                raise EncryptedDocumentError("암호화된 ZIP 항목입니다 (%s)" % info.filename)
            raise CorruptDocumentError("ZIP 항목 읽기 실패 (%s: %s)" % (info.filename, e))
        if len(data) > limit:
            raise CorruptDocumentError("ZIP 항목이 너무 큽니다 (%s)" % info.filename)
        return data

    def _xml(self, lname, limit=MAX_XML_PART):
        raw = self._read(lname, limit)
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        # HWPX parts are UTF-8.  Refusing UTF-16/32 keeps the DTD check below
        # sound (a byte search would not see "<!DOCTYPE" in UTF-16).
        if b"\x00" in raw[:4] or raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            raise CorruptDocumentError("UTF-8이 아닌 XML 파트는 거부합니다 (non-UTF-8 XML: %s)" % lname)
        if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
            raise CorruptDocumentError("DTD/엔티티가 포함된 XML은 거부합니다 (%s)" % lname)
        # Pull parsing lets us count elements while the tree grows and stop a
        # document that would explode in memory (millions of tiny elements).
        parser = ET.XMLPullParser(events=("start",))
        root = None
        step = 1 << 20
        try:
            pos = 0
            while True:
                chunk = raw[pos:pos + step]
                pos += step
                if chunk:
                    parser.feed(chunk)
                else:
                    parser.close()
                count = 0
                for _event, el in parser.read_events():
                    if root is None:
                        root = el
                    count += 1
                if count:
                    if self.budget.elements < count:
                        # keep the part of the tree built so far
                        self.budget.elements = 0
                        self.doc.warn("%s: XML 요소 수 한도에 도달해 이후 내용을 생략했습니다 "
                                      "(element limit reached; rest omitted)" % lname)
                        break
                    self.budget.elements -= count
                if not chunk:
                    break
        except ET.ParseError as e:
            raise _XmlSyntaxError("XML 해석 실패 (%s: %s)" % (lname, e))
        if root is None:
            raise _XmlSyntaxError("XML 해석 실패 (%s: empty document)" % lname)
        return root

    _TOKEN_RE = re.compile(r"<[^<>]*>|[^<]+|<")
    _TAG_RE = re.compile(r"<(/?)(?:[\w.-]+:)?([\w.-]+)")

    def _salvage_section(self, path):
        """Best-effort text recovery from malformed section XML.

        A single linear scan over tags and text runs (no backtracking regex),
        collecting the text of <t> elements paragraph by paragraph.
        """
        try:
            raw = self._read(path, MAX_XML_PART, charge=False).decode("utf-8", "replace")
        except HwpReaderError:
            return False
        sec = Section()
        cur = None
        in_t = False
        for m in self._TOKEN_RE.finditer(raw):
            tok = m.group()
            if len(tok) > 1 and tok[0] == "<" and tok[-1] == ">":
                tm = self._TAG_RE.match(tok)
                if not tm:
                    continue
                closing, name = tm.group(1) == "/", tm.group(2)
                if name == "p" and not closing:
                    cur = Paragraph()
                    sec.blocks.append(cur)
                    in_t = False
                elif name == "t":
                    in_t = not closing and not tok.endswith("/>")
                elif name in ("tab", "lineBreak") and cur is not None:
                    cur.inlines.append("\t" if name == "tab" else "\n")
            elif in_t:
                if cur is None:
                    cur = Paragraph()
                    sec.blocks.append(cur)
                cur.inlines.append(html.unescape(tok))
        sec.blocks = [p for p in sec.blocks if any(isinstance(x, str) and x.strip() for x in p.inlines)]
        if not sec.blocks:
            return False
        self.doc.sections.append(sec)
        return True

    # -- package ---------------------------------------------------------------------
    def parse(self):
        doc = self.doc
        self._read_version()
        self._read_package()
        try:
            self._read_header()
        except (HwpReaderError, KeyError) as e:
            doc.warn("header.xml 해석 실패: %s" % e)
        self.numbering_state = _NumberingState(self.numberings)
        if "preview/prvtext.txt" in self.names:
            try:
                raw = self._read("preview/prvtext.txt", 4 * 1024 * 1024)
                doc.preview_text = _decode_text(raw)
            except HwpReaderError:
                pass
        if not self.section_paths:
            raise CorruptDocumentError("본문 섹션이 없습니다 (no sections)")
        for path in self.section_paths:
            if self.budget.bytes <= 0 or self.budget.elements <= 0:
                doc.warn("문서 크기 한도에 도달해 나머지 구역을 생략했습니다 (size limit reached)")
                break
            try:
                root = self._xml(path)
                self._section(root)
            except _XmlSyntaxError as e:
                if self._salvage_section(path):
                    doc.warn("%s: XML이 손상되어 텍스트만 복구했습니다 (malformed XML, text-only "
                             "recovery: %s)" % (path, e))
                else:
                    doc.warn("%s 해석 실패: %s" % (path, e))
            except EncryptedDocumentError:
                raise
            except HwpReaderError as e:
                doc.warn("%s 해석 실패: %s" % (path, e))
            except (KeyError, ValueError, RecursionError) as e:
                doc.warn("%s 해석 중 오류: %r" % (path, e))
        if not _preview_fallback(doc, "Preview/PrvText.txt") and not doc.sections:
            raise CorruptDocumentError("본문을 읽을 수 없습니다 (body could not be read)%s"
                                       % (": " + doc.warnings[0] if doc.warnings else ""))
        doc._bin_loader = self._load_bin
        doc._bin_names = sorted(set(h for h in self.bin_items.values()
                                    if h.lower().startswith("bindata/")))
        return doc

    def _read_version(self):
        if "version.xml" not in self.names:
            return
        try:
            root = self._xml("version.xml")
        except HwpReaderError:
            return
        parts = [_attr(root, k) for k in ("major", "minor", "micro", "buildNumber")]
        if all(p is not None for p in parts):
            self.doc.version = ".".join(parts)
        app = _attr(root, "application")
        if app:
            self.doc.metadata["application"] = app
            av = _attr(root, "appVersion")
            if av:
                self.doc.metadata["application"] += " " + av

    def _read_package(self):
        hpf = None
        if "meta-inf/container.xml" in self.names:
            try:
                root = self._xml("meta-inf/container.xml")
                for el in root.iter():
                    if _ln(el.tag) == "rootfile":
                        fp = _attr(el, "full-path") or ""
                        if fp.lower().endswith(".hpf") and fp.lower() in self.names:
                            hpf = fp.lower()
                            break
            except HwpReaderError as e:
                self.doc.warn("container.xml 해석 실패: %s" % e)
        if hpf is None and "contents/content.hpf" in self.names:
            hpf = "contents/content.hpf"
        spine_paths = []
        if hpf is not None:
            try:
                root = self._xml(hpf)
                base = hpf.rsplit("/", 1)[0] + "/" if "/" in hpf else ""
                items = {}
                md = self.doc.metadata
                for el in root.iter():
                    ln = _ln(el.tag)
                    if ln == "item":
                        iid = _attr(el, "id")
                        href = _attr(el, "href") or ""
                        resolved = self._resolve(href, base)
                        if iid and resolved:
                            items[iid] = resolved
                    elif ln == "itemref":
                        spine_paths.append(_attr(el, "idref"))
                    elif ln == "title" and (el.text or "").strip():
                        md.setdefault("title", el.text.strip())
                    elif ln == "language" and (el.text or "").strip():
                        md.setdefault("language", el.text.strip())
                    elif ln == "meta":
                        # the "content" attribute is a datatype ("text", "date"),
                        # never the value itself
                        name = _attr(el, "name")
                        val = (el.text or "").strip()
                        if name and val:
                            md.setdefault(_META_NAMES.get(name.lower(), name), val.strip())
                self.bin_items = dict((k, v) for k, v in items.items())
                ordered = []
                for idref in spine_paths:
                    p = items.get(idref)
                    if p and re.search(r"section\d+\.xml$", p, re.I) and p not in ordered:
                        ordered.append(p)
                self.section_paths = ordered
            except HwpReaderError as e:
                self.doc.warn("content.hpf 해석 실패: %s" % e)
        found = sorted((int(m.group(1)), n) for n in self.names
                       for m in [re.match(r"^contents/section(\d+)\.xml$", n)] if m)
        if not self.section_paths:
            self.section_paths = [n for _, n in found]
        else:
            extra = [n for _, n in found if n not in self.section_paths]
            if extra:
                self.doc.warn("spine에 없는 섹션 파일을 뒤에 추가했습니다: %s" % ", ".join(extra))
                self.section_paths.extend(extra)

    def _resolve(self, href, base):
        if not href:
            return None
        h = href.lstrip("/").lower()
        if h in self.names:
            return h
        b = (base + h).lower()
        if b in self.names:
            return b
        return h

    def _load_bin(self, name):
        key = name.lower()
        if key not in self.names:
            for v in self.bin_items.values():
                if v.rsplit("/", 1)[-1] == key:
                    key = v
                    break
        if key not in self.names:
            return None
        return self._read(key, charge=False)

    def _read_header(self):
        path = None
        for cand in ("contents/header.xml",):
            if cand in self.names:
                path = cand
        if path is None:
            return
        root = self._xml(path)
        for el in root.iter():
            ln = _ln(el.tag)
            if ln == "style":
                sid = _attr(el, "id")
                if sid is not None:
                    self.styles[sid] = (_attr(el, "name") or _attr(el, "engName"),
                                        _attr(el, "paraPrIDRef"))
            elif ln == "paraPr":
                pid = _attr(el, "id")
                if pid is None:
                    continue
                for h in el.iter():
                    if _ln(h.tag) == "heading":
                        self.para_prs[pid] = ((_attr(h, "type") or "NONE").upper(),
                                              _attr(h, "idRef"), _int(_attr(h, "level"), 0))
                        break
            elif ln == "numbering":
                nid = _attr(el, "id")
                if nid is None:
                    continue
                nd = _NumberingDef()
                default_start = _int(_attr(el, "start"), 1) or 1
                for ph in el:
                    if _ln(ph.tag) != "paraHead":
                        continue
                    lv = _int(_attr(ph, "level"), 0)
                    if lv <= 0:
                        continue
                    nd.levels[lv] = ((ph.text or ""), _attr(ph, "numFormat") or "DIGIT",
                                     _int(_attr(ph, "start"), default_start) or 1)
                self.numberings[nid] = nd
            elif ln == "bullet":
                bid = _attr(el, "id")
                if bid is not None:
                    self.bullets[bid] = _bullet_char((_attr(el, "char") or "").strip(), "●")

    # -- body ------------------------------------------------------------------------
    def _section(self, root):
        sec = Section()
        self.cur_section = sec
        self.doc.sections.append(sec)
        self._track_changes = any(_ln(e.tag) == "deleteBegin" for e in root.iter())
        if self._track_changes:
            self.doc.flags["track_changes"] = True
        for el in root.iter():
            if _ln(el.tag) == "secPr":
                oid = _attr(el, "outlineShapeIDRef")
                if oid not in (None, "", "0"):
                    self.outline_id = oid
                for mp in el:
                    if _ln(mp.tag) == "masterPage":
                        self._master_page(_attr(mp, "idRef"))
                    elif _ln(mp.tag) in ("header", "footer"):
                        # some writers put header/footer definitions in secPr;
                        # duplicates of hp:ctrl copies are merged when rendering
                        self._ctrl(mp, Paragraph(), 0)
                break
        for child in root:
            ln = _ln(child.tag)
            try:
                if ln == "p":
                    sec.blocks.append(self._p(child, 0))
                elif ln == "memogroup":
                    # alternate memo form: hs:sec > hp:memogroup > hp:memo(id) > hp:paraList > hp:p
                    for memo in child:
                        if _ln(memo.tag) == "memo":
                            self.doc._memos[("hwpx", _attr(memo, "id"))] = self._sublist(memo, 1)
                elif ln in ("tbl", "run", "ctrl"):
                    p = Paragraph()                      # tolerated top-level objects
                    self._run_child(child, p, 0)
                    sec.blocks.append(p)
            except RecursionError:
                self.doc.warn("문단 구조가 너무 깊어 한 문단을 생략했습니다 (paragraph nesting too deep)")

    def _master_page(self, item_id):
        """Read a master page (바탕쪽) part referenced from hp:secPr."""
        path = self.bin_items.get(item_id) if item_id else None
        if not path or path in self._masters_seen or path not in self.names:
            return
        self._masters_seen.add(path)
        try:
            root = self._xml(path)
        except HwpReaderError as e:
            self.doc.warn("바탕쪽 %s 해석 실패: %s" % (path, e))
            return
        blocks = self._sublist(root, 1)
        if blocks:
            apply = (_attr(root, "type") or "BOTH").lower()
            self.cur_section.headers.append(HeaderFooter("master", apply, blocks))

    def _apply_heading(self, p, para_pr_id):
        info = self.para_prs.get(para_pr_id)
        if not info:
            return
        typ, ref, level = info
        if typ == "OUTLINE":
            p.heading_level = min(level + 1, 6)
            p.list_level = level
            nid = ref if ref not in (None, "", "0") else self.outline_id
            if nid is not None:
                p.label = self.numbering_state.next_label(nid, level + 1)
        elif typ in ("NUMBER", "NUMBERING"):
            p.list_type = "number"
            p.list_level = level
            p.label = self.numbering_state.next_label(ref, level + 1)
        elif typ == "BULLET":
            p.list_type = "bullet"
            p.list_level = level
            p.label = self.bullets.get(ref, "●")

    def _p(self, el, depth):
        p = Paragraph()
        style = self.styles.get(_attr(el, "styleIDRef"))
        if style:
            p.style = style[0]
        p.page_break = _attr(el, "pageBreak") in ("1", "true")
        self._apply_heading(p, _attr(el, "paraPrIDRef"))
        # Track changes: text between deleteBegin/deleteEnd is deleted in the
        # final document.  Only ranges closed inside this paragraph are dropped,
        # so a missing deleteEnd can never swallow the rest of the document.
        saved = (self._deleting, self._closable)
        self._deleting = None
        self._closable = set()
        if self._track_changes:
            for sub in el.iter():
                if _ln(sub.tag) == "deleteEnd":
                    self._closable.add(_attr(sub, "Id"))
        try:
            for child in el:
                self._run_child(child, p, depth)
        finally:
            self._deleting, self._closable = saved
        return p

    def _sublist(self, el, depth):
        """Paragraphs under the first subList (or direct hp:p children)."""
        if depth >= MAX_DEPTH:
            self.doc.warn("개체 중첩이 너무 깊어 일부를 생략했습니다 (nesting limit)")
            return []
        blocks = []
        for child in el:
            ln = _ln(child.tag)
            if ln in ("subList", "paraList"):
                for pp in child:
                    if _ln(pp.tag) == "p":
                        blocks.append(self._p(pp, depth))
            elif ln == "p":
                blocks.append(self._p(child, depth))
        return blocks

    def _find(self, el, name):
        for child in el:
            if _ln(child.tag) == name:
                return child
        return None

    def _run_child(self, c, p, depth):
        if depth >= MAX_DEPTH:
            self.doc.warn("개체 중첩이 너무 깊어 일부를 생략했습니다 (nesting limit)")
            return
        ln = _ln(c.tag)
        if ln == "run":
            for cc in c:
                self._run_child(cc, p, depth + 1)
        elif ln == "t":
            self._text(c, p)
        elif ln == "ctrl":
            for cc in c:
                self._ctrl(cc, p, depth)
        elif ln in ("secPr", "linesegarray", "lineseg"):
            return
        elif ln == "switch":
            case = self._find(c, "case")
            target = case if case is not None else self._find(c, "default")
            if target is not None:
                for cc in target:
                    self._run_child(cc, p, depth + 1)
        elif ln == "tbl":
            p.inlines.append(self._table(c, depth + 1))
        elif ln == "pic":
            p.inlines.append(self._picture(c, depth + 1))
        elif ln in _TEXT_ATOMS:                       # tab/lineBreak/... directly under hp:run
            p.inlines.append(_TEXT_ATOMS[ln])
        elif ln == "equation":
            script = self._find(c, "script")
            p.inlines.append(Equation(_clean_script(script.text if script is not None else "")))
            cap = self._find(c, "caption")
            if cap is not None:
                blocks = self._sublist(cap, depth + 1)
                if blocks:
                    p.inlines.append(TextBox("caption", blocks))
        elif ln in self._SHAPES:
            self._shape(c, p, depth + 1)
        elif ln == "container":
            for cc in c:
                if _ln(cc.tag) in self._SHAPES or _ln(cc.tag) in ("pic", "container", "ole", "equation"):
                    self._run_child(cc, p, depth + 1)
            cap = self._find(c, "caption")
            if cap is not None:
                blocks = self._sublist(cap, depth + 1)
                if blocks:
                    p.inlines.append(TextBox("caption", blocks))
        elif ln in self._PLACEHOLDERS:
            p.inlines.append(Placeholder(self._PLACEHOLDERS[ln]))
        elif ln == "compose":
            text = _attr(c, "composeText")
            if text:
                p.inlines.append(text)
        elif ln == "dutmal":
            main = self._find(c, "mainText")
            sub = self._find(c, "subText")
            mt = (main.text or "") if main is not None else ""
            st = (sub.text or "") if sub is not None else ""
            if mt:
                p.inlines.append("%s(%s)" % (mt, st) if st else mt)
        elif ln in ("checkBtn", "radioBtn", "btn"):
            caption = _button_caption(_attr(c, "caption"))
            checked = (_attr(c, "value") or "").upper() == "CHECKED"
            if ln == "checkBtn":
                p.inlines.append(("☑ " if checked else "☐ ") + caption)
            elif ln == "radioBtn":
                p.inlines.append(("◉ " if checked else "○ ") + caption)
            elif caption:
                p.inlines.append("[%s]" % caption)
        elif ln == "edit":
            if not _attr(c, "passwordChar"):            # never reveal masked input
                txt = self._find(c, "text")
                value = (txt.text or "").strip() if txt is not None else ""
                if value:
                    p.inlines.append(value)
        elif ln in ("comboBox", "listBox"):
            selected = _attr(c, "selectedValue")
            if selected:
                shown = selected
                for item in c.iter():
                    if _ln(item.tag) == "listItem" and _attr(item, "value") == selected:
                        shown = _attr(item, "displayText") or selected
                        break
                p.inlines.append(shown)
        elif ln in self._FORM:
            return
        elif ln in ("fieldBegin", "fieldEnd", "footNote", "endNote", "header",
                    "footer", "hiddenComment", "autoNum"):
            self._ctrl(c, p, depth)
        else:
            blocks = self._nested_lists(c, depth + 1)
            if blocks:
                p.inlines.append(TextBox(ln, blocks))

    def _nested_lists(self, el, depth):
        """Paragraphs of every subList/paraList in ``el``'s subtree (outermost only)."""
        blocks = []
        stack = [iter(el)]
        while stack and depth < MAX_DEPTH:
            for sub in stack[-1]:
                if _ln(sub.tag) in ("subList", "paraList"):
                    blocks.extend(self._p(pp, depth) for pp in sub if _ln(pp.tag) == "p")
                elif _ln(sub.tag) != "p":
                    stack.append(iter(sub))
                    break
            else:
                stack.pop()
        return blocks

    def _text(self, t, p):
        if t.text and self._deleting is None:
            p.inlines.append(t.text)
        for c in t:
            ln = _ln(c.tag)
            if ln == "deleteBegin":
                if _attr(c, "Id") in self._closable:
                    self._deleting = _attr(c, "Id")
            elif ln == "deleteEnd":
                if _attr(c, "Id") == self._deleting:
                    self._deleting = None
            elif self._deleting is not None:
                continue                      # deleted content (tail skipped as well)
            elif ln in _TEXT_ATOMS:
                p.inlines.append(_TEXT_ATOMS[ln])
            elif ln not in ("markpenBegin", "markpenEnd", "titleMark", "insertBegin",
                            "insertEnd", "unknownch"):
                if c.text:
                    p.inlines.append(c.text)
            if c.tail and self._deleting is None:
                p.inlines.append(c.tail)

    def _ctrl(self, c, p, depth):
        ln = _ln(c.tag)
        if ln in ("header", "footer"):
            apply = (_attr(c, "applyPageType") or "BOTH").lower()
            self.cur_section.headers.append(HeaderFooter(ln, apply, self._sublist(c, depth + 1)))
        elif ln in ("footNote", "endNote"):
            p.inlines.append(NoteRef("footnote" if ln == "footNote" else "endnote",
                                     self._sublist(c, depth + 1)))
        elif ln == "hiddenComment":
            p.inlines.append(Comment("hidden", self._sublist(c, depth + 1)))
        elif ln == "fieldBegin":
            ftype = (_attr(c, "type") or "").upper()
            params = {}
            for prm in c.iter():
                if _ln(prm.tag) in ("stringParam", "integerParam"):
                    params.setdefault(_attr(prm, "name") or "", prm.text or "")
            if ftype == "MEMO" or params.get("Command", "").startswith("MEMO/"):
                blocks = self._sublist(c, depth + 1)
                key = blocks if blocks else ("hwpx", params.get("ID"))
                p.inlines.append(FieldStart("memo", memo=key))
            elif ftype == "HYPERLINK":
                url = _usable_link(params.get("Path")) or _hyperlink_target(params.get("Command"))
                p.inlines.append(FieldStart("hyperlink", url))
            elif ftype == "CLICK_HERE":
                hint = (params.get("Direction") or "").strip() or _clickhere_hint(params.get("Command"))
                p.inlines.append(FieldStart("click_here", hint=hint or None))
            else:
                p.inlines.append(FieldStart(ftype.lower() or "field"))
        elif ln == "fieldEnd":
            p.inlines.append(FieldEnd())
        elif ln == "autoNum":
            fmt = prefix = suffix = None
            for f in c:
                if _ln(f.tag) == "autoNumFormat":
                    fmt = _attr(f, "type")
                    prefix = _attr(f, "prefixChar") or ""
                    suffix = _attr(f, "suffixChar") or ""
            s = _auto_number_text((_attr(c, "numType") or "").upper(), _int(_attr(c, "num"), 0),
                                  fmt or "DIGIT", prefix or "", suffix or "")
            if s:
                p.inlines.append(s)
        # colPr, pageNum, newNum, pageHiding, pageNumCtrl, bookmark, indexmark: no text

    def _table(self, el, depth):
        t = Table()
        t.n_rows = _int(_attr(el, "rowCnt"), 0)
        t.n_cols = _int(_attr(el, "colCnt"), 0)
        if depth >= MAX_DEPTH:
            self.doc.warn("표 중첩이 너무 깊어 일부를 생략했습니다 (nesting limit)")
            t.n_rows = t.n_cols = 0                  # never trust the declared size
            return t
        tr_index = -1
        for child in el:
            ln = _ln(child.tag)
            if ln == "caption":
                t.caption.extend(self._sublist(child, depth + 1))
            elif ln == "tr":
                tr_index += 1
                for tc in child:
                    if _ln(tc.tag) != "tc":
                        continue
                    cell = Cell()
                    cell.row_hint = tr_index
                    for part in tc:
                        pl = _ln(part.tag)
                        if pl == "cellAddr":
                            cell.col = _int(_attr(part, "colAddr"), None)
                            cell.row = _int(_attr(part, "rowAddr"), None)
                        elif pl == "cellSpan":
                            cell.col_span = max(1, _int(_attr(part, "colSpan"), 1))
                            cell.row_span = max(1, _int(_attr(part, "rowSpan"), 1))
                        elif pl in ("subList", "paraList"):
                            cell.blocks.extend(self._p(pp, depth + 1) for pp in part
                                               if _ln(pp.tag) == "p")
                        elif pl == "p":                  # non-Hangul writers omit subList
                            cell.blocks.append(self._p(part, depth + 1))
                    t.cells.append(cell)
        _normalize_cells(t, self.doc)
        return t

    def _picture(self, el, depth):
        pic = Picture()
        for sub in el.iter():
            if _ln(sub.tag) == "img":
                ref = _attr(sub, "binaryItemIDRef")
                if ref:
                    href = self.bin_items.get(ref)
                    pic.name = href.rsplit("/", 1)[-1] if href else ref
                break
        comment = self._find(el, "shapeComment")
        if comment is not None:
            pic.alt = _clean_alt(comment.text)
        cap = self._find(el, "caption")
        if cap is not None:
            pic.caption = self._sublist(cap, depth + 1)
        return pic

    def _shape(self, el, p, depth):
        blocks = []
        dt = self._find(el, "drawText")
        if dt is not None:
            blocks = self._sublist(dt, depth + 1)
        if _ln(el.tag) == "textart":
            text = _attr(el, "text")
            if text:
                para = Paragraph()
                para.inlines.append(text)
                blocks.append(para)
        cap = self._find(el, "caption")
        caption = self._sublist(cap, depth + 1) if cap is not None else []
        if blocks or caption:
            p.inlines.append(TextBox(_ln(el.tag), blocks, caption))


_TEXT_ATOMS = {"tab": "\t", "lineBreak": "\n", "hyphen": "", "hypen": "",
               "nbSpace": " ", "fwSpace": " "}

_META_NAMES = {"creator": "author", "subject": "subject", "description": "comments",
               "lastsaveby": "last_saved_by", "createddate": "created",
               "modifieddate": "last_saved", "date": "date", "keyword": "keywords"}


def _decode_text(raw):
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if len(raw) >= 2 and raw[1:2] == b"\0":
        return raw.decode("utf-16-le", "replace")
    return raw.decode("cp949", "replace")


# =========================================================================
# Rendering (Markdown / plain text / JSON)
# =========================================================================

_HF_LABELS = {("header", "both"): "머리말", ("header", "even"): "머리말(짝수 쪽)",
              ("header", "odd"): "머리말(홀수 쪽)", ("footer", "both"): "꼬리말",
              ("footer", "even"): "꼬리말(짝수 쪽)", ("footer", "odd"): "꼬리말(홀수 쪽)"}


class _Renderer(object):
    def __init__(self, doc, mode, table_format="auto", headers_footers=True,
                 notes=True, comments=True, unwrap_single_cell=True):
        if table_format not in ("auto", "markdown", "html", "text"):
            raise ValueError("table_format must be auto|markdown|html|text")
        self.doc = doc
        self.mode = mode                    # 'markdown' | 'text' | 'json'
        self.table_format = table_format
        self.want_hf = headers_footers
        self.want_notes = notes
        self.want_comments = comments
        self.unwrap = unwrap_single_cell
        self.notes = []                     # (kind, number, blocks)
        self.counters = {"footnote": 0, "endnote": 0}
        self.comments = []                  # (kind, blocks, anchor text or None)
        self.used_memos = set()             # memo keys / ids already listed
        self.note_numbers = {}              # id(NoteRef) -> number

    # ---------------------------------------------------------------- inline
    def _note_label(self, kind, number):
        if self.mode == "markdown":
            return "[^%d]" % number if kind == "footnote" else "[^e%d]" % number
        return "[%s %d]" % ("각주" if kind == "footnote" else "미주", number)

    def _inline(self, it, target):
        """Short textual form of an inline object."""
        if isinstance(it, Picture):
            if target == "html" and it.path:
                s = '<img src="%s" alt="%s">' % (html.escape(it.path), html.escape(it.alt or ""))
            elif target == "markdown" and it.path:
                s = "![%s](%s)" % ((it.alt or it.name or "그림").replace("]", ")"), it.path.replace(" ", "%20"))
            else:
                label = it.alt or it.name
                s = "[그림: %s]" % label if label else "[그림]"
                if target == "html":
                    s = html.escape(s, quote=False)
            if it.caption:
                cap = self._flat(it.caption, target)
                if cap:
                    s += " " + cap
            return s
        if isinstance(it, Equation):
            s = "[수식: %s]" % it.script if it.script else "[수식]"
            return html.escape(s, quote=False) if target == "html" else s
        if isinstance(it, Placeholder):
            s = {"ole": "[OLE 개체]", "chart": "[차트]", "video": "[동영상]"}.get(it.kind, "[개체]")
            return s
        if isinstance(it, NoteRef):
            n = self.note_numbers.get(id(it))
            if n is None:
                self.counters[it.kind] += 1
                n = self.counters[it.kind]
                self.note_numbers[id(it)] = n
                self.notes.append((it.kind, n, it.blocks))
            return self._note_label(it.kind, n) if self.want_notes else ""
        return ""

    def _chunks(self, p, target):
        """Split a paragraph into ('text', str, objs) and ('block', obj) chunks."""
        chunks = []
        buf = []
        objs = []
        links = []
        if target == "html":
            def esc(s):
                return html.escape(s, quote=False)
        else:
            def esc(s):
                return s
        spaced = False                      # last item was an object marker
        for it in p.inlines:
            if isinstance(it, str):
                s = esc(_map_pua(it))
                if spaced and s and not s[0].isspace():
                    s = " " + s
                spaced = False
                buf.append(s)
            elif isinstance(it, FieldStart):
                if len(links) < 64:         # pathological nesting: ignore the excess
                    links.append((len(buf), it))
            elif isinstance(it, FieldEnd):
                if links:
                    pos, fs = links.pop()
                    if pos > len(buf) or (fs.memo is None and not fs.url and not fs.hint):
                        continue
                    inner = "".join(buf[pos:])
                    if fs.memo is not None:
                        self._add_memo(fs.memo, inner)
                    elif fs.url:
                        buf[pos:] = [self._link(inner, fs.url, target)]
                    elif inner.strip() in ("", esc(fs.hint)):
                        # an empty fill-in field; Hangul may store the guide
                        # text itself as the field's content
                        buf[pos:] = [esc("[입력란: %s]" % fs.hint)]
            elif isinstance(it, _INLINE_BLOCK_TYPES):
                for pos, fs in links:       # memos whose range spans this block
                    if fs.memo is not None:
                        self._add_memo(fs.memo, "".join(buf[pos:]) if pos <= len(buf) else "")
                if isinstance(it, Table) and self.unwrap and self.mode != "json" \
                        and _is_single_cell(it):
                    if buf:
                        chunks.append(("text", "".join(buf), objs))
                    buf, objs, links = [], [], []
                    chunks.append(("block", TextBox("cell", it.cells[0].blocks if it.cells else [],
                                                    it.caption)))
                    continue
                if buf or objs:
                    chunks.append(("text", "".join(buf), objs))
                buf, objs, links = [], [], []
                chunks.append(("block", it))
            elif isinstance(it, Comment):
                if id(it) not in self.used_memos:
                    self.used_memos.add(id(it))
                    self.comments.append((it.kind, it.blocks, None))
            else:
                s = self._inline(it, target)
                if s and not isinstance(it, NoteRef):
                    # keep object markers ("[그림: …]", "[수식: …]") apart from words
                    if buf and buf[-1] and not buf[-1][-1].isspace():
                        s = " " + s
                    spaced = True
                buf.append(s)
                objs.append(it)
        for pos, fs in links:                 # memo whose range continues past this paragraph
            if fs.memo is not None:
                self._add_memo(fs.memo, "".join(buf[pos:]) if pos <= len(buf) else "")
        if buf or objs or not chunks:
            chunks.append(("text", "".join(buf), objs))
        return chunks

    def _add_memo(self, memo, anchor):
        if isinstance(memo, list):
            if id(memo) in self.used_memos:
                return
            self.used_memos.add(id(memo))
            blocks = memo
        else:
            if memo in self.used_memos:
                return
            blocks = self.doc._memos.get(memo)
            if blocks is None:
                return
            self.used_memos.add(memo)
        anchor = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", anchor or "")).strip()
        self.comments.append(("memo", blocks, _shorten(anchor, 60) or None))

    def _leftover_memos(self):
        for key, blocks in self.doc._memos.items():
            if key not in self.used_memos:
                self.used_memos.add(key)
                self.comments.append(("memo", blocks, None))

    @staticmethod
    def _link(inner, url, target):
        if target == "markdown":
            label = inner.strip() or url
            return "[%s](%s)" % (label, url.replace(" ", "%20").replace(")", "%29"))
        if target == "html":
            return '<a href="%s">%s</a>' % (html.escape(url), inner or html.escape(url))
        if inner.strip() and inner.strip() != url:
            return "%s <%s>" % (inner, url)
        return inner or url

    def _flat(self, blocks, target, sep=" "):
        """Render blocks into a single line of text."""
        parts = []
        for p in blocks:
            for kind, a, *rest in self._chunks(p, target):
                if kind == "text":
                    t = a.strip()
                    if t:
                        parts.append(self._labelled(p, t))
                else:
                    parts.append(self._block_flat(a, target))
        return sep.join(x for x in parts if x)

    def _block_flat(self, obj, target):
        if isinstance(obj, Table):
            rows = []
            for row in self._grid_rows(obj):
                cells = [self._flat(c.blocks, target, " ") if c is not None else "" for c in row]
                rows.append(" | ".join(cells))
            body = " / ".join(r for r in rows if r.strip(" |"))
            cap = self._flat(obj.caption, target) if obj.caption else ""
            return " ".join(x for x in (cap, body) if x)
        if isinstance(obj, TextBox):
            return self._flat(obj.caption + obj.blocks, target)
        return ""

    @staticmethod
    def _labelled(p, text):
        if p.label and p.list_type != "bullet":
            return p.label + " " + text
        if p.label and p.list_type == "bullet":
            return p.label + " " + text
        return text

    # ---------------------------------------------------------------- blocks
    def blocks(self, blocks, depth=0):
        """Render paragraphs into a list of block strings (markdown/text)."""
        out = []
        for p in blocks:
            out.extend(self.paragraph(p, depth))
        return out

    def paragraph(self, p, depth):
        md = self.mode == "markdown"
        out = []
        first = True                        # nothing emitted yet for this paragraph
        decorated = False                   # label / heading already applied
        for kind, a, *rest in self._chunks(p, "markdown" if md else "text"):
            if kind == "text":
                toc_like = bool(re.search(r"\t\s*\d+\s*$", a))
                text = a.rstrip()
                if md:
                    text = "\n".join(line.strip() for line in text.split("\n"))
                    text = text.replace("\t", " ")
                else:
                    text = text.rstrip()
                if not text.strip():
                    if not md and first and not rest[0]:
                        out.append("")
                    continue
                if not decorated:
                    text = self._decorate(p, text.strip() if md else text, toc_like)
                    decorated = True
                first = False
                out.append(text)
            else:
                first = False
                out.append(self._block(a, depth + 1))
        return out

    def _decorate(self, p, text, toc_like=False):
        md = self.mode == "markdown"
        style = (p.style or "").strip().lower()
        is_toc = toc_like or style.startswith(("목차", "차례", "toc"))
        if p.heading_level and md and not is_toc and len(text) <= 120 and "\n" not in text:
            label = (p.label + " ") if p.label else ""
            return "#" * p.heading_level + " " + label + text
        if p.list_type == "bullet":
            indent = "  " * p.list_level
            if md:
                return indent + "- " + text
            return indent + (p.label or "•") + " " + text
        if p.label:
            indent = "  " * p.list_level if p.list_type == "number" else ""
            return indent + p.label + " " + text
        return text

    def _block(self, obj, depth):
        if depth > MAX_DEPTH:
            return ""
        if isinstance(obj, Table):
            return self.table(obj, depth)
        if isinstance(obj, TextBox):
            parts = self.blocks(obj.caption, depth) if obj.caption else []
            parts += self.blocks(obj.blocks, depth)
            sep = "\n\n" if self.mode == "markdown" else "\n"
            return sep.join(x for x in parts if x is not None).strip("\n")
        return ""

    # ---------------------------------------------------------------- tables
    def _dims(self, t):
        """Grid size to render, clamped to MAX_TABLE_CELLS (with a warning)."""
        n_rows = max(t.n_rows, 1)
        n_cols = max(t.n_cols, 1)
        if n_cols > MAX_TABLE_CELLS:
            n_cols = MAX_TABLE_CELLS
        if n_rows * n_cols > MAX_TABLE_CELLS:
            n_rows = max(1, MAX_TABLE_CELLS // n_cols)
            self.doc.warn("표가 너무 커서 일부 행을 생략했습니다 (table truncated)")
        return n_rows, n_cols

    def _grid_rows(self, t):
        """Rows of anchor cells (None where a slot is covered or empty)."""
        n_rows, n_cols = self._dims(t)
        grid = [[None] * n_cols for _ in range(n_rows)]
        for c in t.cells:
            if 0 <= c.row < n_rows and 0 <= c.col < n_cols:
                grid[c.row][c.col] = c
        return grid

    def _occupancy(self, t):
        """Grid of slots: None (hole) or (cell, is_anchor)."""
        n_rows, n_cols = self._dims(t)
        occ = [[None] * n_cols for _ in range(n_rows)]
        for c in t.cells:
            if not (0 <= c.row < n_rows and 0 <= c.col < n_cols):
                continue
            for r in range(c.row, min(n_rows, c.row + c.row_span)):
                row = occ[r]
                for k in range(c.col, min(n_cols, c.col + c.col_span)):
                    if row[k] is None:
                        row[k] = (c, False)
            occ[c.row][c.col] = (c, True)
        return occ

    def _cell_has_content(self, c):
        for p in c.blocks:
            for it in p.inlines:
                if isinstance(it, str):
                    if it.strip():
                        return True
                elif isinstance(it, FieldStart):
                    if it.hint or it.memo is not None:   # fill-in field / memo
                        return True
                elif not isinstance(it, FieldEnd):       # objects, comments, notes
                    return True
        return False

    def table(self, t, depth):
        fmt = self.table_format
        target = "markdown" if self.mode == "markdown" else "text"
        if not any(self._cell_has_content(c) for c in t.cells):
            caption = self._flat(t.caption, target)
            marker = "[빈 표 %d×%d]" % (max(t.n_rows, 1), max(t.n_cols, 1))
            return (caption + "\n" + marker) if caption else marker
        if fmt in ("auto", "text") and t.n_rows == 1 and self.unwrap:
            # one-row "title box" tables (e.g. | 1 | | 성 과 |) read better as a line
            texts = [self._cell_text(c, target, "text") for c in t.cells]
            texts = [x for x in texts if x]
            if len(texts) <= 4 and sum(len(x) for x in texts) <= 80 and not any(
                    isinstance(it, _INLINE_BLOCK_TYPES) for c in t.cells for p in c.blocks
                    for it in p.inlines):
                caption = self._flat(t.caption, target)
                line = " ".join(texts)
                return (caption + "\n" + line) if caption else line
        if self.mode == "text" and fmt == "auto":
            fmt = "text"
        if fmt == "auto":
            complex_ = any(c.row_span > 1 or c.col_span > 1 for c in t.cells) or \
                any(isinstance(it, _INLINE_BLOCK_TYPES) for c in t.cells for p in c.blocks for it in p.inlines)
            fmt = "html" if complex_ else "markdown"
        caption = self._flat(t.caption, "html" if fmt == "html" else target)
        if fmt == "html":
            return self._table_html(t, caption, depth)
        lines = []
        if caption:
            lines.append(caption)
        occ = self._occupancy(t)
        texts = {}
        repeat_budget = 4000                # characters of repeated labels per table
        for r, row in enumerate(occ):
            cells = []
            for k, slot in enumerate(row):
                if slot is None:
                    cells.append("")
                    continue
                cell, anchor = slot
                if id(cell) not in texts:
                    texts[id(cell)] = self._cell_text(cell, target, fmt)
                text = texts[id(cell)]
                if not anchor:
                    # A vertically merged label (e.g. a row heading spanning
                    # several rows) is repeated so each row stays readable;
                    # horizontal spans and long text are shown once only.
                    if k == cell.col and r > cell.row and len(text) <= 40 \
                            and repeat_budget >= len(text):
                        repeat_budget -= len(text)
                    else:
                        text = ""
                cells.append(text)
            lines.append("| " + " | ".join(cells) + " |")
            if r == 0 and fmt == "markdown":
                lines.append("|" + "|".join([" --- "] * len(row)) + "|")
        return "\n".join(lines)

    def _cell_text(self, cell, target, fmt):
        parts = []
        for p in cell.blocks:
            for kind, a, *rest in self._chunks(p, target):
                if kind == "text":
                    t = a.strip()
                    if t:
                        parts.append(self._labelled(p, t))
                else:
                    parts.append(self._block_flat(a, target))
        sep = "<br>" if fmt == "markdown" else " / "
        text = sep.join(x for x in parts if x)
        text = text.replace("\r", "").replace("\t", " ")
        if fmt == "markdown":
            text = text.replace("\n", "<br>").replace("|", "\\|")
        else:
            text = text.replace("\n", " ").replace("|", "¦")
        return text

    def _table_html(self, t, caption, depth, nl="\n"):
        lines = ["<table>"]
        if caption:
            lines.append("<caption>%s</caption>" % caption)
        occ = self._occupancy(t)
        for row in occ:
            tds = []
            for slot in row:
                if slot is None:
                    tds.append("<td></td>")
                    continue
                cell, anchor = slot
                if not anchor:
                    continue
                attrs = ""
                if cell.row_span > 1:
                    attrs += ' rowspan="%d"' % cell.row_span
                if cell.col_span > 1:
                    attrs += ' colspan="%d"' % cell.col_span
                tds.append("<td%s>%s</td>" % (attrs, self._html_blocks(cell.blocks, depth + 1)))
            lines.append("<tr>" + "".join(tds) + "</tr>")
        lines.append("</table>")
        return nl.join(lines)

    def _html_blocks(self, blocks, depth):
        if depth > MAX_DEPTH:
            self.doc.warn("개체 중첩이 너무 깊어 일부를 생략했습니다 (nesting limit)")
            return ""
        parts = []
        for p in blocks:
            for kind, a, *rest in self._chunks(p, "html"):
                if kind == "text":
                    t = a.strip().replace("\r", "").replace("\n", "<br>")
                    if t:
                        parts.append(html.escape(p.label, quote=False) + " " + t if p.label else t)
                elif isinstance(a, Table):
                    cap = self._flat(a.caption, "html")
                    parts.append(self._table_html(a, cap, depth, nl=""))
                elif isinstance(a, TextBox):
                    inner = self._html_blocks(a.caption + a.blocks, depth)
                    if inner:
                        parts.append(inner)
        return "<br>".join(parts)

    # ---------------------------------------------------------------- document
    def _collect_hf(self):
        seen = set()
        out = []
        for sec in self.doc.sections:
            for hf in sec.headers:
                text = self._flat(hf.blocks, "markdown" if self.mode == "markdown" else "text", " / ")
                if not text.strip():
                    continue
                key = (hf.kind, hf.apply, text)
                if key in seen:
                    continue
                seen.add(key)
                label = _HF_LABELS.get((hf.kind, hf.apply)) or \
                    ("바탕쪽" if hf.kind == "master" else _HF_LABELS.get((hf.kind, "both"), hf.kind))
                out.append((label, text))
        return out

    def _drain_notes(self):
        """Render queued notes (rendering can enqueue more notes)."""
        rendered = []
        i = 0
        while i < len(self.notes):
            kind, n, blocks = self.notes[i]
            body = self.blocks(blocks, 1)
            rendered.append((kind, n, body))
            i += 1
        return rendered

    def render(self):
        md = self.mode == "markdown"
        parts = []
        for sec in self.doc.sections:
            parts.extend(self.blocks(sec.blocks, 0))
            for memo in sec.memos:
                self.comments.append((memo.kind, memo.blocks, None))
        if md:
            body = "\n\n".join(x for x in parts if x and x.strip())
        else:
            body = "\n".join(parts)
            body = re.sub(r"\n{3,}", "\n\n", body)
        tail = []
        notes = self._drain_notes()
        if self.want_notes and notes:
            if md:
                for kind, n, body_blocks in notes:
                    label = "^%d" % n if kind == "footnote" else "^e%d" % n
                    text = "\n\n".join(b for b in body_blocks if b.strip()).strip()
                    text = text.replace("\n", "\n    ")
                    tail.append("[%s]: %s" % (label, text))
            else:
                for kind, n, body_blocks in notes:
                    text = "\n".join(b for b in body_blocks if b.strip()).strip()
                    tail.append("[%s %d] %s" % ("각주" if kind == "footnote" else "미주", n, text))
        extras = []
        if self.want_hf:
            for label, text in self._collect_hf():
                extras.append("%s: %s" % (label, text))
        self._leftover_memos()
        if self.want_comments and self.comments:
            for kind, blocks, anchor in self.comments:
                text = self._flat(blocks, "markdown" if md else "text", " / ")
                if text.strip():
                    label = "메모" if kind == "memo" else "숨은 설명"
                    if anchor:
                        label += ' ("%s")' % anchor
                    extras.append("%s: %s" % (label, text))
        out = body.strip("\n")
        if tail:
            out += "\n\n" + ("\n\n" if md else "\n").join(tail)
        if extras:
            out += "\n\n---\n" + ("\n\n" if md else "\n").join(extras)
        return out.lstrip("\n") + "\n"

    # ---------------------------------------------------------------- JSON
    def render_dict(self):
        self.mode = "json"
        sections = []
        for i, sec in enumerate(self.doc.sections):
            s = {"index": i, "blocks": self._json_blocks(sec.blocks, 0)}
            hfs = []
            for hf in sec.headers:
                text = self._flat(hf.blocks, "text", " / ")
                if text.strip():
                    hfs.append({"type": hf.kind, "apply": hf.apply, "text": text})
            s["headers_footers"] = hfs            # always present (may be empty)
            for memo in sec.memos:
                self.comments.append((memo.kind, memo.blocks, None))
            sections.append(s)
        notes = []
        i = 0
        while i < len(self.notes):
            kind, n, blocks = self.notes[i]
            notes.append({"type": kind, "number": n, "text": self._flat(blocks, "text", "\n")})
            i += 1
        self._leftover_memos()
        comments = []
        for kind, blocks, anchor in self.comments:
            text = self._flat(blocks, "text", "\n")
            if text.strip():
                c = {"type": kind, "text": text}
                if anchor:
                    c["anchor"] = anchor
                comments.append(c)
        out = {
            "file": self.doc.path,
            "format": self.doc.format,
            "version": self.doc.version,
            "metadata": self.doc.metadata,
            "flags": self.doc.flags,
            "sections": sections,
        }
        out["notes"] = notes                      # always present (may be empty)
        out["comments"] = comments
        out["warnings"] = self.doc.warnings
        return out

    def _json_blocks(self, blocks, depth):
        out = []
        if depth > MAX_DEPTH:
            return out
        for p in blocks:
            for kind, a, *rest in self._chunks(p, "text"):
                if kind == "text":
                    text = a.rstrip()
                    if not text.strip() and not rest[0]:
                        continue
                    d = {"type": "paragraph", "text": text}
                    if p.style:
                        d["style"] = p.style
                    if p.heading_level:
                        d["heading_level"] = p.heading_level
                    if p.list_type:
                        d["list_type"] = p.list_type
                        d["list_level"] = p.list_level
                    if p.label:
                        d["label"] = p.label
                    if p.page_break:
                        d["page_break"] = True
                    objs = []
                    for o in rest[0]:
                        if isinstance(o, Picture):
                            od = {"type": "image", "name": o.name}
                            if o.alt:
                                od["alt"] = o.alt
                            if o.path:
                                od["path"] = o.path
                            if o.caption:
                                od["caption"] = self._flat(o.caption, "text")
                            objs.append(od)
                        elif isinstance(o, Equation):
                            objs.append({"type": "equation", "script": o.script})
                        elif isinstance(o, Placeholder):
                            objs.append({"type": o.kind})
                    if objs:
                        d["objects"] = objs
                    out.append(d)
                elif isinstance(a, Table):
                    out.append(self._json_table(a, depth + 1))
                elif isinstance(a, TextBox):
                    d = {"type": "textbox", "kind": a.kind,
                         "blocks": self._json_blocks(a.blocks, depth + 1)}
                    if a.caption:
                        d["caption"] = self._flat(a.caption, "text")
                    out.append(d)
        return out

    def _json_table(self, t, depth):
        occ = self._occupancy(t)
        grid = []
        cells = []
        for c in t.cells:
            cd = {"row": c.row, "col": c.col, "text": self._flat(c.blocks, "text", "\n")}
            if c.row_span > 1:
                cd["row_span"] = c.row_span
            if c.col_span > 1:
                cd["col_span"] = c.col_span
            if any(isinstance(it, _INLINE_BLOCK_TYPES) for p in c.blocks for it in p.inlines):
                cd["blocks"] = self._json_blocks(c.blocks, depth + 1)
            cells.append(cd)
        texts = dict(((c["row"], c["col"]), c["text"]) for c in cells)
        for r, row in enumerate(occ):
            line = []
            for k, slot in enumerate(row):
                if slot is None:
                    line.append("")                  # empty slot (no cell)
                elif slot[1]:
                    line.append(texts.get((r, k)))   # anchor cell
                else:
                    line.append(None)                # covered by a merged cell
            grid.append(line)
        d = {"type": "table", "rows": t.n_rows, "cols": t.n_cols, "cells": cells, "grid": grid}
        if t.caption:
            d["caption"] = self._flat(t.caption, "text")
        return d


def _is_single_cell(t):
    return t.n_rows <= 1 and t.n_cols <= 1 and len(t.cells) <= 1


# =========================================================================
# Public API
# =========================================================================

def detect_format(data):
    """Return 'hwp', 'hwpx', 'hwp3', 'hwpml' or None from the leading bytes."""
    if data[:8] == _OLE_MAGIC:
        return "hwp"
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "hwpx"
    if data[:17] == b"HWP Document File":
        return "hwp3"
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"<?xml") or head.startswith(b"<HWPML"):
        if b"<HWPML" in data[:8192]:
            return "hwpml"
    return None


def read_document(source, password=None):
    """Read a .hwp / .hwpx file (path, bytes or binary file object)."""
    path = None
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
    elif hasattr(source, "read"):
        data = source.read(MAX_INPUT_SIZE + 1)
    else:
        path = os.fspath(source)
        if os.path.isdir(path):
            raise HwpReaderError("파일이 아니라 디렉터리입니다: %s (is a directory)" % path)
        try:
            size = os.path.getsize(path)
            if size > MAX_INPUT_SIZE:
                raise CorruptDocumentError("파일이 너무 큽니다 (> %d bytes)" % MAX_INPUT_SIZE)
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            raise HwpReaderError("파일을 열 수 없습니다: %s (%s)" % (path, e.strerror or e))
    if len(data) > MAX_INPUT_SIZE:
        raise CorruptDocumentError("파일이 너무 큽니다 (> %d bytes)" % MAX_INPUT_SIZE)
    if not data:
        raise CorruptDocumentError("빈 파일입니다 (empty file)")
    kind = detect_format(data)
    doc = Document()
    doc.path = path
    if kind in ("hwp", "hwpx"):
        parser = _HwpParser if kind == "hwp" else _HwpxParser
        try:
            parser(data, doc).parse()
        except HwpReaderError:
            raise
        except (MemoryError, RecursionError) as e:
            raise CorruptDocumentError("문서가 너무 크거나 복잡합니다 (%s)" % type(e).__name__)
        except Exception as e:  # malformed input must never escape as a random exception
            raise CorruptDocumentError("문서 구조를 해석할 수 없습니다 (%s: %s)"
                                       % (type(e).__name__, e))
    elif kind == "hwp3":
        raise UnsupportedFormatError(
            "HWP 3.x 이하(한글 97 등) 형식은 지원하지 않습니다. 한글에서 HWP 5.x(.hwp) 또는 "
            ".hwpx로 다시 저장해 주세요 (HWP 3.x / HWP 97 is not supported)")
    elif kind == "hwpml":
        raise UnsupportedFormatError(
            "HWPML(.hml, XML) 형식은 지원하지 않습니다. .hwp 또는 .hwpx로 저장해 주세요 "
            "(HWPML is not supported)")
    else:
        raise UnsupportedFormatError(
            "HWP/HWPX 파일이 아닙니다 (not a HWP 5.x or HWPX file)")
    return doc


def _write_output(text, output):
    if output and output != "-":
        d = os.path.dirname(os.path.abspath(output))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(output, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return
    stream = sys.stdout
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    try:
        stream.write(text)
    except UnicodeEncodeError:
        stream.buffer.write(text.encode("utf-8", "replace"))
    stream.flush()


def _page(text, offset=0, max_chars=0):
    """Return the [offset, offset+max_chars) window of ``text`` with notices.

    Pages end at a blank line when possible, never inside an open HTML
    <table>, and the notice states the exact --offset of the next page.
    """
    total = len(text)
    offset = max(0, offset or 0)
    if offset >= total and offset:
        return "[... offset %d은(는) 문서 끝(전체 %d자)을 넘었습니다 / offset beyond end ...]\n" % (
            offset, total)
    body = text[offset:]
    head = ""
    if offset:
        head = "[... 앞부분 %d자 생략 / skipped first %d chars ...]\n\n" % (offset, offset)
    if not max_chars or len(body) <= max_chars:
        return head + body
    cut = body.rfind("\n\n", 0, max_chars)
    if cut < max_chars * 0.5:
        cut = body.rfind("\n", 0, max_chars)
        if cut < max_chars * 0.5:
            # one very long line (e.g. a big HTML table): end after a table
            # row, a <br> or at least at a space rather than inside a word
            window = body[:max_chars]
            best = max(window.rfind("</tr>") + 5 if "</tr>" in window else -1,
                       window.rfind("<br>") + 4 if "<br>" in window else -1,
                       window.rfind(" "))
            cut = best if best >= max_chars * 0.5 else max_chars
    # do not end inside a table that could start on the next page instead
    depth = 0
    outer = -1
    for m in re.finditer(r"<(/?)table\b", body[:cut]):
        if m.group(1):
            depth = max(0, depth - 1)
            if depth == 0:
                outer = -1
        else:
            if depth == 0:
                outer = m.start()
            depth += 1
    if outer >= 0 and outer > max_chars * 0.3:
        cut = outer
    cut = max(1, cut)
    nxt = offset + cut
    return (head + body[:cut].rstrip() +
            "\n\n[... 출력이 잘렸습니다: 전체 %d자 중 %d~%d자 표시. 이어서 읽으려면 --offset %d "
            "옵션을 쓰세요 / truncated: %d of %d chars shown, continue with --offset %d ...]\n"
            % (total, offset, nxt, nxt, cut, total, nxt))


_DOC_EXTS = (".hwp", ".hwpx")
MAX_ARCHIVE_MEMBERS = 1000


def _zip_member_name(info):
    """Decode a zip member name; Korean names from Windows tools are often CP949."""
    name = info.filename
    if not info.flag_bits & 0x800:
        try:
            name = name.encode("cp437").decode("cp949")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name


class _ZipMember(object):
    """Lazy loader for one document inside an uploaded .zip (read on demand)."""

    def __init__(self, path, name, size):
        self.path = path
        self.name = name
        self.size = size

    def __call__(self):
        if self.size > MAX_INPUT_SIZE:
            raise CorruptDocumentError("압축 안의 파일이 너무 큽니다 (member too large)")
        try:
            with zipfile.ZipFile(self.path) as zf:
                with zf.open(self.name) as f:
                    return f.read(MAX_INPUT_SIZE + 1)
        except HwpReaderError:
            raise
        except Exception as e:
            raise CorruptDocumentError("압축 해제 실패 (%s)" % e)


def iter_input_documents(path):
    """Yield (display_name, source) for ``path``.

    A plain .hwp/.hwpx yields itself.  A .zip archive that is *not* an HWPX
    package yields each .hwp/.hwpx member as a zero-argument callable that
    returns its bytes, so users can upload documents zipped when a chat client
    refuses .hwp/.hwpx attachments.  A directory yields its .hwp/.hwpx/.zip
    files (not recursive).  ``source`` may also be an HwpReaderError to report.
    """
    if os.path.isdir(path):
        found = False
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if os.path.isfile(full) and name.lower().endswith(_DOC_EXTS + (".zip",)):
                found = True
                for item in iter_input_documents(full):
                    yield item
        if not found:
            yield path, UnsupportedFormatError(
                "디렉터리에 .hwp/.hwpx/.zip 파일이 없습니다 (no documents in directory)")
        return
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        yield path, path
        return
    if head not in (b"PK\x03\x04", b"PK\x05\x06"):
        yield path, path
        return
    try:
        zf = zipfile.ZipFile(path)
    except Exception:  # let read_document report the problem
        yield path, path
        return
    with zf:
        infos = zf.infolist()
    lower = [i.filename.lower() for i in infos]
    if any(re.match(r"^contents/section\d+\.xml$", n) for n in lower) or \
            "contents/content.hpf" in lower:
        yield path, path
        return
    members = [i for i in infos
               if not i.is_dir() and _zip_member_name(i).lower().endswith(_DOC_EXTS)
               and not os.path.basename(_zip_member_name(i)).startswith("._")]
    if not members:
        yield path, path
        return
    if len(members) > MAX_ARCHIVE_MEMBERS:
        yield path, CorruptDocumentError(
            "압축 안의 문서가 너무 많아 앞의 %d개만 읽습니다 (too many members)" % MAX_ARCHIVE_MEMBERS)
        members = members[:MAX_ARCHIVE_MEMBERS]
    for info in members:
        yield "%s!%s" % (path, _zip_member_name(info)), _ZipMember(path, info.filename, info.file_size)


def _build_arg_parser():
    ap = argparse.ArgumentParser(
        prog="hwp_reader.py",
        description="Read Hancom HWP 5.x (.hwp) and HWPX (.hwpx) documents "
                    "(Python standard library only).")
    ap.add_argument("files", nargs="+", metavar="FILE",
                    help=".hwp or .hwpx file(s), a .zip containing them, or a directory. "
                         "The format is detected from the content, not the extension.")
    ap.add_argument("-f", "--format", choices=("markdown", "md", "text", "txt", "json"),
                    default="markdown", help="output format (default: markdown)")
    ap.add_argument("-o", "--output", metavar="PATH",
                    help="write output to PATH instead of stdout (UTF-8). When the input "
                         "expands to several documents (several files, a .zip or a "
                         "directory) or PATH is an existing directory, one file per "
                         "document is written into the directory PATH.")
    ap.add_argument("--info", action="store_true",
                    help="print metadata, statistics and a short preview as JSON")
    ap.add_argument("--tables", choices=("auto", "markdown", "html", "text"), default="auto",
                    help="table rendering: auto = Markdown tables, HTML when cells are "
                         "merged or nested (default)")
    ap.add_argument("--no-headers-footers", action="store_true",
                    help="omit page headers/footers and master pages (머리말/꼬리말/바탕쪽)")
    ap.add_argument("--no-notes", action="store_true", help="omit footnotes/endnotes")
    ap.add_argument("--no-comments", action="store_true",
                    help="omit hidden comments and memos (숨은 설명/메모)")
    ap.add_argument("--extract-images", metavar="DIR",
                    help="save embedded pictures to DIR (one sub-folder per document when "
                         "there are several) and link them from the output")
    ap.add_argument("--max-chars", type=int, default=0, metavar="N",
                    help="print at most N characters per document; the output ends with a "
                         "notice giving the --offset of the next page (not for JSON)")
    ap.add_argument("--offset", type=int, default=0, metavar="N",
                    help="start at character N of the rendered output; take N from the "
                         "previous page's notice")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return ap


def _render(doc, args):
    fmt = {"md": "markdown", "txt": "text"}.get(args.format, args.format)
    opts = dict(table_format=args.tables, headers_footers=not args.no_headers_footers,
                notes=not args.no_notes, comments=not args.no_comments)
    if args.info:
        return json.dumps(doc.info(), ensure_ascii=False, indent=2) + "\n"
    if fmt == "json":
        return doc.to_json(**opts) + "\n"
    if fmt == "text":
        return doc.to_text(**opts)
    return doc.to_markdown(**opts)


def _output_name(label, used):
    base = os.path.splitext(os.path.basename(label.replace("!", "/").replace("\\", "/")))[0]
    base = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", base).strip(" .") or "document"
    name = base
    k = 2
    while name in used:
        name = "%s-%d" % (base, k)
        k += 1
    used.add(name)
    return name


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):    # Korean messages regardless of locale
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = _build_arg_parser()
    args = ap.parse_args(argv)
    fmt = {"md": "markdown", "txt": "text"}.get(args.format, args.format)
    ext = {"markdown": ".md", "text": ".txt", "json": ".json"}[fmt]
    if args.info:
        ext = ".info.json"
    if args.max_chars < 0 or args.offset < 0:
        ap.error("--max-chars and --offset must be >= 0")
    if (args.max_chars or args.offset) and (fmt == "json" or args.info):
        ap.error("--max-chars/--offset는 JSON 출력에 쓸 수 없습니다. -o 로 파일에 저장하세요 "
                 "(paging would produce invalid JSON; use -o)")
    inputs = []
    for path in args.files:
        inputs.extend(iter_input_documents(path))
    multi = len([s for _, s in inputs if not isinstance(s, HwpReaderError)]) > 1
    to_dir = bool(args.output) and (multi or os.path.isdir(args.output) or
                                    args.output.endswith(("/", os.sep)))
    exit_code = 0
    combined = []
    used_names = set()
    for label, source in inputs:
        name = _output_name(label, used_names) if (to_dir or multi) else None
        try:
            if isinstance(source, HwpReaderError):
                raise source
            if callable(source):
                source = source()
            doc = read_document(source)
            doc.path = label
            if args.extract_images:
                img_dir = os.path.join(args.extract_images, name) if multi else args.extract_images
                doc.extract_images(img_dir)
            out = _page(_render(doc, args), args.offset, args.max_chars)
            for w in doc.warnings:
                sys.stderr.write("경고(warning) [%s]: %s\n" % (os.path.basename(label), w))
        except HwpReaderError as e:
            sys.stderr.write("오류(error) [%s]: %s\n" % (label, e))
            exit_code = exit_code or e.exit_code
            continue
        except Exception as e:  # pragma: no cover - last-resort guard
            sys.stderr.write("오류(error) [%s]: 예기치 않은 오류 %r\n" % (label, e))
            exit_code = exit_code or 1
            continue
        try:
            if to_dir:
                target = os.path.join(args.output, name + ext)
                _write_output(out, target)
                sys.stderr.write("저장(written): %s\n" % target)
            elif args.output:
                _write_output(out, args.output)
            elif multi:
                combined.append("===== %s =====\n%s" % (label, out))
            else:
                combined.append(out)
        except OSError as e:
            sys.stderr.write("오류(error) [%s]: 출력 파일을 쓸 수 없습니다 (%s)\n" % (label, e))
            exit_code = exit_code or 1
    if combined:
        _write_output("\n".join(combined), None)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
