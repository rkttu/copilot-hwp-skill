# hwp-reader reference

Details that `SKILL.md` leaves out: the full command line, the JSON layout, the Python API, what each format feature turns into, and troubleshooting.

## Command line

```text
python3 scripts/hwp_reader.py FILE [FILE ...] [options]
```

| Option | Default | Description |
| --- | --- | --- |
| `-f`, `--format` | `markdown` | `markdown` (`md`), `text` (`txt`) or `json` |
| `-o`, `--output PATH` | stdout | Write UTF-8 output. If the input expands to several documents (several files, a `.zip`, a directory), or `PATH` is an existing directory or ends with `/`, `PATH` is a directory that gets one `<name>.md/.txt/.json` per document; each written path is printed to stderr |
| `--info` | off | Print JSON with metadata, statistics, a 400-character preview and warnings |
| `--tables` | `auto` | `auto`: Markdown table, or HTML when cells are merged or contain tables or text boxes. `markdown`: always Markdown (a short vertically merged label is repeated in each row it spans; other merged cells appear once). `html`: always HTML. `text`: pipe-separated rows |
| `--extract-images DIR` | off | Save pictures referenced in the body to `DIR` and link them (`![alt](DIR/BIN0001.png)`). With several documents each gets its own sub-folder `DIR/<name>/`; existing files are never overwritten |
| `--no-headers-footers` | off | Omit 머리말/꼬리말 and 바탕쪽 |
| `--no-notes` | off | Omit 각주/미주 markers and their text |
| `--no-comments` | off | Omit 숨은 설명 and 메모 |
| `--max-chars N` | 0 (no limit) | Print at most `N` characters per document. Pages end at a blank line or line break where possible and avoid ending inside an HTML table that could start on the next page; a table or line longer than `N` is split after a table row, a `<br>` or a space. The page ends with a notice giving the `--offset` of the next page. Not allowed with `-f json` / `--info` |
| `--offset N` | 0 | Start printing at character `N` of the rendered output. Take `N` from the previous page's notice |
| `--version`, `--help` | | |

Exit codes: `0` success, `1` unexpected error or missing file, `2` invalid arguments, `3` unsupported format, `4` encrypted or password-protected, `5` corrupt file. When several files are given, all of them are processed, and the exit code is the first non-zero one.

Inputs:

- `.hwp` (HWP 5.0 to 5.1.x, compressed or not, including 배포용 distribution documents).
- `.hwpx` (OWPML).
- A `.zip` archive that contains `.hwp`/`.hwpx` files. Each member is read; CP949-encoded member names from Windows zip tools are decoded, and macOS `__MACOSX/._*` entries are skipped.
- A directory: its `.hwp`, `.hwpx` and `.zip` files are read in name order (not recursive).
- Any file above with a wrong or missing extension. The format is detected from the leading bytes.

## Output conventions

| Document feature | Markdown | Plain text |
| --- | --- | --- |
| Outline paragraph (개요 1-7) | `#`-`######` heading with the numbering label | Numbering label + text |
| Numbered paragraph (문단 번호) | Label + text, indented by level | Same |
| Bulleted paragraph (글머리표) | `- text` | Bullet character + text |
| Table | GFM table, or HTML with `rowspan`/`colspan` | `\| a \| b \|` rows; a short vertically merged label is repeated per row |
| Table caption | Line before the table or `<caption>` | Line before the table |
| Single-cell table (box) | Unwrapped into normal paragraphs | Same |
| Short one-row table (title box, ≤ 4 cells, ≤ 80 chars) | Cell texts on one line | Same |
| Table without any text | `[빈 표 R×C]` | Same |
| Text box, drawing object text | Normal paragraphs in place | Same |
| Picture | `[그림: alt-or-name]` or `![alt](path)` after `--extract-images` | `[그림: ...]` |
| Equation | `[수식: script]` (HWP equation script) | Same |
| Footnote, endnote | `[^1]`, `[^e1]` + definitions at the end | `[각주 1]`, `[미주 1]` + list at the end |
| Hyperlink | `[text](url)`; links to bookmarks inside the document or to `javascript:` are plain text | `text <url>` |
| Private-use glyphs (Wingdings bullets, old-Hangul "ᄒᆞᆫ") | Mapped to Unicode (`☑`, `•`, `❖`, `ᄒᆞᆫ`…) | Same |
| Empty fill-in field (누름틀) | `[입력란: guide text]` | Same |
| Form check box, radio button | `☐`/`☑`, `○`/`◉` + caption | Same |
| Tab, line break | Space, newline | Tab, newline |
| 덧말 (ruby) | `본말(덧말)` | Same |
| Auto number in a caption (표/그림/수식 번호) | The stored number, e.g. `표 3` | Same |
| Page header/footer | `머리말: ...` after the final `---` | Same |
| Master page (바탕쪽) | `바탕쪽: ...` after `---` | Same |
| Memo | `메모 ("anchored text"): ...` after `---` | Same |
| Hidden comment | `숨은 설명: ...` after `---` | Same |
| OLE object, chart, video | `[OLE 개체]`, `[차트]`, `[동영상]` | Same |

Identical headers, footers and master pages are listed once. Page numbers are not available (no layout is computed).

Track changes (변경 추적): the final text is returned. For HWP 5.x the body stream already holds the final text; for HWPX, text between `deleteBegin` and `deleteEnd` inside one paragraph is dropped (a deletion spanning paragraphs is kept).

## JSON layout

```json
{
  "file": "보고서.hwp",
  "format": "hwp",
  "version": "5.1.0.1",
  "metadata": {"title": "...", "author": "...", "created": "2024-01-02T03:04:05", "last_saved": "..."},
  "flags": {"compressed": true, "distribution": false, "password": false},
  "sections": [
    {
      "index": 0,
      "blocks": [
        {"type": "paragraph", "text": "1. 개요", "style": "개요 1", "heading_level": 1, "label": "1."},
        {"type": "table", "rows": 2, "cols": 2, "caption": "표 1. ...",
         "cells": [{"row": 0, "col": 0, "col_span": 2, "text": "머리"}, {"row": 1, "col": 0, "text": "a"}, {"row": 1, "col": 1, "text": "b"}],
         "grid": [["머리", null], ["a", "b"]]},
        {"type": "textbox", "kind": "textbox", "blocks": [{"type": "paragraph", "text": "..."}]}
      ],
      "headers_footers": [{"type": "header", "apply": "both", "text": "..."}, {"type": "master", "apply": "both", "text": "..."}]
    }
  ],
  "notes": [{"type": "footnote", "number": 1, "text": "..."}],
  "comments": [{"type": "memo", "text": "...", "anchor": "commented text"}, {"type": "hidden", "text": "..."}],
  "warnings": []
}
```

- Paragraph keys `style`, `heading_level`, `list_type`, `list_level`, `label`, `page_break` and `objects` appear only when they apply. `objects` lists the inline pictures (`image`), equations (`equation`) and embedded objects in that paragraph.
- In `grid`, `null` marks a slot covered by a merged cell, and `""` an empty slot. `cells[].row_span`/`col_span` appear only when they are greater than 1. A cell that holds nested tables or text boxes also has `blocks`.
- HWP metadata comes from the summary information stream. HWPX metadata comes from `Contents/content.hpf`. The keys present depend on the document.
- JSON keeps every table as a table (no unwrapping of one-cell or title-box tables). `rows`/`cols` are the size actually used by the cells: declared sizes are ignored and rows or columns that no cell starts or ends in are merged away.
- `notes`, `comments` and each section's `headers_footers` are always present (possibly empty). `flags` differs by format: HWP has `compressed`, `password`, `distribution`, `drm`, `track_changes`…; HWPX has `mimetype` (and `track_changes` when present). A `textbox` block may have `caption`; an `image` object may have `alt`, `path` (after `--extract-images`) and `caption`.

`--info` statistics: `characters` (non-whitespace text in the body, cells, text boxes and notes), `markdown_chars` (length of the default Markdown output; use it to decide whether to page), `sections`, `paragraphs`, `non_empty_paragraphs`, `tables` (one-cell tables count as `text_boxes`), `pictures`, `equations`, `footnotes`, `endnotes`, `text_boxes`, `headers_footers`, `comments`.

## Python API

```python
import sys
sys.path.insert(0, "/app/skills/hwp-reader/scripts")   # folder that holds hwp_reader.py
import hwp_reader

doc = hwp_reader.read_document("보고서.hwp")   # also accepts bytes or a binary file object
print(doc.format, doc.version, doc.metadata)
md = doc.to_markdown(table_format="auto", headers_footers=True, notes=True, comments=True)
txt = doc.to_text()
data = doc.to_dict()            # same layout as -f json
info = doc.info()               # same as --info
doc.extract_images("/tmp/img")  # writes pictures, returns paths
for w in doc.warnings:
    print("warning:", w)
```

Errors are subclasses of `hwp_reader.HwpReaderError`: `UnsupportedFormatError`, `EncryptedDocumentError` and `CorruptDocumentError`. No other exception type escapes `read_document` for malformed input.

## Supported features

| Area | HWP 5.x | HWPX |
| --- | --- | --- |
| Body text, tabs, line breaks, special spaces | yes | yes |
| Multiple sections in document order | yes | yes (spine order) |
| Tables, merged cells, nested tables, captions | yes | yes |
| Text boxes, drawing objects with text, groups | yes | yes |
| Footnotes, endnotes | yes | yes |
| Page headers and footers | yes | yes |
| Master pages (바탕쪽) | yes | yes |
| Hyperlinks | yes | yes |
| Fill-in fields (누름틀) guide text | yes | yes |
| Form check boxes / radio buttons | – | yes |
| Track changes, final text | yes | yes (per paragraph) |
| Equations (script) | yes | yes |
| Pictures (reference + extraction) | yes | yes |
| Outline, numbered and bulleted paragraph labels | yes | yes |
| Hidden comments, memos | yes | yes |
| 덧말, 글자 겹침 | yes | yes |
| Distribution documents (배포용) | yes (AES-128, pure Python) | n/a |
| Password-protected | detected, not readable | detected, not readable |
| Metadata | summary information | content.hpf |
| Preview text fallback | `PrvText` | `Preview/PrvText.txt` |
| Malformed XML | n/a | text-only recovery with a warning |

Automatic paragraph numbers are recomputed from the numbering definitions in document order: each numbering list keeps its own counters, counting continues across sections, and plain or bulleted paragraphs don't interrupt a list. In unusual cases a label can differ from what Hangul displays.

## Troubleshooting

- **The chat client refuses `.hwp`/`.hwpx` attachments.** Many Microsoft 365 surfaces only accept common Office, PDF and text formats. Ask the user to zip the file and attach the `.zip`, or, if zip is also refused, to rename the file to an accepted extension. The reader detects the real format from the content.
- **The file can't be found.** List candidate files with `find` (see SKILL.md). The attachment may have been renamed, so check recently modified files: `find . /mnt/data /tmp -type f -mmin -60`.
- **Garbled Korean on screen.** Write to a file with `-o`, which is always UTF-8, and read the file.
- **Very large documents.** Parsing is fast (well under a second for typical documents). Distribution documents need AES decryption in pure Python, at about 1 MB/s. Use `-o` and page through with `--offset`/`--max-chars`.
- **Text seems incomplete.** Check `--info` warnings. A warning that mentions `PrvText` or 미리보기 means the body could not be parsed and only the first part of the document (the preview) is available.
- **Old documents.** HWP 3.x (한글 97) and HWPML must be re-saved in 한글 as `.hwpx` or `.hwp`.

## Security

The reader never executes document content and makes no network calls. HWPX XML parts must be UTF-8, and XML with DTDs or entity declarations is rejected. Every document has a budget: 256 MB of decompressed data (128 MB per stream, 64 MB per XML part), 1.5 million HWP records or 1 million HWPX XML elements (on reaching a limit the document is read up to that point, with a warning), and zip entries with a compression ratio above 1000:1 are refused. OLE sector chains are loop-checked and bounded by the file size, streams that alias each other are read once, table sizes and cell spans are clamped, number labels stay short, and object nesting is limited (about 40 levels of nested HWPX tables, 128 HWP levels; deeper content is cut with a warning). Malformed input only ever raises the reader's own error types.
