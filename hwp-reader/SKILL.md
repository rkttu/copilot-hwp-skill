---
name: hwp-reader
description: "Read Korean Hangul word-processor documents (.hwp HWP 5.x and .hwpx OWPML, including 배포용 distribution-protected files) and convert them to Markdown, plain text or JSON with tables, footnotes, headers and image references. Use when the user attaches or mentions a 한글/한컴/아래아한글 document (.hwp, .hwpx, or a .zip of them) and asks to read, summarize, translate, search, compare, extract tables from, or convert it. Works offline with the Python standard library only."
---

# HWP / HWPX reader

Use the bundled script `scripts/hwp_reader.py` to turn a Hangul (한글) document into text you can reason about. It needs only Python 3.8+ and its standard library: no network, no packages. Do not try to `pip install` HWP libraries (pyhwp, olefile, hwp5...). They are unavailable in the sandbox and not needed.

Always answer the user in their language (usually Korean).

## 1. Locate the script and the document

The working directory is not guaranteed to be this skill's folder, and each shell call may start fresh, so resolve absolute paths first and reuse them literally in later commands.

```bash
ls /app/skills/hwp-reader/scripts/hwp_reader.py scripts/hwp_reader.py 2>/dev/null
# if neither exists:
find / -name hwp_reader.py -path '*hwp-reader*' 2>/dev/null | head -1
```

Find the user's file. Attachments usually land in the working directory, `/mnt/data`, or `/tmp`:

```bash
find . /mnt/data /mnt/user-data /tmp "$HOME" -maxdepth 4 -type f \( -iname '*.hwp' -o -iname '*.hwpx' -o -iname '*.zip' \) 2>/dev/null | head -50
```

The reader detects the format from the file content, not the extension. A renamed file (`문서.bin`, `upload`), a `.zip` that contains `.hwp`/`.hwpx` files, or a directory of documents works as is. Quote paths, because Korean file names and spaces are common.

## 2. Read the document

Below, `READER` stands for the absolute script path you found, for example `/app/skills/hwp-reader/scripts/hwp_reader.py`.

1. Inspect first. This prints JSON with format, version, flags, metadata (title, author, dates), statistics, a short preview and warnings. In the statistics, `characters` is the amount of document text and `markdown_chars` the length of the default Markdown output.

   ```bash
   python3 READER "/path/문서.hwp" --info
   ```

2. Small document (`markdown_chars` under about 12000): print Markdown directly.

   ```bash
   python3 READER "/path/문서.hwp"
   ```

3. Large document: read it in pages. Tool output is truncated at roughly 10-30K characters, so never rely on one huge print.

   ```bash
   python3 READER "/path/문서.hwp" --max-chars 15000             # page 1
   python3 READER "/path/문서.hwp" --max-chars 15000 --offset N  # next page
   ```

   Every page except the last ends with a notice like `... 이어서 읽으려면 --offset 14873 옵션을 쓰세요 / continue with --offset 14873`. Use exactly that number as `N` for the next page; pages end at paragraph boundaries where possible, so it is usually not a multiple of 15000. A table or line longer than a page is split at a row end or a space. Read every page before you summarize or answer questions about the whole document. You can also save everything with `-o /tmp/doc.md` and read that file in parts.

Useful options:

| Option | Effect |
| --- | --- |
| `-f markdown` (default), `-f text`, `-f json` | Output format. JSON can't be paged; save it with `-o` |
| `-o PATH` | Write UTF-8 output to a file. If the input expands to several documents (several files, a `.zip` or a directory), or `PATH` is an existing directory or ends with `/`, one file per document goes into the directory `PATH`, and each written path is printed to stderr |
| `--info` | Metadata and statistics only |
| `--max-chars N`, `--offset N` | Page through long output, one document at a time |
| `--tables auto\|markdown\|html\|text` | Table rendering (default `auto`) |
| `--extract-images DIR` | Save embedded pictures and link them from the output (one sub-folder per document when there are several) |
| `--no-headers-footers`, `--no-notes`, `--no-comments` | Omit page headers/footers and master pages, foot/endnotes, memos/hidden comments |

## 3. How to read the output

- `#` headings are outline (개요) paragraphs. Automatic numbering such as `1.`, `가.`, `1)` or `①` is reproduced as plain text in front of the paragraph.
- Simple tables are Markdown tables. Tables with merged or nested cells are HTML `<table>` with `rowspan`/`colspan`, so the structure is exact. In `-f text` (and `--tables markdown|text`) a short label merged vertically (a row heading spanning several rows) is repeated in each row; other merged cells appear once, with empty slots after them. One-cell tables used as text boxes, and short one-row "title" tables, are shown as ordinary lines. A table with no text at all appears as `[빈 표 3×4]`.
- `[^1]` marks a footnote and `[^e1]` an endnote. Their text is listed at the end of the output.
- `[그림: name]` is a picture, `[수식: ...]` an equation in HWP equation syntax (not LaTeX; `a over b` means a/b, `sqrt x` means √x), and `[OLE 개체]`, `[차트]`, `[동영상]` are embedded objects whose content is not text.
- `[입력란: 안내문]` is an empty fill-in field (누름틀) showing its guide text, i.e. a blank the form expects someone to fill. `☐`/`☑` and `○`/`◉` are unchecked/checked form check boxes and radio buttons.
- After a final `---` line come the page headers and footers (`머리말`, `꼬리말`), master-page text (`바탕쪽`), and memos or hidden comments (`메모 ("anchored text"): ...`, `숨은 설명`). These belong to the page layout or to reviewers, not to the body flow.
- JSON (`-f json`): `sections[].blocks[]` holds `paragraph`, `table` and `textbox` blocks. A table has `cells` (row, col, row_span, col_span, text) and `grid`, a row-by-column matrix in which `null` marks a slot covered by a merged cell. Use JSON when you need exact table data, for example to build an Excel file.

## 4. Errors and what to tell the user

Messages go to stderr in Korean and English, and the exit code tells you the cause.

| Exit | Meaning | What to do |
| --- | --- | --- |
| 0 | Success. Warnings may still be printed | If a warning says the preview text (미리보기) was used, tell the user the extraction is partial |
| 3 | Not HWP 5.x/HWPX: HWP 3.x (한글 97), HWPML (.hml), or another format | Ask the user to open the file in 한글 and save it as `.hwpx` or `.hwp` (한글 2010 or later) |
| 4 | Password-protected or DRM/certificate-encrypted | Content cannot be read. Ask for a copy without the password or DRM (`보안 문서` 해제) |
| 5 | Damaged or truncated file | Ask the user to re-save or re-upload. Other files in the same command are still processed |
| 1 | File not found or unexpected error | Check the path, then report the stderr message |

Distribution-protected documents (배포용 문서) are decrypted automatically. You don't need a password for them.

## 5. Common tasks

- Summarize, translate or answer questions: read all pages, then answer. Mention section headings or table captions so the user can find the source.
- Search: write the text to a file (`-f text -o /tmp/doc.txt`) and use `grep -n`.
- Tables to Excel/CSV: run `-f json -o /tmp/doc.json`, load it with Python, and turn each table's `grid` into rows (fill `null` with the merged cell's value if the user wants flat data). Use openpyxl or pandas if the sandbox has them, otherwise the `csv` module.
- Convert to another format: deliver the Markdown or text file (`-o`), or build a .docx with python-docx if it's available. Save files where the platform offers downloads (for example `/mnt/data`).
- Several documents: pass several files, a directory or one `.zip`. Each output starts with `===== file =====`, or use `-o DIR` to get one output file per document. Page with `--max-chars`/`--offset` one document at a time.
- Forms: look for `[입력란: ...]` blanks, `☐` check boxes and empty table cells to see what still needs to be filled in.
- Images: `--extract-images /tmp/images` saves the pictures, and the Markdown links to them.

## 6. Limits

- Text and structure only. Page layout, fonts, colors and page numbers are not reproduced.
- Charts, OLE objects and videos appear only as placeholders. Equations stay in HWP equation syntax.
- Documents with track changes (변경 추적) are read as their final text. Rarely, an HWPX deletion that spans several paragraphs is kept.
- HWP 3.x and older, HWPML (.hml) and password or DRM documents can't be read.

For the full CLI, JSON schema, Python API and troubleshooting, see `references/REFERENCE.md`.
