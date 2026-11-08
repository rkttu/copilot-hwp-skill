# -*- coding: utf-8 -*-
# Copyright 2026 rkttu@rkttu.com (남정현)
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for hwp_reader (stdlib unittest; no corpus needed).

Run:  python3 -m unittest discover -s tests -v
"""
import ast
import io
import json
import os
import random
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT_DIR = os.path.normpath(os.path.join(HERE, "..", "hwp-reader", "scripts"))
SCRIPT = os.path.join(SCRIPT_DIR, "hwp_reader.py")
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, HERE)

import hwp_reader as hr  # noqa: E402
import builders as B     # noqa: E402


# ---------------------------------------------------------------------------
# sample documents
# ---------------------------------------------------------------------------

def sample_docinfo():
    di = b""
    di += B.rec(0x12, 0, struct.pack("<HH", 0x0001, 1) + B.wstr("png"))       # BIN_DATA #1
    di += B.rec(0x17, 0, B.numbering_record(
        ["^1.", "^2.", "^3)", "^4)", "(^5)", "(^6)", "^7"], [0, 8, 0, 8, 0, 8, 1]))
    di += B.rec(0x18, 0, struct.pack("<IHHI", 0, 0, 0, 0) + struct.pack("<H", ord("●")))
    di += B.rec(0x19, 0, B.para_shape())            # 0 normal
    di += B.rec(0x19, 0, B.para_shape(1, 0))        # 1 outline level 1
    di += B.rec(0x19, 0, B.para_shape(1, 1))        # 2 outline level 2
    di += B.rec(0x19, 0, B.para_shape(3, 0, 1))     # 3 bullet #1
    di += B.rec(0x1A, 0, B.style_record("바탕글", "Normal"))
    di += B.rec(0x1A, 0, B.style_record("개요 1", "Outline 1"))
    return di


def secd_ctrl(level, outline_numbering_id=1):
    payload = struct.pack("<I", B.cid("secd")) + struct.pack("<IHHHI", 0, 0, 0, 0, 8000)
    payload += struct.pack("<HH", outline_numbering_id, 1) + b"\0" * 20
    return B.rec(0x47, level, payload)


def sample_section():
    s = b""
    s += B.paragraph(0, [("ext", 2, "secd"), "문서 제목"], [secd_ctrl(1)])
    s += B.paragraph(0, ["서론"], shape_id=1, style_id=1)
    s += B.paragraph(0, ["배경"], shape_id=2)
    s += B.paragraph(0, ["자세한 내용은 ", ("ext", 3, "%hlk"), "홈페이지", ("inline", 4), "를 참고"],
                     [B.hyperlink_ctrl(1, "https://example.com/a")])
    s += B.paragraph(0, ["본문 문장", ("ext", 17, "fn  "), "."],
                     [B.ctrl_with_list(1, "fn  ", [["각주 내용입니다"]], b"\0" * 8)])
    cells = [
        (0, 0, 1, 2, ["병합 머리"]), (0, 2, 1, 1, ["C"]),
        (1, 0, 1, 1, ["가"]), (1, 1, 1, 1, ["나|다"]), (1, 2, 2, 1, ["세로병합"]),
        (2, 0, 1, 1, ["라"]), (2, 1, 1, 1, ["마"]),
    ]
    s += B.paragraph(0, [("ext", 11, "tbl ")], [B.table_ctrl(1, 3, 3, cells, caption=["표 1. 예시 표"])])
    s += B.paragraph(0, ["수식: ", ("ext", 11, "eqed")], [B.equation_ctrl(1, "a over b")])
    s += B.paragraph(0, [("ext", 16, "head"), "머리말 있는 문단"],
                     [B.ctrl_with_list(1, "head", [["회사 머리말"]], struct.pack("<I", 0))])
    s += B.paragraph(0, [("ext", 11, "gso ")], [B.picture_ctrl(1, 1, "로고 그림")])
    s += B.paragraph(0, [("ext", 11, "gso ")], [B.textbox_ctrl(1, [["글상자 안의 글"]])])
    s += B.paragraph(0, ["항목 하나"], shape_id=3)
    s += B.paragraph(0, ["A", ("inline", 9), "B", ("char", 10), "C"])
    s += B.paragraph(0, ["숨은", ("ext", 15, "tcmt")],
                     [B.ctrl_with_list(1, "tcmt", [["숨은 설명 내용"]])])
    s += B.paragraph(0, ["한자 漢字 😀 이모지"])
    s += B.paragraph(0, None)
    return s


def sample_hwp(**kw):
    summary = B.summary_info({2: "샘플 제목", 4: "작성자"})
    return B.build_hwp(sample_docinfo(), [sample_section()], summary=summary,
                       prvtext="미리보기", extra_streams={"BinData/BIN0001.png": B.deflate(b"\x89PNG fake")},
                       **kw)


HEADER_XML = (
    '<?xml version="1.0" encoding="UTF-8"?><hh:head %s version="1.5" secCnt="1"><hh:refList>'
    '<hh:numberings itemCnt="1"><hh:numbering id="1" start="0">'
    '<hh:paraHead start="1" level="1" numFormat="DIGIT">^1.</hh:paraHead>'
    '<hh:paraHead start="1" level="2" numFormat="HANGUL_SYLLABLE">^2.</hh:paraHead>'
    '</hh:numbering></hh:numberings>'
    '<hh:bullets itemCnt="1"><hh:bullet id="1" char="■" useImage="0"/></hh:bullets>'
    '<hh:paraProperties itemCnt="4">'
    '<hh:paraPr id="0"><hh:heading type="NONE" idRef="0" level="0"/></hh:paraPr>'
    '<hh:paraPr id="1"><hh:heading type="OUTLINE" idRef="0" level="0"/></hh:paraPr>'
    '<hh:paraPr id="2"><hh:heading type="OUTLINE" idRef="0" level="1"/></hh:paraPr>'
    '<hh:paraPr id="3"><hh:heading type="BULLET" idRef="1" level="0"/></hh:paraPr>'
    '</hh:paraProperties>'
    '<hh:styles itemCnt="1"><hh:style id="0" type="PARA" name="바탕글" engName="Normal" '
    'paraPrIDRef="0" charPrIDRef="0"/></hh:styles></hh:refList></hh:head>' % B.NS)


def sample_hwpx_sections():
    p, r, t = B.hp_p, B.hp_run, B.hp_t
    sec0 = ""
    sec0 += p(r('<hp:secPr id="" outlineShapeIDRef="1"/>') + r(t("문서 제목")))
    sec0 += p(r(t("서론")), para_pr="1")
    sec0 += p(r(t("배경")), para_pr="2")
    sec0 += p(r(t("자세한 내용은 ")) +
              r('<hp:ctrl><hp:fieldBegin id="1" type="HYPERLINK" name=""><hp:parameters cnt="2">'
                '<hp:stringParam name="Command">https\\://example.com/b;1;0;0;</hp:stringParam>'
                '<hp:stringParam name="Path">https://example.com/b</hp:stringParam>'
                '</hp:parameters></hp:fieldBegin></hp:ctrl>') +
              r(t("홈페이지")) + r('<hp:ctrl><hp:fieldEnd beginIDRef="1"/></hp:ctrl>') + r(t("를 참고")))
    sec0 += p(r(t("본문 문장")) + r('<hp:ctrl><hp:footNote number="1"><hp:subList>' +
                                  p(r(t("각주 내용입니다"))) + '</hp:subList></hp:footNote></hp:ctrl>') + r(t(".")))

    def tc(col, row, cs, rs, text):
        return ('<hp:tc><hp:subList>%s</hp:subList><hp:cellAddr colAddr="%d" rowAddr="%d"/>'
                '<hp:cellSpan colSpan="%d" rowSpan="%d"/><hp:cellSz width="1" height="1"/></hp:tc>'
                % (p(r(t(text))), col, row, cs, rs))
    nested = ('<hp:tbl rowCnt="1" colCnt="2"><hp:tr>%s%s</hp:tr></hp:tbl>'
              % (tc(0, 0, 1, 1, "내부1"), tc(1, 0, 1, 1, "내부2")))
    table = ('<hp:tbl rowCnt="3" colCnt="3"><hp:caption><hp:subList>%s</hp:subList></hp:caption>'
             '<hp:tr>%s%s</hp:tr><hp:tr>%s%s%s</hp:tr><hp:tr>%s%s</hp:tr></hp:tbl>' % (
                 p(r(t("표 1. 예시 표"))),
                 tc(0, 0, 2, 1, "병합 머리"), tc(2, 0, 1, 1, "C"),
                 tc(0, 1, 1, 1, "가"), tc(1, 1, 1, 1, "나|다"), tc(2, 1, 1, 2, "세로병합"),
                 '<hp:tc><hp:subList>%s</hp:subList><hp:cellAddr colAddr="0" rowAddr="2"/>'
                 '<hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>' % p(r(nested)),
                 tc(1, 2, 1, 1, "마")))
    sec0 += p(r(table))
    sec0 += p(r(t("수식: ") + '<hp:equation id="5"><hp:script>a over b</hp:script></hp:equation>'))
    sec0 += p(r('<hp:ctrl><hp:header id="1" applyPageType="BOTH"><hp:subList>' +
                p(r(t("회사 머리말"))) + '</hp:subList></hp:header></hp:ctrl>') + r(t("머리말 있는 문단")))
    sec0 += p(r('<hp:pic id="9"><hc:img binaryItemIDRef="image1"/>'
                '<hp:shapeComment>로고 그림</hp:shapeComment></hp:pic>'))
    sec0 += p(r('<hp:rect id="3"><hp:drawText><hp:subList>' + p(r(t("글상자 안의 글"))) +
                '</hp:subList></hp:drawText></hp:rect>'))
    sec0 += p(r(t("항목 하나")), para_pr="3")
    sec0 += p(r("<hp:t>A<hp:tab/>B<hp:lineBreak/>C</hp:t>"))
    sec0 += p(r('<hp:dutmal posType="TOP"><hp:mainText>본말</hp:mainText><hp:subText>덧말</hp:subText></hp:dutmal>'))
    sec1 = p(r(t("두 번째 구역")))
    return [sec0, sec1]


def sample_hwpx(**kw):
    extra = {"BinData/image1.png": b"\x89PNG fake", "Preview/PrvText.txt": "미리보기".encode("utf-8")}
    return B.build_hwpx(sample_hwpx_sections(), header_xml=HEADER_XML, extra=extra,
                        manifest_extra='<opf:item id="image1" href="BinData/image1.png" '
                                       'media-type="image/png" isEmbeded="1"/>', **kw)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

class CryptoTests(unittest.TestCase):
    def test_aes_fips197(self):
        key = bytes(range(16))
        ct = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(hr.AES128Decryptor(key).decrypt_ecb(ct).hex(),
                         "00112233445566778899aabbccddeeff")
        self.assertEqual(B.aes128_encrypt_ecb(key, bytes.fromhex("00112233445566778899aabbccddeeff")), ct)

    def test_aes_roundtrip(self):
        rnd = random.Random(7)
        key = bytes(rnd.randrange(256) for _ in range(16))
        data = bytes(rnd.randrange(256) for _ in range(160))
        self.assertEqual(hr.AES128Decryptor(key).decrypt_ecb(B.aes128_encrypt_ecb(key, data)), data)

    def test_distribution_key(self):
        key = b"fedcba9876543210"
        for seed in (0, 1, 0xDEADBEEF, 0x12345678):
            self.assertEqual(hr._distribution_keys(B.distribution_blob(seed, key))[0], key)


class NumberingTests(unittest.TestCase):
    def test_formats(self):
        f = hr.format_number
        self.assertEqual(f(3, "HANGUL_SYLLABLE"), "다")
        self.assertEqual(f(14, "HANGUL_SYLLABLE"), "하")
        self.assertEqual(f(15, "HANGUL_SYLLABLE"), "거")
        self.assertEqual(f(29, "HANGUL_SYLLABLE"), "고")
        self.assertEqual(f(84, "HANGUL_SYLLABLE"), "히")
        self.assertEqual(f(85, "HANGUL_SYLLABLE"), "가")
        self.assertEqual(f(2, "CIRCLED_DIGIT"), "②")
        self.assertEqual(f(21, "CIRCLED_DIGIT"), "①")
        self.assertEqual(f(14, "ROMAN_CAPITAL"), "XIV")
        self.assertEqual(f(4, "ROMAN_SMALL"), "iv")
        self.assertEqual(f(28, "LATIN_CAPITAL"), "B")
        self.assertEqual(f(5, 0x80), "**")
        self.assertEqual(f(1, "CIRCLED_HANGUL_JAMO"), "㉠")
        self.assertEqual(f(23, "HANGUL_PHONETIC"), "이십삼")
        self.assertEqual(f(11, "IDEOGRAPH"), "十一")
        self.assertEqual(f(2, "DECAGON_CIRCLE"), "을")
        self.assertEqual(f(5, 0), "5")

    def test_counters(self):
        nd = hr._NumberingDef()
        nd.levels = {1: ("^1.", "DIGIT", 1), 2: ("^1.^2.", "HANGUL_SYLLABLE", 1)}
        st = hr._NumberingState({7: nd})
        self.assertEqual(st.next_label(7, 1), "1.")
        self.assertEqual(st.next_label(7, 2), "1.가.")
        self.assertEqual(st.next_label(7, 2), "1.나.")
        self.assertEqual(st.next_label(7, 1), "2.")
        self.assertEqual(st.next_label(7, 2), "2.가.")
        self.assertIsNone(st.next_label(99, 1))

    def test_path_templates(self):
        nd = hr._NumberingDef()
        nd.levels = {1: ("^N", "DIGIT", 1), 2: ("^n", "DIGIT", 1), 3: ("(^3)", "LATIN_SMALL", 1)}
        st = hr._NumberingState({1: nd})
        self.assertEqual(st.next_label(1, 1), "1.")
        self.assertEqual(st.next_label(1, 2), "1.1")
        self.assertEqual(st.next_label(1, 2), "1.2")
        self.assertEqual(st.next_label(1, 3), "(a)")

    def test_hyperlink_command(self):
        t = hr._hyperlink_target
        self.assertEqual(t("http\\://google.com;1;0;0;"), "http://google.com")
        self.assertEqual(t("mailto\\:a@b.com\\?subject=x|전자우편;2;0;0;"), "mailto:a@b.com?subject=x")
        self.assertEqual(t("D\\:\\dir\\file.hml|label;3;0;0;"), "D:\\dir\\file.hml")
        self.assertIsNone(t("|북마크;0;0;0;"))

    def test_bullet_pua(self):
        self.assertEqual(hr._bullet_char("\uf06c"), "●")
        self.assertEqual(hr._bullet_char("\ue123", "•"), "•")
        self.assertEqual(hr._bullet_char("※"), "※")


class HwpTests(unittest.TestCase):
    def check_common(self, doc):
        md = doc.to_markdown()
        self.assertIn("문서 제목", md)
        self.assertIn("# 1. 서론", md)
        self.assertIn("## 가. 배경", md)
        self.assertIn("[홈페이지](https://example.com/", md)
        self.assertIn("본문 문장[^1].", md)
        self.assertIn("[^1]: 각주 내용입니다", md)
        self.assertIn('<td colspan="2">병합 머리</td>', md)
        self.assertIn('<td rowspan="2">세로병합</td>', md)
        self.assertIn("표 1. 예시 표", md)
        self.assertIn("[수식: a over b]", md)
        self.assertIn("머리말: 회사 머리말", md)
        self.assertIn("글상자 안의 글", md)
        self.assertIn("한자 漢字 😀 이모지", md)
        self.assertIn("숨은 설명: 숨은 설명 내용", md)
        txt = doc.to_text()
        self.assertIn("A\tB\nC", txt)
        self.assertIn("1. 서론", txt)
        # plain-text tables: a vertically merged label repeats in the rows it
        # spans; horizontal spans are shown once
        self.assertIn("| 병합 머리 |  | C |", txt)
        self.assertIn("| 라 | 마 | 세로병합 |", txt)
        self.assertIn("[각주 1] 각주 내용입니다", txt)
        data = json.loads(doc.to_json())
        tables = [b for s in data["sections"] for b in s["blocks"] if b["type"] == "table"]
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]["grid"][0], ["병합 머리", None, "C"])
        self.assertEqual(tables[0]["grid"][2][2], None)
        info = doc.info()
        self.assertEqual(info["statistics"]["tables"], 1)
        self.assertEqual(info["statistics"]["footnotes"], 1)
        return md

    def test_compressed(self):
        doc = hr.read_document(sample_hwp())
        self.assertEqual(doc.format, "hwp")
        self.assertEqual(doc.version, "5.1.0.1")
        self.assertTrue(doc.flags["compressed"])
        self.assertEqual(doc.metadata.get("title"), "샘플 제목")
        self.assertEqual(doc.metadata.get("author"), "작성자")
        self.assertEqual(doc.preview_text, "미리보기")
        md = self.check_common(doc)
        self.assertIn("- 항목 하나", md)
        self.assertIn("[그림: 로고 그림]", md)
        self.assertEqual(doc.warnings, [])

    def test_uncompressed(self):
        doc = hr.read_document(sample_hwp(compressed=False))
        self.assertFalse(doc.flags["compressed"])
        self.check_common(doc)

    def test_bullet_label_text_mode(self):
        txt = hr.read_document(sample_hwp()).to_text()
        self.assertIn("● 항목 하나", txt)

    def test_extract_images(self):
        doc = hr.read_document(sample_hwp())
        with tempfile.TemporaryDirectory() as d:
            paths = doc.extract_images(d)
            self.assertEqual(len(paths), 1)
            with open(paths[0], "rb") as f:
                self.assertEqual(f.read(), b"\x89PNG fake")
            self.assertIn("](%s)" % paths[0].replace(" ", "%20"), doc.to_markdown())

    def test_password(self):
        with self.assertRaises(hr.EncryptedDocumentError):
            hr.read_document(sample_hwp(password=True))

    def test_master_page_and_clickhere(self):
        secd = secd_ctrl(1) + B.rec(0x48, 2, struct.pack("<HHI", 1, 0, 0) + b"\0" * 26)
        secd += B.paragraph(2, ["바탕쪽 문구"])
        guide = "여기에 입력"
        cmd = "Clickhere:set:66:Direction:wstring:%d:%s HelpState:wstring:0: " % (len(guide), guide)
        clk = B.rec(0x47, 1, struct.pack("<I", B.cid("%clk")) + struct.pack("<IB", 0, 0)
                    + B.wstr(cmd) + struct.pack("<II", 1, 0))
        clk2 = B.rec(0x47, 1, struct.pack("<I", B.cid("%clk")) + struct.pack("<IB", 0, 0)
                     + B.wstr(cmd) + struct.pack("<II", 2, 0))
        s = B.paragraph(0, [("ext", 2, "secd"), "본문"], [secd])
        s += B.paragraph(0, ["성명: ", ("ext", 3, "%clk"), ("inline", 4), " / 소속: ",
                             ("ext", 3, "%clk"), "기획팀", ("inline", 4)], [clk, clk2])
        doc = hr.read_document(B.build_hwp(sample_docinfo(), [s]))
        md = doc.to_markdown()
        self.assertIn("성명: [입력란: 여기에 입력] / 소속: 기획팀", md)
        self.assertIn("바탕쪽: 바탕쪽 문구", md)
        self.assertNotIn("바탕쪽", doc.to_markdown(headers_footers=False))

    def test_memo(self):
        cmd = "MEMO/0/7/0/0/user/\\;;"
        field = B.rec(0x47, 1, struct.pack("<I", B.cid("%unk")) + struct.pack("<IB", 1, 0)
                      + B.wstr(cmd) + struct.pack("<II", 1234, 7))
        s = B.paragraph(0, ["앞 ", ("ext", 3, "%%me"), "검토 대상", ("inline", 4), " 뒤"], [field])
        s += B.paragraph(0, ["마지막 문단"])
        s += B.rec(0x5D, 1, struct.pack("<I", 7))
        s += B.rec(0x48, 1, struct.pack("<HHIII", 1, 0, 0, 0, 0))
        s += B.paragraph(1, ["메모 본문입니다"])
        doc = hr.read_document(B.build_hwp(sample_docinfo(), [s]))
        md = doc.to_markdown()
        self.assertIn("앞 검토 대상 뒤", md)
        self.assertIn("마지막 문단", md)
        self.assertNotIn("마지막 문단\n\n메모 본문", md)
        self.assertIn('메모 ("검토 대상"): 메모 본문입니다', md)
        self.assertEqual(json.loads(doc.to_json())["comments"][0]["anchor"], "검토 대상")
        self.assertEqual(doc.info()["statistics"]["comments"], 1)

    def test_distribution(self):
        data = B.build_distribution_hwp(sample_docinfo(), sample_section())
        doc = hr.read_document(data)
        self.assertTrue(doc.flags["distribution"])
        self.check_common(doc)

    def test_hwp3_and_garbage(self):
        with self.assertRaises(hr.UnsupportedFormatError):
            hr.read_document(b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05" + b"\0" * 200)
        with self.assertRaises(hr.UnsupportedFormatError):
            hr.read_document(b"hello world, not a document")
        with self.assertRaises(hr.UnsupportedFormatError):
            hr.read_document(B.build_cfb({"WordDocument": b"x" * 100}))
        with self.assertRaises(hr.CorruptDocumentError):
            hr.read_document(b"")

    def test_truncated_never_crashes(self):
        data = sample_hwp()
        for cut in (600, 1200, len(data) // 2, len(data) - 100):
            try:
                hr.read_document(data[:cut]).to_markdown()
            except hr.HwpReaderError:
                pass

    def test_fuzz_hwp(self):
        base = bytearray(sample_hwp(compressed=False))
        rnd = random.Random(1234)
        for _ in range(150):
            buf = bytearray(base)
            for _ in range(rnd.randint(1, 30)):
                buf[rnd.randrange(512, len(buf))] = rnd.randrange(256)
            try:
                doc = hr.read_document(bytes(buf))
                doc.to_markdown()
                doc.to_text()
                doc.to_json()
            except hr.HwpReaderError:
                pass


class HwpxTests(unittest.TestCase):
    def test_full(self):
        doc = hr.read_document(sample_hwpx())
        self.assertEqual(doc.format, "hwpx")
        self.assertEqual(doc.metadata.get("title"), "테스트 문서")
        self.assertEqual(doc.metadata.get("author"), "홍길동")
        md = doc.to_markdown()
        for s in ("문서 제목", "# 1. 서론", "## 가. 배경", "[홈페이지](https://example.com/b)",
                  "본문 문장[^1].", "[^1]: 각주 내용입니다", '<td colspan="2">병합 머리</td>',
                  '<td rowspan="2">세로병합</td>',
                  "<td><table><tr><td>내부1</td><td>내부2</td></tr></table></td>",
                  "[수식: a over b]", "머리말: 회사 머리말", "[그림: 로고 그림]", "글상자 안의 글",
                  "- 항목 하나", "본말(덧말)", "두 번째 구역", "<caption>표 1. 예시 표</caption>"):
            self.assertIn(s, md)
        self.assertLess(md.index("문서 제목"), md.index("두 번째 구역"))
        txt = doc.to_text()
        self.assertIn("A\tB\nC", txt)
        self.assertIn("■ 항목 하나", txt)
        self.assertEqual(doc.preview_text, "미리보기")
        self.assertEqual(doc.binary("image1.png"), b"\x89PNG fake")

    def test_memo_field(self):
        p, r, t = B.hp_p, B.hp_run, B.hp_t
        sx = p(r(t("앞 ")) + r('<hp:ctrl><hp:fieldBegin id="9" type="MEMO"><hp:parameters cnt="1">'
                              '<hp:stringParam name="Command">MEMO/65535/1/0/0/user/\\;;</hp:stringParam>'
                              '</hp:parameters><hp:subList>' + p(r(t("메모 내용"))) +
                              '</hp:subList></hp:fieldBegin></hp:ctrl>') +
               r(t("대상 텍스트")) + r('<hp:ctrl><hp:fieldEnd beginIDRef="9"/></hp:ctrl>') + r(t(" 뒤")))
        sx += ('<hp:memogroup><hp:memo id="m2"><hp:paraList>%s</hp:paraList></hp:memo></hp:memogroup>'
               % p(r(t("그룹 메모"))))
        md = hr.read_document(B.build_hwpx([sx])).to_markdown()
        self.assertIn("앞 대상 텍스트 뒤", md)
        self.assertIn('메모 ("대상 텍스트"): 메모 내용', md)
        self.assertIn("메모: 그룹 메모", md)
        self.assertNotIn("메모 내용\n", md.split("---")[0])

    def test_track_changes_master_page_clickhere(self):
        p, r, t = B.hp_p, B.hp_run, B.hp_t
        sx = p(r('<hp:secPr id=""><hp:masterPage idRef="masterpage0"/></hp:secPr>') + r(t("머리")))
        sx += p(r('<hp:t>변경<hp:deleteBegin Id="2" TcId="2"/> 추적<hp:deleteEnd Id="2" TcId="2" '
                  'paraend="0"/> 끝<hp:insertBegin Id="1" TcId="1"/>추가<hp:insertEnd Id="1" TcId="1"/>'
                  '</hp:t>'))
        sx += p(r('<hp:t>열림<hp:deleteBegin Id="9" TcId="9"/> 남는 글</hp:t>'))   # unclosed: keep text
        sx += p(r(t("이름: ")) + r('<hp:ctrl><hp:fieldBegin id="5" type="CLICK_HERE"><hp:parameters>'
                                 '<hp:stringParam name="Direction">이름을 쓰세요</hp:stringParam>'
                                 '</hp:parameters></hp:fieldBegin></hp:ctrl>') +
                r('<hp:ctrl><hp:fieldEnd beginIDRef="5"/></hp:ctrl>'))
        master = ('<?xml version="1.0" encoding="UTF-8"?><masterPage %s id="0" type="BOTH">'
                  '<hp:subList>%s</hp:subList></masterPage>' % (B.NS, p(r(t("배경 문구")))))
        data = B.build_hwpx([sx], extra={"Contents/masterpage0.xml": master},
                            manifest_extra='<opf:item id="masterpage0" href="Contents/masterpage0.xml" '
                                           'media-type="application/xml"/>')
        doc = hr.read_document(data)
        md = doc.to_markdown()
        self.assertIn("변경 끝추가", md)
        self.assertNotIn("추적", md)
        self.assertIn("열림 남는 글", md)
        self.assertIn("이름: [입력란: 이름을 쓰세요]", md)
        self.assertIn("바탕쪽: 배경 문구", md)
        self.assertTrue(doc.flags.get("track_changes"))

    def test_simple_table_is_gfm(self):
        p, r, t = B.hp_p, B.hp_run, B.hp_t
        cells = "".join('<hp:tc><hp:subList>%s</hp:subList><hp:cellAddr colAddr="%d" rowAddr="%d"/>'
                        '<hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>' % (p(r(t(x))), i % 2, i // 2)
                        for i, x in enumerate(["이름", "값", "a|b", "줄1"]))
        sx = p(r('<hp:tbl rowCnt="2" colCnt="2"><hp:tr>%s</hp:tr></hp:tbl>' % cells))
        md = hr.read_document(B.build_hwpx([sx])).to_markdown()
        self.assertIn("| 이름 | 값 |\n| --- | --- |\n| a\\|b | 줄1 |", md)

    def test_single_cell_table_unwrapped(self):
        p, r, t = B.hp_p, B.hp_run, B.hp_t
        sx = p(r('<hp:tbl rowCnt="1" colCnt="1"><hp:tr><hp:tc><hp:subList>%s%s</hp:subList>'
                 '<hp:cellAddr colAddr="0" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>'
                 '</hp:tr></hp:tbl>' % (p(r(t("상자 첫 줄"))), p(r(t("상자 둘째 줄"))))))
        md = hr.read_document(B.build_hwpx([sx])).to_markdown()
        self.assertIn("상자 첫 줄\n\n상자 둘째 줄", md)
        self.assertNotIn("|", md)

    def test_encrypted(self):
        with self.assertRaises(hr.EncryptedDocumentError):
            hr.read_document(sample_hwpx(encrypted=True))

    def test_doctype_rejected(self):
        evil = ('<!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
                '<hs:sec %s><hp:p><hp:run><hp:t>&b;</hp:t></hp:run></hp:p></hs:sec>' % B.NS)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("mimetype", "application/hwp+zip")
            z.writestr("Contents/section0.xml", evil)
        with self.assertRaises(hr.HwpReaderError):
            hr.read_document(buf.getvalue())

    def test_not_hwpx_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr("word/document.xml", "<w:document/>")
        with self.assertRaises(hr.UnsupportedFormatError):
            hr.read_document(buf.getvalue())

    def test_spine_order(self):
        p, r, t = B.hp_p, B.hp_run, B.hp_t
        data = B.build_hwpx([p(r(t("첫째"))), p(r(t("둘째")))])
        md = hr.read_document(data).to_markdown()
        self.assertLess(md.index("첫째"), md.index("둘째"))

    def test_fuzz_hwpx(self):
        base = sample_hwpx()
        rnd = random.Random(99)
        for _ in range(100):
            buf = bytearray(base)
            for _ in range(rnd.randint(1, 10)):
                buf[rnd.randrange(len(buf))] = rnd.randrange(256)
            try:
                hr.read_document(bytes(buf)).to_markdown()
            except hr.HwpReaderError:
                pass


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, "-I", SCRIPT] + list(args),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        # Windows text-mode stdout writes CRLF; compare in LF form
        r.stdout = r.stdout.replace(b"\r\n", b"\n")
        r.stderr = r.stderr.replace(b"\r\n", b"\n")
        return r

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            hwp = os.path.join(d, "샘플 문서.hwp")
            hwpx = os.path.join(d, "sample.hwpx")
            locked = os.path.join(d, "locked.hwp")
            junk = os.path.join(d, "junk.hwp")
            with open(hwp, "wb") as f:
                f.write(sample_hwp())
            with open(hwpx, "wb") as f:
                f.write(sample_hwpx())
            with open(locked, "wb") as f:
                f.write(sample_hwp(password=True))
            with open(junk, "wb") as f:
                f.write(b"junk" * 100)

            r = self.run_cli(hwp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("# 1. 서론", r.stdout.decode("utf-8"))

            r = self.run_cli(hwpx, "-f", "text")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("두 번째 구역", r.stdout.decode("utf-8"))

            r = self.run_cli(hwp, "--info")
            info = json.loads(r.stdout.decode("utf-8"))
            self.assertEqual(info["format"], "hwp")
            self.assertEqual(info["statistics"]["tables"], 1)

            out = os.path.join(d, "out", "doc.json")
            r = self.run_cli(hwpx, "-f", "json", "-o", out)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(out, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["format"], "hwpx")

            outdir = os.path.join(d, "many")
            r = self.run_cli(hwp, hwpx, "-o", outdir)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(sorted(os.listdir(outdir)), ["sample.md", "샘플 문서.md"])

            r = self.run_cli(hwp, "--max-chars", "50")
            self.assertIn("출력이 잘렸습니다", r.stdout.decode("utf-8"))

            r = self.run_cli(locked)
            self.assertEqual(r.returncode, 4)
            self.assertIn("password", r.stderr.decode("utf-8"))

            r = self.run_cli(junk)
            self.assertEqual(r.returncode, 3)

            r = self.run_cli(os.path.join(d, "missing.hwp"))
            self.assertEqual(r.returncode, 1)

            docs = os.path.join(d, "docs")                  # directory input
            os.makedirs(docs)
            with open(os.path.join(docs, "x.hwpx"), "wb") as f:
                f.write(sample_hwpx())
            r = self.run_cli(docs, "-f", "text")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("두 번째 구역", r.stdout.decode("utf-8"))
            empty = os.path.join(d, "empty")
            os.makedirs(empty)
            self.assertEqual(self.run_cli(empty).returncode, 3)
            with self.assertRaises(hr.HwpReaderError):
                hr.read_document(docs)

            r = self.run_cli(hwp, locked)          # partial failure keeps going
            self.assertEqual(r.returncode, 4)
            self.assertIn("서론", r.stdout.decode("utf-8"))

            imgdir = os.path.join(d, "imgs")
            r = self.run_cli(hwp, "--extract-images", imgdir)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.listdir(imgdir))

            # renamed extension: detection is by content
            renamed = os.path.join(d, "upload.bin")
            with open(renamed, "wb") as f:
                f.write(sample_hwpx())
            r = self.run_cli(renamed, "-f", "text")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("두 번째 구역", r.stdout.decode("utf-8"))

    def test_paging(self):
        with tempfile.TemporaryDirectory() as d:
            hwp = os.path.join(d, "a.hwp")
            with open(hwp, "wb") as f:
                f.write(sample_hwp())
            full = self.run_cli(hwp, "-f", "text").stdout.decode("utf-8")
            first = self.run_cli(hwp, "-f", "text", "--max-chars", "60").stdout.decode("utf-8")
            m = __import__("re").search(r"--offset (\d+)", first)
            self.assertIsNotNone(m)
            nxt = int(m.group(1))
            second = self.run_cli(hwp, "-f", "text", "--offset", str(nxt)).stdout.decode("utf-8")
            self.assertIn(full[nxt:nxt + 20].strip()[:10], second)
            beyond = self.run_cli(hwp, "--offset", "999999").stdout.decode("utf-8")
            self.assertIn("offset beyond end", beyond)

    def test_zip_archive_of_documents(self):
        with tempfile.TemporaryDirectory() as d:
            arc = os.path.join(d, "upload.zip")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("docs/XXXX.hwp", sample_hwp())        # renamed to CP949 below
                z.writestr("second.hwpx", sample_hwpx())
                z.writestr("readme.txt", "not a document")
                z.writestr("__MACOSX/docs/._XXXX.hwp", b"junk")
            raw = buf.getvalue().replace(b"XXXX.hwp", "한글.hwp".encode("cp949"))
            with open(arc, "wb") as f:
                f.write(raw)
            r = self.run_cli(arc, "-f", "text")
            self.assertEqual(r.returncode, 0, r.stderr)
            out = r.stdout.decode("utf-8")
            self.assertIn("upload.zip!docs/한글.hwp", out)
            self.assertIn("upload.zip!second.hwpx", out)
            self.assertIn("서론", out)
            self.assertIn("두 번째 구역", out)
            outdir = os.path.join(d, "o")
            r = self.run_cli(arc, "-o", outdir)
            self.assertEqual(sorted(os.listdir(outdir)), ["second.md", "한글.md"])


class StdlibOnlyTests(unittest.TestCase):
    ALLOWED = {"argparse", "datetime", "html", "io", "json", "os", "re", "struct", "sys",
               "zipfile", "zlib", "xml"}

    def test_imports_are_stdlib(self):
        with open(SCRIPT, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                mods.add((node.module or "").split(".")[0])
        self.assertTrue(mods <= self.ALLOWED, mods - self.ALLOWED)
        if hasattr(sys, "stdlib_module_names"):
            self.assertTrue(mods <= set(sys.stdlib_module_names))

    def test_no_network_or_subprocess(self):
        with open(SCRIPT, encoding="utf-8") as f:
            src = f.read()
        for bad in ("socket", "urllib", "http.client", "subprocess", "ctypes", "pip"):
            self.assertNotIn("import " + bad, src)


if __name__ == "__main__":
    unittest.main()
