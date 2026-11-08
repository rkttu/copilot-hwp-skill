# -*- coding: utf-8 -*-
# Copyright 2026 rkttu@rkttu.com (남정현)
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for issues found by the multi-lens review (stdlib only)."""
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT_DIR = os.path.normpath(os.path.join(HERE, "..", "hwp-reader", "scripts"))
SCRIPT = os.path.join(SCRIPT_DIR, "hwp_reader.py")
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, HERE)

import hwp_reader as hr  # noqa: E402
import builders as B     # noqa: E402
from test_hwp_reader import sample_docinfo, sample_hwp, sample_hwpx, secd_ctrl  # noqa: E402

P, R, T = B.hp_p, B.hp_run, B.hp_t


def hwp(section, docinfo=None, **kw):
    return hr.read_document(B.build_hwp(docinfo or sample_docinfo(), [section], **kw))


def hwpx(section_xml, **kw):
    return hr.read_document(B.build_hwpx([section_xml], **kw))


def tc(col, row, text, cs=1, rs=1):
    return ('<hp:tc><hp:subList>%s</hp:subList><hp:cellAddr colAddr="%d" rowAddr="%d"/>'
            '<hp:cellSpan colSpan="%d" rowSpan="%d"/></hp:tc>' % (P(R(T(text))) if text else "",
                                                                 col, row, cs, rs))


class HwpContentTests(unittest.TestCase):
    def test_form_controls(self):
        def form(level, kind, params):
            out = B.rec(0x47, level, struct.pack("<I", B.cid("form")) + b"\0" * 42)
            out += B.rec(0x5B, level + 1, kind + kind + struct.pack("<I", 0) + B.wstr(params))
            return out
        radio = form(1, b"tbr+", "Name:wstring:3:opt Caption:wstring:2:신문 Value:int:1 ")
        check = form(1, b"tbc+", "Caption:wstring:3:회사원 Value:int:0 ")
        button = form(1, b"tbp+", "Caption:wstring:2:확인 ")
        s = B.paragraph(0, [("ext", 11, "form"), " ", ("ext", 11, "form"), " ", ("ext", 11, "form")],
                        [radio, check, button])
        md = hwp(s).to_markdown()
        self.assertIn("◉ 신문", md)
        self.assertIn("☐ 회사원", md)
        self.assertIn("[확인]", md)

    def test_textart(self):
        ctrl = B.rec(0x47, 1, struct.pack("<I", B.cid("gso ")) + b"\0" * 42)
        ctrl += B.rec(0x4C, 2, b"\0" * 40)
        ctrl += B.rec(0x5A, 3, b"\0" * 32 + B.wstr("글맵시 문구") + B.wstr("굴림") + b"\0" * 8)
        md = hwp(B.paragraph(0, [("ext", 11, "gso ")], [ctrl])).to_markdown()
        self.assertIn("글맵시 문구", md)

    def test_equation_caption(self):
        ctrl = B.rec(0x47, 1, struct.pack("<I", B.cid("eqed")) + b"\0" * 54)
        ctrl += B.rec(0x48, 2, struct.pack("<HHI", 1, 0, 0) + b"\0" * 22)
        ctrl += B.paragraph(2, ["수식 캡션 A"])
        ctrl += B.rec(0x58, 2, struct.pack("<I", 0) + B.wstr("1 over 4") + b"\0" * 12)
        md = hwp(B.paragraph(0, [("ext", 11, "eqed")], [ctrl])).to_markdown()
        self.assertIn("[수식: 1 over 4]", md)
        self.assertIn("수식 캡션 A", md)

    def test_label_kept_when_paragraph_starts_with_block(self):
        s = B.paragraph(0, [("ext", 2, "secd"), "머리"], [secd_ctrl(1)])
        s += B.paragraph(0, ["첫째"], shape_id=1)
        s += B.paragraph(0, [("ext", 11, "gso "), "둘째"], [B.textbox_ctrl(1, [["상자 안"]])], shape_id=1)
        doc = hwp(s)
        md = doc.to_markdown()
        self.assertIn("# 1. 첫째", md)
        self.assertIn("# 2. 둘째", md)
        self.assertIn("2. 둘째", doc.to_text())

    def test_unusable_links_become_text(self):
        t = hr._hyperlink_target
        self.assertIsNone(t("javascript:;;0;0;0;"))
        self.assertIsNone(t("?#645989673;0;1;0;"))
        self.assertEqual(t("http\\://x.com;1;0;0;"), "http://x.com")
        s = B.paragraph(0, ["목차 ", ("ext", 3, "%hlk"), "1장", ("inline", 4)],
                        [B.hyperlink_ctrl(1, "?#12345")])
        md = hwp(s).to_markdown()
        self.assertIn("목차 1장", md)
        self.assertNotIn("](", md)

    def test_trailing_master_page_list(self):
        s = B.paragraph(0, ["본문"])
        s += B.rec(0x48, 1, struct.pack("<HHI", 1, 0, 0) + b"\0" * 26)
        s += B.paragraph(1, ["바탕 문구"])
        md = hwp(s).to_markdown()
        self.assertIn("바탕쪽: 바탕 문구", md)
        self.assertNotIn("본문\n\n바탕 문구", md)

    def test_pua_symbols_mapped(self):
        md = hwp(B.paragraph(0, [" 동의  항목 글"])).to_markdown()
        self.assertIn("☑ 동의 • 항목 ᄒᆞᆫ글", md)

    def test_discretionary_hyphen_invisible(self):
        md = hwp(B.paragraph(0, ["Word", ("char", 24), "processor"])).to_markdown()
        self.assertIn("Wordprocessor", md)
        md2 = hwpx(P(R("<hp:t>Word<hp:hypen/>processor</hp:t>"))).to_markdown()
        self.assertIn("Wordprocessor", md2)

    def test_summary_noise_filtered(self):
        summary = B.summary_info({2: "제목", 5: "\r\n", 21: "정체불명"})
        doc = hr.read_document(B.build_hwp(sample_docinfo(), [B.paragraph(0, ["x"])], summary=summary))
        self.assertEqual(doc.metadata, {"title": "제목"})


class HwpxContentTests(unittest.TestCase):
    def test_metadata_placeholder_dropped(self):
        src = zipfile.ZipFile(io.BytesIO(B.build_hwpx([P(R(T("x")))], creator="홍길동")))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for info in src.infolist():
                data = src.read(info)
                if info.filename == "Contents/content.hpf":
                    data = data.replace(b"</opf:metadata>",
                                        b'<opf:meta name="subject" content="text"/></opf:metadata>')
                z.writestr(info, data)
        doc = hr.read_document(buf.getvalue())
        self.assertNotIn("subject", doc.metadata)
        self.assertEqual(doc.metadata.get("author"), "홍길동")

    def test_clickhere_guide_as_content(self):
        field = ('<hp:ctrl><hp:fieldBegin id="7" type="CLICK_HERE"><hp:parameters>'
                 '<hp:stringParam name="Direction">성명</hp:stringParam></hp:parameters>'
                 '</hp:fieldBegin></hp:ctrl>')
        end = '<hp:ctrl><hp:fieldEnd beginIDRef="7"/></hp:ctrl>'
        md = hwpx(P(R(field) + R(T("성명")) + R(end)) + P(R(field) + R(T("홍길동")) + R(end))).to_markdown()
        self.assertIn("[입력란: 성명]", md)
        self.assertIn("홍길동", md)

    def test_form_values(self):
        x = P(R('<hp:edit name="e"><hp:text>입력값</hp:text></hp:edit>'
                '<hp:edit name="pw" passwordChar="*"><hp:text>1234</hp:text></hp:edit>'
                '<hp:comboBox selectedValue="V2"><hp:listItem displayText="One" value="V1"/>'
                '<hp:listItem displayText="Two" value="V2"/></hp:comboBox>'))
        md = hwpx(x).to_markdown()
        self.assertIn("입력값", md)
        self.assertIn("Two", md)
        self.assertNotIn("1234", md)

    def test_header_in_secpr(self):
        x = P(R('<hp:secPr id=""><hp:header id="1" applyPageType="BOTH"><hp:subList>%s'
                '</hp:subList></hp:header></hp:secPr>' % P(R(T("섹션 머리말")))) + R(T("본문")))
        md = hwpx(x).to_markdown()
        self.assertIn("머리말: 섹션 머리말", md)

    def test_alt_text_without_newline(self):
        self.assertEqual(hr._clean_alt("그림입니다. 원본 그림의 이름: 네모.jpg 원본 그림의 크기: 가로 1"),
                         "네모.jpg")
        self.assertEqual(hr._clean_alt("그림입니다.\r\n원본 그림의 이름: 네모.jpg\r\n원본 그림의 크기: 1"),
                         "네모.jpg")

    def test_utf16_xml_rejected(self):
        evil = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "ENTITY_TEXT">]>'
                '<hs:sec %s><hp:p><hp:run><hp:t>&a;</hp:t></hp:run></hp:p></hs:sec>' % B.NS)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("mimetype", "application/hwp+zip")
            z.writestr("Contents/section0.xml", evil.encode("utf-16"))
        try:
            out = hr.read_document(buf.getvalue()).to_markdown()
            self.assertNotIn("ENTITY_TEXT", out)
        except hr.HwpReaderError:
            pass

    def test_deep_run_nesting_keeps_section(self):
        inner = "<hp:run>" * 3000 + T("깊음") + "</hp:run>" * 3000
        doc = hwpx(P(inner) + P(R(T("AFTER"))))
        self.assertIn("AFTER", doc.to_markdown())


class ResourceTests(unittest.TestCase):
    def assertFast(self, fn, seconds=3.0):
        t = time.time()
        result = fn()
        self.assertLess(time.time() - t, seconds)
        return result

    def test_salvage_is_linear(self):
        broken = "<hs:sec %s><hp:p>" % B.NS + "<hp:t>a" * 100000
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("mimetype", "application/hwp+zip")
            z.writestr("Contents/section0.xml", broken)
        doc = self.assertFast(lambda: hr.read_document(buf.getvalue()))
        self.assertIn("a", doc.to_text())

    def test_huge_spans(self):
        x = P(R('<hp:tbl rowCnt="1" colCnt="1"><hp:tr>%s%s</hp:tr></hp:tbl>' % (
            '<hp:tc><hp:subList>%s</hp:subList><hp:cellSpan colSpan="1" rowSpan="300000000"/></hp:tc>'
            % P(R(T("x"))),
            '<hp:tc><hp:subList>%s</hp:subList><hp:cellSpan colSpan="100000" rowSpan="1"/></hp:tc>'
            % P(R(T("y"))))))
        doc = self.assertFast(lambda: hwpx(x))
        md = self.assertFast(lambda: doc.to_markdown())
        self.assertIn("x", md)
        self.assertIn("y", md)

    def test_declared_dimensions_ignored(self):
        x = P(R('<hp:tbl rowCnt="65535" colCnt="65535"><hp:tr>%s</hp:tr></hp:tbl>' % tc(0, 0, "셀")))
        doc = self.assertFast(lambda: hwpx(x))
        md = doc.to_markdown()
        self.assertLess(len(md), 100)
        table = [b for b in doc.to_dict()["sections"][0]["blocks"] if b["type"] == "table"][0]
        self.assertEqual((table["rows"], table["cols"]), (1, 1))

    def test_symbol_number_bomb(self):
        self.assertLess(len(hr.format_number(400000000, "SYMBOL")), 20)
        self.assertLess(len(hr.format_number(400000000, 0x80)), 20)
        x = P(R('<hp:ctrl><hp:autoNum num="400000000" numType="TABLE">'
                '<hp:autoNumFormat type="SYMBOL"/></hp:autoNum></hp:ctrl>') + R(T(" 표")))
        md = self.assertFast(lambda: hwpx(x).to_markdown())
        self.assertLess(len(md), 100)

    def test_many_nested_fields_render_fast(self):
        s = "".join('<hp:ctrl><hp:fieldBegin id="%d" type="BOOKMARK"/></hp:ctrl>' % i for i in range(20000))
        s += T("본문") + "".join('<hp:ctrl><hp:fieldEnd beginIDRef="%d"/></hp:ctrl>' % i for i in range(20000))
        doc = hwpx(P(R(s)))
        md = self.assertFast(lambda: doc.to_markdown())
        self.assertIn("본문", md)


class RenderingTests(unittest.TestCase):
    def test_empty_and_title_box_tables(self):
        empty = '<hp:tbl rowCnt="2" colCnt="2"><hp:tr>%s%s</hp:tr><hp:tr>%s%s</hp:tr></hp:tbl>' % (
            tc(0, 0, ""), tc(1, 0, ""), tc(0, 1, ""), tc(1, 1, ""))
        title = '<hp:tbl rowCnt="1" colCnt="3"><hp:tr>%s%s%s</hp:tr></hp:tbl>' % (
            tc(0, 0, "1"), tc(1, 0, ""), tc(2, 0, "성 과"))
        md = hwpx(P(R(empty)) + P(R(title))).to_markdown()
        self.assertIn("[빈 표 2×2]", md)
        self.assertIn("1 성 과", md)
        self.assertNotIn("| 1 |", md)

    def test_html_text_not_quote_escaped(self):
        cells = "<hp:tr>%s%s</hp:tr><hp:tr>%s%s%s</hp:tr>" % (
            tc(0, 0, 'a "b" \'c\' &lt;d&gt;', cs=2), tc(2, 0, "z"),
            tc(0, 1, "1"), tc(1, 1, "2"), tc(2, 1, "3"))
        md = hwpx(P(R('<hp:tbl rowCnt="2" colCnt="3">%s</hp:tbl>' % cells))).to_markdown()
        self.assertIn('a "b" \'c\' &lt;d&gt;', md)
        self.assertNotIn("&quot;", md)

    def test_equation_spacing_tokens(self):
        md = hwpx(P(R('<hp:equation><hp:script>lim _{h ``rarrow`` 0}~k`</hp:script></hp:equation>'))).to_markdown()
        self.assertIn("[수식: lim _{h rarrow 0} k]", md)
        self.assertNotIn("`", md)

    def test_toc_line_not_heading(self):
        from test_hwp_reader import HEADER_XML
        x = P(R(T("서론")), para_pr="1") + P(R("<hp:t>서론<hp:tab/>3</hp:t>"), para_pr="1")
        md = hr.read_document(B.build_hwpx([x], header_xml=HEADER_XML)).to_markdown()
        lines = md.splitlines()
        self.assertTrue(any(l.startswith("# ") and "서론" in l for l in lines))
        self.assertTrue(any(not l.startswith("#") and l.rstrip().endswith("3") for l in lines))

    def test_overlapping_spans_are_clipped(self):
        cells = tc(0, 0, "A", cs=2) + tc(1, 0, "B") + tc(2, 0, "C")
        md = hwpx(P(R('<hp:tbl rowCnt="1" colCnt="3"><hp:tr>%s</hp:tr></hp:tbl>' % cells))).to_markdown()
        for word in ("A", "B", "C"):
            self.assertIn(word, md)

    def test_picture_markers_spaced(self):
        x = P(R('<hp:pic><hc:img binaryItemIDRef="i1"/></hp:pic><hp:pic><hc:img binaryItemIDRef="i2"/>'
                '</hp:pic>' + T("제목")))
        md = hwpx(x).to_markdown()
        self.assertIn("[그림: i1] [그림: i2] 제목", md)

    def test_info_counts_text_only(self):
        x = P(R('<hp:tbl rowCnt="2" colCnt="2"><hp:tr>%s%s</hp:tr><hp:tr>%s%s</hp:tr></hp:tbl>' % (
            tc(0, 0, "abc"), tc(1, 0, ""), tc(0, 1, ""), tc(1, 1, ""))))
        info = hwpx(x).info()
        self.assertEqual(info["statistics"]["characters"], 3)
        self.assertGreater(info["statistics"]["markdown_chars"], 3)

    def test_paging_never_ends_inside_table(self):
        rows = "".join("<hp:tr>%s%s</hp:tr>" % (tc(0, r + 1, "행%d" % r), tc(1, r + 1, "값 %d" % r))
                       for r in range(60))
        merged = '<hp:tbl rowCnt="61" colCnt="2"><hp:tr>%s</hp:tr>%s</hp:tbl>' % (
            tc(0, 0, "머리", cs=2), rows)
        intro = P(R(T("가" * 600)))
        text = hwpx(intro + P(R(merged))).to_markdown()
        page = hr._page(text, 0, 700)
        body = page.split("[... 출력이 잘렸습니다")[0]
        self.assertGreaterEqual(body.count("</table>"), body.count("<table"))


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        r = subprocess.run([sys.executable, "-I", SCRIPT] + list(args),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        r.stdout = r.stdout.replace(b"\r\n", b"\n")
        r.stderr = r.stderr.replace(b"\r\n", b"\n")
        return r

    def test_json_paging_refused(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "a.hwpx")
            with open(f, "wb") as fh:
                fh.write(sample_hwpx())
            r = self.run_cli(f, "-f", "json", "--max-chars", "100")
            self.assertEqual(r.returncode, 2)

    def test_output_into_existing_directory(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "문서.hwpx")
            with open(f, "wb") as fh:
                fh.write(sample_hwpx())
            out = os.path.join(d, "out")
            os.makedirs(out)
            r = self.run_cli(f, "-o", out)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(os.listdir(out), ["문서.md"])
            self.assertIn("저장(written)", r.stderr.decode("utf-8"))

    def test_images_from_several_documents_kept_apart(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a.hwp"), os.path.join(d, "b.hwp")
            for path, payload in ((a, b"\x89PNG one"), (b, b"\x89PNG two")):
                data = B.build_hwp(sample_docinfo(),
                                   [B.paragraph(0, [("ext", 11, "gso ")], [B.picture_ctrl(1, 1)])],
                                   extra_streams={"BinData/BIN0001.png": B.deflate(payload)})
                with open(path, "wb") as fh:
                    fh.write(data)
            imgs = os.path.join(d, "imgs")
            r = self.run_cli(a, b, "--extract-images", imgs)
            self.assertEqual(r.returncode, 0, r.stderr)
            got = {}
            for sub in sorted(os.listdir(imgs)):
                for name in os.listdir(os.path.join(imgs, sub)):
                    with open(os.path.join(imgs, sub, name), "rb") as fh:
                        got[sub] = fh.read()
            self.assertEqual(got, {"a": b"\x89PNG one", "b": b"\x89PNG two"})

    def test_extract_dotdot_name(self):
        doc = hr.read_document(sample_hwpx())
        for sec in doc.sections:
            for p in sec.blocks:
                for it in p.inlines:
                    if isinstance(it, hr.Picture):
                        it.name = "../../evil.png"
        doc._bin_loader = lambda name: b"data"
        with tempfile.TemporaryDirectory() as d:
            paths = doc.extract_images(os.path.join(d, "x"))
            for path in paths:
                self.assertTrue(os.path.abspath(path).startswith(os.path.abspath(os.path.join(d, "x"))))



class SecondPassTests(unittest.TestCase):
    """Issues found by the second, focused review."""

    def test_merged_repetition_is_bounded(self):
        big = "가" * 5000
        x = P(R('<hp:tbl rowCnt="48" colCnt="4096"><hp:tr>%s</hp:tr></hp:tbl>' % tc(0, 0, big, cs=4096, rs=48)))
        doc = hwpx(x)
        t = time.time()
        txt = doc.to_text()
        self.assertLess(time.time() - t, 3)
        self.assertLess(len(txt), 100000)
        self.assertEqual(txt.count(big), 1)

    def test_vertical_label_repeats_horizontal_does_not(self):
        rows = "<hp:tr>%s%s%s</hp:tr><hp:tr>%s%s</hp:tr>" % (
            tc(0, 0, "구분", rs=2), tc(1, 0, "고길동"), tc(2, 0, "홍길동", cs=2),
            tc(1, 1, "a"), tc(2, 1, "b"))
        txt = hwpx(P(R('<hp:tbl rowCnt="2" colCnt="4">%s</hp:tbl>' % rows))).to_text()
        self.assertEqual(txt.count("홍길동"), 1)
        self.assertEqual(txt.count("구분"), 2)

    def test_depth_cut_table_keeps_no_declared_size(self):
        inner = '<hp:tbl rowCnt="65535" colCnt="65535"></hp:tbl>'
        for _ in range(45):
            inner = '<hp:tbl rowCnt="1" colCnt="1"><hp:tr>' \
                    '<hp:tc><hp:subList>%s</hp:subList><hp:cellAddr colAddr="0" rowAddr="0"/>' \
                    '<hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc></hp:tr></hp:tbl>' % P(R(inner))
        doc = hwpx(P(R(inner)))
        self.assertLess(len(doc.to_json()), 2000000)

    def test_far_apart_anchors_compacted(self):
        cells = "<hp:tr>%s</hp:tr><hp:tr>%s</hp:tr>" % (tc(0, 0, "x"), tc(199999, 1, "far"))
        doc = hwpx(P(R('<hp:tbl rowCnt="2" colCnt="200000">%s</hp:tbl>' % cells)))
        md, txt = doc.to_markdown(), doc.to_text()
        self.assertIn("far", md)
        self.assertIn("far", txt)
        self.assertLess(len(md) + len(txt), 2000)

    def test_overlap_clipping_is_rectangular(self):
        cells = "<hp:tr>%s</hp:tr><hp:tr>%s</hp:tr><hp:tr>%s</hp:tr>" % (
            tc(0, 0, "BIG", cs=3, rs=3), tc(1, 1, "mid"), tc(0, 2, "low"))
        md = hwpx(P(R('<hp:tbl rowCnt="3" colCnt="3">%s</hp:tbl>' % cells)), ).to_markdown()
        import re
        widths = []
        for row in re.findall(r"<tr>(.*?)</tr>", md):
            w = 0
            for m in re.finditer(r'<td(?: rowspan="(\d+)")?(?: colspan="(\d+)")?>', row):
                w += int(m.group(2) or 1)
            widths.append(w)
        for word in ("BIG", "mid", "low"):
            self.assertIn(word, md)
        self.assertEqual(widths[0], 3)
        cells2 = "<hp:tr>%s%s</hp:tr><hp:tr>%s</hp:tr>" % (
            tc(0, 0, "X"), tc(1, 0, "A", rs=2), tc(0, 1, "B", cs=2))
        doc = hwpx(P(R('<hp:tbl rowCnt="2" colCnt="2">%s</hp:tbl>' % cells2)))
        table = [b for b in doc.to_dict()["sections"][0]["blocks"] if b["type"] == "table"][0]
        self.assertEqual(table["cols"], 2)

    def test_record_budget_keeps_partial_section(self):
        s = b"".join(B.paragraph(0, ["문단 %d" % i]) for i in range(3000))
        data = B.build_hwp(sample_docinfo(), [s])
        doc = hr.Document()
        parser = hr._HwpParser(data, doc)
        parser.budget = hr._Budget(records=4095)    # cut at the first 4096-record check
        parser.parse()
        txt = doc.to_text()
        self.assertIn("문단 0", txt)
        self.assertNotIn("문단 2999", txt)
        self.assertTrue(any("한도" in w for w in doc.warnings))

    def test_unreadable_body_falls_back_to_preview(self):
        broken = B.rec(0x42, 0, B.para_header(5)) + struct.pack("<I", 0x43 | (1 << 10) | (200 << 20))   # PARA_TEXT cut off: no payload
        data = B.build_hwp(sample_docinfo(), [broken], prvtext="미리보기 본문 내용")
        doc = hr.read_document(data)
        self.assertIn("미리보기 본문 내용", doc.to_text())
        self.assertTrue(any("미리보기" in w for w in doc.warnings))
        clean_empty = B.build_hwp(sample_docinfo(), [B.paragraph(0, None)], prvtext="오래된 미리보기")
        self.assertNotIn("오래된", hr.read_document(clean_empty).to_text())

    def test_fill_in_fields_keep_table_visible(self):
        field = ('<hp:ctrl><hp:fieldBegin id="%d" type="CLICK_HERE"><hp:parameters>'
                 '<hp:stringParam name="Direction">%s</hp:stringParam></hp:parameters></hp:fieldBegin>'
                 '</hp:ctrl><hp:ctrl><hp:fieldEnd beginIDRef="%d"/></hp:ctrl>')
        def fcell(col, row, i, hint):
            return ('<hp:tc><hp:subList>%s</hp:subList><hp:cellAddr colAddr="%d" rowAddr="%d"/>'
                    '<hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>' % (P(R(field % (i, hint, i))), col, row))
        rows = "<hp:tr>%s%s</hp:tr><hp:tr>%s%s</hp:tr>" % (
            fcell(0, 0, 1, "성명"), fcell(1, 0, 2, "날짜"), fcell(0, 1, 3, "주소"), fcell(1, 1, 4, "전화"))
        md = hwpx(P(R('<hp:tbl rowCnt="2" colCnt="2">%s</hp:tbl>' % rows))).to_markdown()
        self.assertNotIn("빈 표", md)
        self.assertIn("[입력란: 주소]", md)

    def test_memo_spanning_table_kept(self):
        rows = "<hp:tr>%s%s</hp:tr><hp:tr>%s%s</hp:tr>" % (tc(0, 0, "a"), tc(1, 0, "b"), tc(0, 1, "c"), tc(1, 1, "d"))
        x = P(R('<hp:ctrl><hp:fieldBegin id="3" type="MEMO"><hp:subList>%s</hp:subList></hp:fieldBegin></hp:ctrl>'
                % P(R(T("표 확인 필요")))) + R(T("앞 글")) +
              R('<hp:tbl rowCnt="2" colCnt="2">%s</hp:tbl>' % rows) + R(T("뒤 글")) +
              R('<hp:ctrl><hp:fieldEnd beginIDRef="3"/></hp:ctrl>'))
        self.assertIn("표 확인 필요", hwpx(x).to_markdown())

    def test_button_caption_ampersand(self):
        md = hwpx(P(R('<hp:checkBtn caption="IP R&amp;&amp;D연계" value="CHECKED"/>'))).to_markdown()
        self.assertIn("☑ IP R&D연계", md)

    def test_json_lists_always_present(self):
        data = hwpx(P(R(T("x")))).to_dict()
        self.assertEqual(data["notes"], [])
        self.assertEqual(data["comments"], [])
        self.assertEqual(data["sections"][0]["headers_footers"], [])

    def test_page_guards(self):
        text = ("A" * 300 + "\n\n<table>\n<tr><td><table><tr><td>in</td></tr></table>" + "b" * 50 +
                "</td></tr>\n<tr><td>" + "c" * 600 + "</td></tr>\n</table>\n\nend")
        page = hr._page(text, 0, 700)
        body = page.split("\n\n[... 출력이 잘렸습니다")[0]
        self.assertNotIn("<table", body)
        words = "word " * 400
        page = hr._page(words, 0, 703)
        self.assertTrue(page.split("\n\n[... 출력이 잘렸습니다")[0].endswith("word"))

    def test_output_dir_with_trailing_slash(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "a.hwpx")
            with open(f, "wb") as fh:
                fh.write(sample_hwpx())
            out = os.path.join(d, "newdir") + os.sep
            r = subprocess.run([sys.executable, "-I", SCRIPT, f, "-o", out],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(os.listdir(out), ["a.md"])


if __name__ == "__main__":
    unittest.main()
