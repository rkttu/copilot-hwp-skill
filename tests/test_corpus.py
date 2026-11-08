# -*- coding: utf-8 -*-
# Copyright 2026 rkttu@rkttu.com (남정현)
# SPDX-License-Identifier: Apache-2.0
"""Regression test over the real-document corpus (skipped when it is absent).

Download the corpus first (third-party samples, not redistributed here):
    python3 tools/fetch_corpus.py
Then:
    python3 -m unittest tests.test_corpus -v
"""
import json
import os
import re
import sys
import traceback
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORPUS = os.path.join(ROOT, ".corpus")
MANIFEST = os.path.join(HERE, "corpus-manifest.json")
sys.path.insert(0, os.path.join(ROOT, "hwp-reader", "scripts"))

import hwp_reader as hr  # noqa: E402

# deliberately broken inputs in the corpus
EXPECTED_ERRORS = {
    "hwpx/hwpforge__corrupt-input.hwpx": hr.CorruptDocumentError,
}
# generated test files whose stored preview text does not match their body
STALE_PREVIEW = {"hwpx/hwpforge__depth-group-32.hwpx"}


def _norm(s):
    return re.sub(r"[\s<>]+", "", s or "")


def preview_coverage(doc, text):
    """Share of the Hangul-written preview text (PrvText) found in ``text``.

    The preview is an independent reference produced by Hangul itself; it
    holds roughly the first 1 KB of the document's text.
    """
    prv = _norm(doc.preview_text)
    if len(prv) < 20:
        return None
    have = {}
    for ch in _norm(text):
        have[ch] = have.get(ch, 0) + 1
    hit = 0
    for ch in prv:
        if have.get(ch, 0) > 0:
            have[ch] -= 1
            hit += 1
    return hit / float(len(prv))


def _load_manifest():
    with open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)


@unittest.skipUnless(os.path.isdir(os.path.join(CORPUS, "hwp")),
                     "corpus not downloaded (run: python3 tools/fetch_corpus.py)")
class CorpusTests(unittest.TestCase):
    def test_every_document(self):
        failures = []
        checked = 0
        for entry in _load_manifest():
            rel = entry["file"]
            path = os.path.join(CORPUS, rel)
            if not os.path.isfile(path):
                continue
            checked += 1
            feats = set(entry.get("verified_features", [])) | set(entry.get("expected_features", []))
            try:
                doc = hr.read_document(path)
                md = doc.to_markdown()
                txt = doc.to_text()
                json.dumps(doc.to_dict(), ensure_ascii=False)
                doc.info()
            except hr.HwpReaderError as e:
                ok = (isinstance(e, hr.UnsupportedFormatError) and "hwp3_format" in feats) or \
                     (isinstance(e, hr.EncryptedDocumentError) and "password_protected" in feats) or \
                     isinstance(e, EXPECTED_ERRORS.get(rel, ()))
                if not ok:
                    failures.append("%s: unexpected %s: %s" % (rel, type(e).__name__, e))
                continue
            except Exception:
                failures.append("%s: CRASH\n%s" % (rel, traceback.format_exc()))
                continue
            if rel in EXPECTED_ERRORS:
                failures.append("%s: expected %s" % (rel, EXPECTED_ERRORS[rel].__name__))
            if "hwp3_format" in feats or "password_protected" in feats:
                failures.append("%s: should have been rejected" % rel)
            if "distribution_protected" in feats and not doc.flags.get("distribution"):
                failures.append("%s: distribution flag not detected" % rel)
            if not _norm(txt) and _norm(doc.preview_text) and rel not in STALE_PREVIEW:
                failures.append("%s: no text extracted, but the preview has text" % rel)
            cov = preview_coverage(doc, txt)
            if cov is not None and cov < 0.9 and rel not in STALE_PREVIEW:
                failures.append("%s: only %.0f%% of the preview text was extracted" % (rel, cov * 100))
            bad = re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", md)
            if bad:
                failures.append("%s: control character %r in output" % (rel, bad.group()))
        self.assertGreater(checked, 0)
        self.assertEqual(failures, [], "\n".join(failures))

    def test_distribution_documents_decrypt(self):
        for entry in _load_manifest():
            if "distribution_protected" not in entry.get("verified_features", []):
                continue
            path = os.path.join(CORPUS, entry["file"])
            if not os.path.isfile(path):
                continue
            doc = hr.read_document(path)
            self.assertTrue(doc.flags["distribution"], entry["file"])
            self.assertFalse(any("미리보기" in w or "PrvText" in w for w in doc.warnings),
                             "%s fell back to preview text" % entry["file"])
            text = doc.to_text()
            self.assertTrue(_norm(text), entry["file"])
            if _norm(doc.preview_text):
                # auto-numbering labels are not part of the preview, so compare
                # character coverage rather than an exact substring
                prv = _norm(doc.preview_text)
                cov = preview_coverage(doc, text) if len(prv) >= 20 else \
                    (1.0 if prv in _norm(text) else 0.0)
                self.assertGreaterEqual(cov, 0.95, entry["file"])


if __name__ == "__main__":
    unittest.main()
