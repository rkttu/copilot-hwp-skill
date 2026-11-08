#!/usr/bin/env python3
# Copyright 2026 rkttu@rkttu.com (남정현)
# SPDX-License-Identifier: Apache-2.0
"""Re-create the HWP/HWPX test corpus from tests/corpus-manifest.json.

The corpus consists of third-party sample documents from public open-source
repositories.  They are not redistributed here; the manifest only records
their pinned URLs, licenses and hashes.  Files land in .corpus/ (git-ignored).

Standard library only. Every file listed in the manifest is downloaded from its
pinned ``source_url`` (raw.githubusercontent.com at a fixed commit) and checked
against the recorded ``size`` and ``sha256`` before it is moved into place.
Files that already exist with the right hash are left alone.

The downloaded documents are untrusted test data: this script only writes bytes
to disk and never opens, parses or executes them.

Usage examples::

    python tools/fetch_corpus.py                 # fetch missing / mismatched files
    python tools/fetch_corpus.py --verify-only   # just check what is on disk
    python tools/fetch_corpus.py --only "hwpx/*" --jobs 4
    python tools/fetch_corpus.py --manifest path/to/manifest.json --root path/to/corpus

Exit status: 0 when every selected file is present and verified, 1 otherwise.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ROOT = os.path.join(REPO_ROOT, ".corpus")
DEFAULT_MANIFEST = os.path.join(REPO_ROOT, "tests", "corpus-manifest.json")
USER_AGENT = "copilot-hwp-skill-corpus-fetch/1.0 (+python-urllib)"
ALLOWED_SCHEMES = ("https",)
MAX_BYTES = 64 * 1024 * 1024  # hard cap per file, well above the 15 MB corpus limit


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_dest(root: str, rel: str) -> str:
    """Resolve a manifest-relative path, refusing anything that escapes root."""
    if not rel or os.path.isabs(rel) or rel.startswith(("/", "\\")):
        raise ValueError(f"refusing absolute/empty path: {rel!r}")
    parts = rel.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"refusing suspicious path: {rel!r}")
    dest = os.path.realpath(os.path.join(root, *parts))
    root_real = os.path.realpath(root)
    if os.path.commonpath([dest, root_real]) != root_real:
        raise ValueError(f"path escapes corpus root: {rel!r}")
    return dest


def download(url: str, expected_size: int | None, timeout: float) -> bytes:
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"refusing non-https URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        limit = MAX_BYTES if expected_size is None else min(MAX_BYTES, expected_size + 1)
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"response larger than expected ({len(data)} > {limit} bytes)")
    return data


def fetch_one(entry: dict, root: str, args) -> tuple[str, str, str]:
    rel = entry["file"]
    try:
        dest = safe_dest(root, rel)
    except ValueError as ex:
        return rel, "ERROR", str(ex)
    want = entry["sha256"].lower()
    size = entry.get("size")

    if os.path.isfile(dest) and not args.force:
        got = sha256_file(dest)
        if got == want:
            return rel, "ok", "already present"
        if args.verify_only:
            return rel, "MISMATCH", f"sha256 {got} != {want}"
    elif args.verify_only:
        return rel, "MISSING", ""

    last = ""
    for attempt in range(1, args.retries + 1):
        try:
            data = download(entry["source_url"], size, args.timeout)
            if size is not None and len(data) != size:
                raise ValueError(f"size {len(data)} != expected {size}")
            got = hashlib.sha256(data).hexdigest()
            if got != want:
                raise ValueError(f"sha256 {got} != expected {want}")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".part-", dir=os.path.dirname(dest))
            try:
                with os.fdopen(fd, "wb") as fp:
                    fp.write(data)
                os.replace(tmp, dest)
            except BaseException:
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise
            return rel, "ok", "downloaded"
        except (urllib.error.URLError, OSError, ValueError) as ex:
            last = f"{type(ex).__name__}: {ex}"
            # integrity failures will not fix themselves; network errors might
            if isinstance(ex, ValueError) and "sha256" in str(ex):
                break
            if attempt < args.retries:
                time.sleep(min(2 ** attempt, 15))
    return rel, "FAILED", last


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT, help="corpus directory (default: %(default)s)")
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST, help="manifest path (default: %(default)s)")
    ap.add_argument("--only", action="append", default=[], metavar="GLOB",
                    help="only entries whose 'file' matches this glob (repeatable), e.g. 'hwp/*' or '*password*'")
    ap.add_argument("--verify-only", action="store_true", help="do not download; just verify files on disk")
    ap.add_argument("--force", action="store_true", help="re-download even if the local file verifies")
    ap.add_argument("--jobs", type=int, default=6, help="parallel downloads (default: %(default)s)")
    ap.add_argument("--retries", type=int, default=3, help="attempts per file (default: %(default)s)")
    ap.add_argument("--timeout", type=float, default=120.0, help="per-request timeout in seconds")
    ap.add_argument("-q", "--quiet", action="store_true", help="only print problems and the summary")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    manifest_path = os.path.abspath(args.manifest)
    try:
        with open(manifest_path, encoding="utf-8") as fp:
            manifest = json.load(fp)
    except (OSError, ValueError) as ex:
        print(f"cannot read manifest {manifest_path}: {ex}", file=sys.stderr)
        return 1
    if not isinstance(manifest, list):
        print("manifest must be a JSON array", file=sys.stderr)
        return 1

    entries = [e for e in manifest
               if not args.only or any(fnmatch.fnmatch(e.get("file", ""), g) for g in args.only)]
    for e in entries:
        for key in ("file", "source_url", "sha256"):
            if not e.get(key):
                print(f"manifest entry missing {key!r}: {e!r}"[:300], file=sys.stderr)
                return 1

    os.makedirs(root, exist_ok=True)
    counts: dict[str, int] = {}
    problems = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futs = [pool.submit(fetch_one, e, root, args) for e in entries]
        for fut in concurrent.futures.as_completed(futs):
            rel, status, detail = fut.result()
            counts[status] = counts.get(status, 0) + 1
            if status != "ok":
                problems.append((rel, status, detail))
                print(f"[{status}] {rel} {detail}", file=sys.stderr)
            elif not args.quiet and detail == "downloaded":
                print(f"[ok] {rel}")

    total = len(entries)
    print(f"{total} entries: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
