#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 rkttu@rkttu.com (남정현)
# SPDX-License-Identifier: Apache-2.0
"""Validate the skill folder against Microsoft 365 Copilot / Copilot Studio
packaging rules and build a deterministic upload zip (SKILL.md at zip root).

    python3 tools/build_skill.py --check                 # validate only
    python3 tools/build_skill.py --out dist              # validate + build dist/hwp-reader.zip
    python3 tools/build_skill.py --expect-version 0.1.0  # also check the script version

Standard library only.
"""
import argparse
import ast
import hashlib
import os
import py_compile
import re
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(ROOT, "hwp-reader")
LICENSE_SRC = os.path.join(ROOT, "LICENSE")
NOTICE_SRC = os.path.join(ROOT, "NOTICE")

# Microsoft 365 Copilot declarative-agent skill allowlist (resources + scripts)
ALLOWED_EXT = {
    ".json", ".xml", ".yaml", ".yml", ".ini", ".config", ".utf8", ".docx", ".doc", ".docm",
    ".pdf", ".txt", ".rtf", ".md", ".ppt", ".pptx", ".ppsm", ".xlsx", ".xls", ".xlsm", ".csv",
    ".tsv", ".html", ".htm", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".log",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".mts", ".sh", ".bash",
}
MAX_FILES = 100                 # strictest: Cowork upload (M365 DA allows 350)
MAX_FILE_BYTES = 5 * 1024 * 1024        # strictest: Cowork plugin companion file
MAX_ZIP_BYTES = 10 * 1024 * 1024        # strictest: Cowork / Agents Toolkit package
MAX_DEPTH = 3
MAX_SKILL_MD_CHARS = 20000      # M365 declarative agents
MAX_SKILL_MD_LINES = 500        # agentskills.io recommendation
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
STDLIB = {"argparse", "datetime", "html", "io", "json", "os", "re", "struct", "sys",
          "zipfile", "zlib", "xml"}
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


class CheckError(Exception):
    pass


def parse_frontmatter(text):
    if not text.startswith("---\n"):
        raise CheckError("SKILL.md must start with a '---' YAML frontmatter line (LF line endings)")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise CheckError("SKILL.md frontmatter is not closed with '---'")
    fields = {}
    for line in text[4:end].split("\n"):
        if not line.strip():
            continue
        m = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", line)
        if not m:
            raise CheckError("unsupported frontmatter line (keep each field on one line): %r" % line)
        key, value = m.group(1), m.group(2).strip()
        if value.startswith('"'):
            if not value.endswith('"') or len(value) < 2:
                raise CheckError("unterminated quoted value for %s" % key)
            inner = value[1:-1]
            if re.search(r'(?<!\\)"', inner):
                raise CheckError("unescaped double quote inside %s" % key)
            # only escapes that strict YAML accepts inside double quotes
            if re.search(r'\\(?![0abtnvfre "/\\N_LP]|x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8})',
                         inner):
                raise CheckError("invalid escape sequence inside %s" % key)
            value = inner.replace('\\"', '"').replace("\\\\", "\\")
        elif key == "description":
            raise CheckError("write description as a double-quoted string (strict YAML parsers "
                             "reject or truncate many plain values)")
        elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
            raise CheckError("quote the %s value (it contains YAML metacharacters)" % key)
        fields[key] = value
    return fields, text[end + 5:]


def check_skill(skill_dir, expect_version=None):
    problems = []

    def bad(msg):
        problems.append(msg)

    md_path = os.path.join(skill_dir, "SKILL.md")
    with open(md_path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        bad("SKILL.md must be UTF-8 without BOM")
    if b"\r\n" in raw:
        bad("SKILL.md must use LF line endings")
    text = raw.decode("utf-8")
    try:
        fields, body = parse_frontmatter(text.replace("\r\n", "\n"))
    except CheckError as e:
        raise CheckError(str(e))
    unknown = set(fields) - {"name", "description"}
    if unknown:
        bad("only 'name' and 'description' are documented for Microsoft hosts; found %s"
            % sorted(unknown))
    name = fields.get("name", "")
    desc = fields.get("description", "")
    folder = os.path.basename(os.path.normpath(skill_dir))
    if not (1 <= len(name) <= 64 and NAME_RE.match(name)):
        bad("name must be 1-64 chars of lowercase letters, digits and single hyphens")
    if name != folder:
        bad("name %r must equal the folder name %r" % (name, folder))
    if not 1 <= len(desc) <= 1024:
        bad("description must be 1-1024 characters (now %d)" % len(desc))
    if "\n" in desc:
        bad("description must be a single line")
    if len(text) > MAX_SKILL_MD_CHARS:
        bad("SKILL.md exceeds %d characters" % MAX_SKILL_MD_CHARS)
    if text.count("\n") > MAX_SKILL_MD_LINES:
        bad("SKILL.md exceeds %d lines" % MAX_SKILL_MD_LINES)
    for ref in sorted(set(re.findall(r"`((?:scripts|references)/[^`\s]+)`", body))):
        if not os.path.exists(os.path.join(skill_dir, ref)):
            bad("SKILL.md references missing file %s" % ref)

    files = []
    for dirpath, dirnames, filenames in os.walk(skill_dir):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for d in dirnames:
            if d.startswith("."):
                bad("hidden directory not allowed: %s" % os.path.join(dirpath, d))
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, skill_dir).replace(os.sep, "/")
            if fn.endswith(".pyc"):
                continue
            files.append(rel)
            ext = os.path.splitext(fn)[1].lower()
            if fn.startswith("."):
                bad("hidden file not allowed: %s" % rel)
            if ext not in ALLOWED_EXT:
                bad("file type not allowed by Microsoft 365 skill uploads: %s" % rel)
            if not all(SAFE_NAME.match(part) for part in rel.split("/")):
                bad("use ASCII letters, digits, '-', '_' and '.' in file names: %s" % rel)
            if rel.count("/") + 1 > MAX_DEPTH:
                bad("directory depth exceeds %d: %s" % (MAX_DEPTH, rel))
            if os.path.getsize(full) > MAX_FILE_BYTES:
                bad("file larger than %d bytes: %s" % (MAX_FILE_BYTES, rel))
            if ext in (".md", ".py", ".txt"):
                with open(full, "rb") as f:
                    data = f.read()
                if data.startswith(b"\xef\xbb\xbf"):
                    bad("UTF-8 BOM not allowed: %s" % rel)
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError:
                    bad("not valid UTF-8: %s" % rel)
    if len(files) > MAX_FILES:
        bad("too many files (%d > %d)" % (len(files), MAX_FILES))

    script = os.path.join(skill_dir, "scripts", "hwp_reader.py")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            py_compile.compile(script, cfile=os.path.join(tmp, "x.pyc"), doraise=True)
        except py_compile.PyCompileError as e:
            bad("script does not compile: %s" % e)
    with open(script, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("eval", "exec"):
            bad("eval/exec are not allowed in bundled scripts")
    if not mods <= STDLIB:
        bad("non-allowlisted imports: %s" % sorted(mods - STDLIB))
    m = re.search(r'^__version__ = "([^"]+)"', src, re.M)
    version = m.group(1) if m else None
    if expect_version and version != expect_version:
        bad("script __version__ %r does not match expected %r" % (version, expect_version))
    if problems:
        raise CheckError("\n".join("- " + p for p in problems))
    return name, version, sorted(files)


def build_zip(skill_dir, files, out_path):
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    entries = [(rel, os.path.join(skill_dir, rel)) for rel in files]
    if "LICENSE.txt" not in files and os.path.exists(LICENSE_SRC):
        entries.append(("LICENSE.txt", LICENSE_SRC))
    if "NOTICE.txt" not in files and os.path.exists(NOTICE_SRC):
        entries.append(("NOTICE.txt", NOTICE_SRC))
    entries.sort(key=lambda e: (e[0] != "SKILL.md", e[0]))
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel, full in entries:
            with open(full, "rb") as f:
                data = f.read()
            if rel.endswith((".md", ".py", ".txt")):
                data = data.replace(b"\r\n", b"\n")
            info = zipfile.ZipInfo(rel, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if rel.endswith(".py") else 0o644) << 16
            info.create_system = 3
            z.writestr(info, data)
    size = os.path.getsize(out_path)
    if size > MAX_ZIP_BYTES:
        raise CheckError("zip is %d bytes, larger than %d" % (size, MAX_ZIP_BYTES))
    with open(out_path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    return size, digest, [e[0] for e in entries]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--skill-dir", default=SKILL_DIR)
    ap.add_argument("--check", action="store_true", help="validate only, do not build")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"), help="output directory")
    ap.add_argument("--expect-version", help="fail unless the script __version__ equals this")
    args = ap.parse_args(argv)
    try:
        name, version, files = check_skill(args.skill_dir, args.expect_version)
    except CheckError as e:
        print("skill check FAILED:\n%s" % e, file=sys.stderr)
        return 1
    print("skill check OK: %s %s (%d files)" % (name, version, len(files)))
    if args.check:
        return 0
    out_path = os.path.join(args.out, name + ".zip")
    try:
        size, digest, entries = build_zip(args.skill_dir, files, out_path)
    except CheckError as e:
        print("build FAILED: %s" % e, file=sys.stderr)
        return 1
    with open(out_path + ".sha256", "w", encoding="ascii", newline="\n") as f:
        f.write("%s  %s\n" % (digest, os.path.basename(out_path)))
    print("built %s (%d bytes, sha256 %s)" % (out_path, size, digest))
    for e in entries:
        print("  " + e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
